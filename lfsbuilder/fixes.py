"""Fixes: what was found when a step failed, kept beside the recipe.

`recipes/<name>.fixes.toml` holds them, each with the step it belongs to,
what it changes, why, and who found it (`check` or Claude). A fix is applied
at the start of its step, so the steps before it keep their checkpoints, and
it stays applied for the steps after. The recipe itself is never rewritten:
it stays as its author wrote it, and the fixes can be read, kept or deleted
on their own. A patch's text is a file in `recipes/<name>.patches/`.

Only these kinds exist, and every field is checked before it reaches a
shell script or Portage's configuration:

  use       Portage USE flags for one package          values: flags
  keywords  accept a package's testing version         values: ["~amd64"]
  env       variables for one package's build          values: ["MAKEOPTS=-j1"]
  patch     a patch Portage applies to one package     values: [file name]
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import tomllib
from pathlib import Path

KINDS = ("use", "keywords", "env", "patch")
_ATOM = re.compile(r"^[a-z0-9][a-z0-9+_.-]*/[A-Za-z0-9+_][A-Za-z0-9+_.-]*$")
_FLAG = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9+_@.-]*$")
_KEYWORD = re.compile(r"^~amd64$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*=[A-Za-z0-9 ._,+=/:-]*$")  # no quotes, $, ` or new lines
_PATCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.patch$")
_STEP = re.compile(r"^[A-Za-z0-9_.+-]+$")


class FixError(ValueError):
    pass


@dataclasses.dataclass
class Fix:
    step: str  # the step's name, as `plan` and `build --until` know it
    kind: str
    package: str
    values: list[str]
    why: str
    by: str  # "check" or "claude"
    date: str = ""
    patch: str = ""  # a patch's text (kept in its own file)

    def problems(self) -> list[str]:
        out = []
        if not _STEP.match(self.step):
            out.append(f"{self.step!r} isn't a step name")
        if self.kind not in KINDS:
            out.append(f"kind must be one of {', '.join(KINDS)}, not {self.kind!r}")
        if not _ATOM.match(self.package):
            out.append(f"{self.package!r} isn't a package (category/name)")
        check = {"use": _FLAG, "keywords": _KEYWORD, "env": _ENV, "patch": _PATCH_NAME}.get(self.kind)
        if not self.values or (check and not all(check.match(v) for v in self.values)):
            out.append(f"values {self.values!r} don't fit kind {self.kind!r}")
        if self.kind == "patch":
            if len(self.values) != 1:
                out.append("a patch fix names one file")
            if self.patch and not re.search(r"^(--- |diff )", self.patch, re.M):
                out.append("the patch text isn't a unified diff")
            if "LFSBUILDER_PATCH_EOF" in self.patch:
                out.append("the patch text holds the marker that ends it in the script")
        return out

    @property
    def tag(self) -> str:
        """A name for the files this fix writes in Portage's directories."""
        return "lfsbuilder-" + self.package.replace("/", "_")


def paths(recipe_path: Path) -> tuple[Path, Path]:
    return recipe_path.with_name(recipe_path.stem + ".fixes.toml"), recipe_path.with_name(recipe_path.stem + ".patches")


def load(recipe_path: Path) -> list[Fix]:
    file, patches = paths(recipe_path)
    if not file.exists():
        return []
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise FixError(f"{file}: {error}") from error
    out = []
    for i, raw in enumerate(data.get("fix", []), start=1):
        fix = Fix(step=str(raw.get("step", "")), kind=str(raw.get("kind", "")), package=str(raw.get("package", "")),
                  values=[str(v) for v in raw.get("values", [])], why=str(raw.get("why", "")),
                  by=str(raw.get("by", "")), date=str(raw.get("date", "")))
        if fix.kind == "patch" and fix.values:
            patch_file = patches / fix.package / fix.values[0]
            if not patch_file.exists():
                raise FixError(f"{file}: fix {i}: {patch_file} is missing")
            fix.patch = patch_file.read_text()
        problems = fix.problems()
        if problems:
            raise FixError(f"{file}: fix {i}: " + "; ".join(problems))
        out.append(fix)
    return out


def save(recipe_path: Path, fixes: list[Fix]) -> None:
    file, patches = paths(recipe_path)
    lines = [
        "# Fixes found when a step failed, applied at the start of that step.",
        "# Written by lfsbuilder (`build --repair`); read them, keep them or delete them.",
    ]
    for fix in fixes:
        lines += ["", "[[fix]]",
                  f"step = {json.dumps(fix.step)}", f"kind = {json.dumps(fix.kind)}",
                  f"package = {json.dumps(fix.package)}", f"values = {json.dumps(fix.values)}",
                  f"why = {json.dumps(fix.why, ensure_ascii=False)}", f"by = {json.dumps(fix.by)}",
                  f"date = {json.dumps(fix.date or datetime.date.today().isoformat())}"]
        if fix.kind == "patch":
            target = patches / fix.package / fix.values[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fix.patch)
    file.write_text("\n".join(lines) + "\n")


def script(fixes: list[Fix]) -> str:
    """The shell commands that put fixes into Portage's configuration."""
    out = ""
    for fix in fixes:
        if fix.kind == "use":
            out += f"mkdir -p /etc/portage/package.use\necho '{fix.package} {' '.join(fix.values)}' >> /etc/portage/package.use/{fix.tag}\n"
        elif fix.kind == "keywords":
            out += f"mkdir -p /etc/portage/package.accept_keywords\necho '{fix.package} {' '.join(fix.values)}' >> /etc/portage/package.accept_keywords/{fix.tag}\n"
        elif fix.kind == "env":
            out += f"mkdir -p /etc/portage/env /etc/portage/package.env\n"
            out += "".join(f"echo '{v}' >> /etc/portage/env/{fix.tag}.conf\n" for v in fix.values)
            out += f"echo '{fix.package} {fix.tag}.conf' >> /etc/portage/package.env/{fix.tag}\n"
        elif fix.kind == "patch":
            directory = f"/etc/portage/patches/{fix.package}"
            out += f"mkdir -p {directory}\ncat > {directory}/{fix.values[0]} << 'LFSBUILDER_PATCH_EOF'\n{fix.patch.rstrip()}\nLFSBUILDER_PATCH_EOF\n"
    return out
