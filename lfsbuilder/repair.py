"""When a step fails: find a fix, without and then with Claude.

1. A dependency problem ("Change USE", "no ebuilds built with USE flags")
   goes to `check`, which asks Portage itself; no Claude.
2. Anything else goes to Claude, through Claude Code (`claude -p`) with no
   tools at all: it reads and answers, it can't run anything. It gets the
   step's script, the failing command, the end of the step's log and the end
   of Portage's log for the package (never whole logs: they are gigabytes),
   and the recipe with its password hashes taken out. It must answer in the
   form of SCHEMA, and every fix is checked (fixes.Fix.problems) before it
   is used: only USE flags, ~amd64 keywords, build variables and patches
   exist, and they are applied by Portage inside the build's container.

The fixes found are saved in <recipe>.fixes.toml (fixes.py), so the next
build doesn't need to find them again.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path

from . import check as checks
from . import fixes as fixes_
from .recipe import Recipe, dump
from .runner import MARKER, Planned, Shared, portage_log

STEP_LOG_LINES = 60
PORTAGE_LOG_LINES = 150
MAX_CHARS = 24000  # what is sent of each log, at most
TIMEOUT = 600

DEPENDENCY_SIGNS = ("Change USE:", "no ebuilds built with USE flags", "no ebuilds to satisfy")

SYSTEM = """You repair a failed step of a Gentoo Linux build made by lfsbuilder.
The build runs in Docker containers (no privileges; Portage's namespace sandboxes are off) from a pinned Gentoo stage3 and a pinned commit of the Gentoo repository, with Gentoo's binary packages where they match.
You can't run anything: you read what you are given and answer in the required form.

Give the smallest fix that makes the step succeed, as a list of fixes of these kinds only:
- use: USE flags for one package. values: the flags, e.g. ["gnutls"] or ["-doc"].
- keywords: accept a package's testing version. values: ["~amd64"].
- env: variables for one package's build, e.g. ["MAKEOPTS=-j1"]. No quotes, $ or backticks.
- patch: a unified diff Portage applies to one package's sources (/etc/portage/patches). values: [a file name ending in .patch]; the diff in "patch". Only when you are sure of the source's contents.
"package" is always category/name, without a version.
Say what went wrong in "diagnosis", briefly. If no fix of these kinds can work (a network error, a full disk, a broken repository), give an empty list of fixes and say why in "diagnosis".
Don't repeat a fix the recipe already has: it didn't work."""

SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"enum": list(fixes_.KINDS)},
                    "package": {"type": "string"},
                    "values": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["kind", "package", "values", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["diagnosis", "fixes"],
    "additionalProperties": False,
}


@dataclasses.dataclass
class Answer:
    fixes: list[fixes_.Fix]
    diagnosis: str
    by: str
    cost_usd: float = 0.0


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return "(none)"
    text = "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    return text[-MAX_CHARS:]


def _redacted(r: Recipe) -> str:
    """The recipe as TOML, without its password hashes."""
    return dump(dataclasses.replace(
        r, root_password="(hidden)" if r.root_password else "",
        accounts=[dataclasses.replace(a, password="(hidden)" if a.password else "") for a in r.accounts]))


def prompt(r: Recipe, p: Planned, logs: Path) -> str:
    step_log = logs / p.log_name
    text = step_log.read_text(errors="replace").splitlines() if step_log.exists() else []
    command = next((l.removeprefix(MARKER).strip() for l in reversed(text) if l.startswith(MARKER)), "(unknown)")
    known = "\n".join(f"- step {f.step}: {f.kind} {f.package} {' '.join(f.values)} ({f.why})" for f in r.fixes) or "(none)"
    return f"""Step {p.number} "{p.step.name}" ({p.step.title}) failed.

The command that failed:
{command}

The step's script:
{p.step.script}

The end of the step's log:
{_tail(step_log, STEP_LOG_LINES)}

The end of Portage's build log for the package that failed:
{_tail(portage_log(step_log), PORTAGE_LOG_LINES)}

The recipe (TOML; the build's other steps come from it):
{_redacted(r)}

Fixes the recipe already has:
{known}
"""


def ask_claude(r: Recipe, p: Planned, logs: Path) -> Answer:
    command = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM]
    # Run outside the project, so Claude Code doesn't load the project's own instructions.
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(command, input=prompt(r, p, logs), text=True, capture_output=True,
                                cwd=empty, timeout=TIMEOUT)
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Answer([], f"Claude Code didn't answer: {(result.stderr or result.stdout)[-500:]}", "claude")
    answer = reply.get("structured_output") or {}
    found, refused = [], []
    for raw in answer.get("fixes", []):
        fix = fixes_.Fix(step=p.step.name, kind=raw.get("kind", ""), package=raw.get("package", ""),
                         values=list(raw.get("values", [])), why=raw.get("why", ""), by="claude",
                         patch=raw.get("patch", ""))
        problems = fix.problems()
        (refused if problems else found).append((fix, problems))
    diagnosis = answer.get("diagnosis", reply.get("result", ""))
    if refused:
        diagnosis += "\n(refused: " + "; ".join(f"{f.kind} {f.package}: {', '.join(pr)}" for f, pr in refused) + ")"
    return Answer([f for f, _ in found], diagnosis, "claude", float(reply.get("total_cost_usd", 0) or 0))


def diagnose(r: Recipe, p: Planned, logs: Path, check_image: str, shared: Shared) -> Answer:
    text = (logs / p.log_name).read_text(errors="replace")
    if any(sign in text for sign in DEPENDENCY_SIGNS):
        result = checks.check(r, check_image, shared)
        have = {(f.package, tuple(f.values)) for f in r.fixes if f.kind == "use"}
        found = [fixes_.Fix(step=p.step.name, kind="use", package=package, values=flags,
                            why="Portage asked for it (found by check)", by="check")
                 for package, flags in result.use.items() if (package, tuple(flags)) not in have]
        if found:
            return Answer(found, "a dependency problem: Portage named the USE changes", "check")
    return ask_claude(r, p, logs)
