"""A change to a package's source code, written by Claude from an instruction.

`python3 -m lfsbuilder customize RECIPE PACKAGE "INSTRUCTION" [--check CMD] [--keep]`
(and the form's Customizations box):

1. The source the build would compile: in a container of the recipe's portage
   checkpoint (its Gentoo pin and settings), Portage picks the version
   (`portageq best_visible`), and `ebuild ... clean prepare` fetches it (checked
   against the tree's signed Manifest), unpacks it and applies Gentoo's own
   patches. Its source directory (${S}) comes out as a tar, unpacked twice: a/
   as it is, b/ for Claude.
2. Claude edits b/ through Claude Code with four tools only (Read, Grep, Glob,
   Edit; no shell, no web), b/ given as an added directory and the session run
   in an empty directory (so neither this project's instructions nor any the
   source carries are loaded); edits are allowed there only.
3. The patch is made here, not taken from Claude: `diff -ruN a b`, refused
   when empty, binary, too large or not a plain diff. The owner reads it
   before keeping it.
4. Kept: <recipe>.patches/<package>/customization.patch, and a
   [[customizations]] entry in the recipe (package, instruction, check);
   steps.package() then compiles the package with it and runs the check.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import fixes as fixes_
from .recipe import CUSTOM_PATCH, Customization, Recipe, dump, load
from .runner import Shared

TIMEOUT = 1800
MAX_PATCH = 1_000_000

SYSTEM = """You change the source code of one program, to be built into a Linux distribution (lfsbuilder, on Gentoo).

You get the owner's instruction and the directory that holds the program's unpacked source, exactly the version that will be compiled (with Gentoo's own patches applied). Make the change the instruction asks for, in that directory, with the Edit tool:
- Find where the change belongs first (Grep, Glob, Read); change as little as the instruction needs.
- Keep the program building: valid code in the program's own language and style; no new files unless they are needed, no new dependencies.
- Change only files inside that directory; don't touch build outputs or generated files you can avoid.
- When done, answer with a summary: what you changed, in which files, in one or two sentences."""

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "files"],
}

PREPARE = r"""
set -e
cpv=$(portageq best_visible / ebuild "$PACKAGE")
[ -n "$cpv" ] || { echo "no visible version of $PACKAGE" >&2; exit 1; }
pf=${cpv#*/}
repo=$(portageq get_repo_path / gentoo)
ebuild "$repo/$PACKAGE/$pf.ebuild" clean prepare >&2
build=/var/tmp/portage/${cpv%/*}/$pf
S=$(sed -n 's/^declare -[-x] S="\(.*\)"$/\1/p' "$build/temp/environment" | head -n1)
if [ -z "$S" ] || [ ! -d "$S" ]; then
    set -- "$build"/work/*/
    [ $# -eq 1 ] && S=${1%/} || S=$build/work
fi
tar -C "$S" -cf /out/source.tar .
chown "$HOST_UID:$HOST_GID" /out/source.tar
echo "$cpv"
"""


class CustomizeError(ValueError):
    pass


@dataclasses.dataclass
class Draft:
    package: str
    version: str   # the category/package-version the patch is for
    summary: str
    patch: str
    cost: float


def prepare(image: str, shared: Shared, package: str, into: Path) -> tuple[str, Path, Path]:
    """The package's prepared source as a/ and b/ under `into`: (its version, a, b)."""
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    result = subprocess.run(
        ["docker", "run", "--rm", *shared.args(), "-v", f"{into}:/out", "-e", f"PACKAGE={package}",
         "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}", image, "bash", "-lc", PREPARE],
        text=True, capture_output=True, timeout=TIMEOUT)
    if result.returncode != 0:
        raise CustomizeError(f"Portage couldn't prepare {package}'s source:\n{result.stderr[-2000:]}")
    version = result.stdout.strip().splitlines()[-1]
    a, b = into / "a", into / "b"
    for d in (a, b):
        d.mkdir()
        with tarfile.open(into / "source.tar") as tar:
            tar.extractall(d, filter="data")  # (no paths out of d, no device files)
    (into / "source.tar").unlink()
    return version, a, b


def ask_claude(instruction: str, source: Path) -> tuple[str, float]:
    """Claude's edit of the source in `source`: (its summary, the cost in USD)."""
    prompt = (f"The owner's instruction:\n\n{instruction.strip()}\n\n"
              f"The program's source is in the directory {source}. Make the change there.\n")
    command = ["claude", "-p", "--tools", "Read,Grep,Glob,Edit", "--permission-mode", "acceptEdits",
               "--add-dir", str(source), "--strict-mcp-config", "--no-session-persistence",
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM]
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=empty, timeout=TIMEOUT)
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CustomizeError(f"Claude Code didn't answer: {(result.stderr or result.stdout)[-800:]}") from None
    answer = reply.get("structured_output") or {}
    return str(answer.get("summary") or reply.get("result", "")), float(reply.get("total_cost_usd", 0) or 0)


def make_patch(a: Path, b: Path) -> str:
    """The unified diff from a/ to b/ (paths a/... and b/..., applied with -p1), checked."""
    result = subprocess.run(["diff", "-ruN", "--no-dereference", a.name, b.name], cwd=a.parent,
                            text=True, capture_output=True, errors="replace")
    if result.returncode == 0:
        raise CustomizeError("Claude changed nothing in the source")
    if result.returncode != 1:
        raise CustomizeError(f"diff failed: {result.stderr[-500:]}")
    return check_patch(result.stdout)


def check_patch(text: str) -> str:
    problems = []
    if len(text.encode()) > MAX_PATCH:
        problems.append(f"it is {len(text.encode())} bytes (at most {MAX_PATCH})")
    if any(line.startswith("Binary files ") for line in text.splitlines()):
        problems.append("it changes binary files")
    for line in text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t")[0]
            if ".." in Path(path).parts or path.startswith("/"):
                problems.append(f"it names {path!r}")
    problems += fixes_.Fix(step="x", kind="patch", package="x/x", values=[CUSTOM_PATCH], why="", by="claude",
                           patch=text).problems()
    if problems:
        raise CustomizeError("the change was refused: " + "; ".join(problems))
    return text


def draft(r: Recipe, image: str, shared: Shared, package: str, instruction: str, work: Path) -> Draft:
    """Prepare the source, let Claude change it, and make the patch (nothing is kept yet)."""
    version, a, b = prepare(image, shared, package, work)
    summary, cost = ask_claude(instruction, b)
    patch = make_patch(a, b)
    return Draft(package, version, summary, patch, cost)


def keep(recipe_path: Path, package: str, instruction: str, check: str, patch: str) -> Recipe:
    """The change kept: its patch beside the recipe and its [[customizations]] entry in it (the
    recipe is written again, as the form saves it). Returns the recipe as loaded after."""
    check_patch(patch)
    r = load(recipe_path)
    patch_file = fixes_.paths(recipe_path)[1] / package / CUSTOM_PATCH
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch)
    others = [c for c in r.customizations if c.package != package]
    r = dataclasses.replace(r, customizations=[*others, Customization(package, instruction.strip(), check.strip())])
    recipe_path.write_text(dump(r))
    return load(recipe_path)


def remove(recipe_path: Path, package: str) -> None:
    """A customization taken out of the recipe, with its patch."""
    r = load(recipe_path)
    r = dataclasses.replace(r, customizations=[c for c in r.customizations if c.package != package])
    recipe_path.write_text(dump(r))
    (fixes_.paths(recipe_path)[1] / package / CUSTOM_PATCH).unlink(missing_ok=True)


def _step_packages(r: Recipe) -> list[str]:
    from . import overlay
    return overlay.packages(r)


# Which program does a wish belong to? Claude answers from the list of the system's packages.
WHICH_SYSTEM = """You help the owner of a Linux distribution (built with lfsbuilder on Gentoo) find where a change they want belongs.

You get their wish, the desktop the distribution has, and the list of every package installed in it (category/name). Answer:
- kind "setting": the wish is a setting the program already has (a GSettings/dconf key for GNOME, a KDE config key, an Xfce xfconf property, a config file): no source change is needed. Give the setting as precisely as you can in `setting` (for GSettings: schema, key and value, like "org.gnome.desktop.interface clock-show-seconds true").
- kind "extension": an add-on or theme the desktop supports does it, better than changing the source.
- kind "source": it needs a change to a program's source code. Give that program's package in `package`, exactly as it appears in the list.
- kind "unknown": you can't tell.
Always name in `package` the package (from the list) the wish is about, when there is one, even for a setting. `explanation`: two or three plain sentences for someone who isn't a programmer: what the thing is, where it lives, and what you suggest."""

WHICH_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["setting", "extension", "source", "unknown"]},
        "package": {"type": "string"},
        "setting": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["kind", "package", "setting", "explanation"],
}


