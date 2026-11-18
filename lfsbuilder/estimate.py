"""How long a build will take, from how long steps took before.

The runner records every step it finishes in `work/step-times.json`:

  - by fingerprint: the same step again (after its checkpoint was cleaned,
    or on another machine's copy of work/) takes what it took;
  - by step name, the last few times in any recipe: a package step is the
    same package elsewhere (`www-client_firefox`); a general step (`world`,
    `kernel`) is a guess from other recipes, the longest of them.

A step whose checkpoint exists takes nothing. A step never run under its
name anywhere has no estimate; it is listed instead of guessed.

Builds before 2026-11-18 recorded nothing; their times can't be told from
what they left (a log rewritten by a retry keeps the first attempt's
creation time), so estimates start with the next builds.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

KEEP = 5  # times kept per step name


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return {"fingerprints": dict(data.get("fingerprints", {})), "names": dict(data.get("names", {}))}


def _add(data: dict, fingerprint: str, name: str, seconds: float) -> None:
    data["fingerprints"][fingerprint] = round(seconds)
    data["names"][name] = (data["names"].get(name, []) + [round(seconds)])[-KEEP:]


def record(path: Path, fingerprint: str, name: str, seconds: float) -> None:
    data = load(path)
    _add(data, fingerprint, name, seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def built_images(repository: str = "lfsbuilder-build") -> set[str]:
    """Every checkpoint image there is, in one docker call."""
    out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repository],
                         capture_output=True, text=True).stdout
    return set(out.split())


@dataclasses.dataclass
class Estimate:
    seconds: float            # the steps not built yet that have an estimate
    steps: list[tuple]        # (planned, seconds or None, how: "done" / "measured" / "guess" / "")
    unknown: list[str]        # step names with no estimate

    @property
    def todo(self) -> int:
        return sum(1 for _, _, how in self.steps if how != "done")

    def text(self) -> str:
        if not self.todo:
            return "Everything is built."
        parts = [f"{self.todo} of {len(self.steps)} steps to build"]
        if self.seconds or not self.unknown:
            guessed = any(how == "guess" for _, _, how in self.steps)
            parts.append(f"about {duration(self.seconds)}{' (partly guessed from other recipes)' if guessed else ''}")
        if self.unknown:
            parts.append(f"{'plus ' if self.seconds else ''}{len(self.unknown)} with no time recorded, "
                         f"so no estimate: {', '.join(self.unknown)}")
        return ", ".join(parts) + "."


def estimate(planned: list, data: dict, built: set[str]) -> Estimate:
    steps, unknown, total = [], [], 0.0
    for p in planned:
        if p.image in built:
            steps.append((p, 0, "done"))
        elif p.fingerprint in data["fingerprints"]:
            seconds = data["fingerprints"][p.fingerprint]
            steps.append((p, seconds, "measured"))
            total += seconds
        elif data["names"].get(p.step.name):
            seconds = max(data["names"][p.step.name])
            steps.append((p, seconds, "guess"))
            total += seconds
        else:
            steps.append((p, None, ""))
            unknown.append(p.step.name)
    return Estimate(total, steps, unknown)


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02} min"