def system_packages(image: str) -> list[str]:
    """Every package installed in the checkpoint `image` (category/name, sorted), from Portage's database."""
    result = subprocess.run(["docker", "run", "--rm", image, "sh", "-c", "cd /var/db/pkg && ls -d */*"],
                            text=True, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise CustomizeError(f"couldn't list the system's packages: {result.stderr[-300:]}")
    names = set()
    for entry in result.stdout.split():
        category, _, pf = entry.partition("/")
        m = _VERSIONED.match(pf)
        if m:
            names.add(f"{category}/{m['name']}")
    return sorted(names)


# name-version[-rN], and a binary package's build number after it (Portage's binpkg-multi-instance: gdm-49.4-5).
_VERSIONED = __import__("re").compile(r"^(?P<name>.+?)-[0-9][^-]*(-r[0-9]+)?(-[0-9]+)?$")


def unversioned(cpv: str) -> str:
    """"gnome-base/gnome-shell-49.2-r1" -> "gnome-base/gnome-shell"."""
    category, _, pf = cpv.partition("/")
    m = _VERSIONED.match(pf)
    return f"{category}/{m['name']}" if m else cpv


def which_program(wish: str, desktop: str, packages: list[str]) -> dict:
    """Claude's answer to where the wish belongs: {kind, package, setting, explanation, cost}."""
    prompt = (f"The owner's wish:\n\n{wish.strip()}\n\nThe desktop: {desktop or 'none (a text console)'}\n\n"
              f"The installed packages ({len(packages)}):\n" + "\n".join(packages) + "\n")
    command = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--output-format", "json", "--json-schema", json.dumps(WHICH_SCHEMA), "--system-prompt", WHICH_SYSTEM]
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=empty, timeout=600)
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CustomizeError(f"Claude Code didn't answer: {(result.stderr or result.stdout)[-500:]}") from None
    answer = dict(reply.get("structured_output") or {})
    if answer.get("package") and answer["package"] not in packages:
        answer["explanation"] = (answer.get("explanation", "") +
                                 f" (Note: {answer['package']} isn't installed in this system.)").strip()
        answer["package"] = ""
    answer["cost"] = float(reply.get("total_cost_usd", 0) or 0)
    return answer
