"""What a recipe would build, asked of Portage before building it.

`emerge --pretend` runs in a container of the recipe's `portage` checkpoint
(make.conf, USE flags, profile already written) with everything the build
would install. When Portage answers that a package needs other USE flags
("Change USE: +gnutls"), the change is added to a scratch package.use in
that throwaway container and Portage is asked again, until it is satisfied
or answers something else. The result: the USE changes the recipe needs
(written as TOML, to paste into [use.packages]), and how many packages come
as binaries and how many would be compiled.

Most of these changes come from Gentoo's binary packages, which were built
with particular USE flags; --autounmask doesn't find them.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess

from .recipe import Recipe
from .runner import Shared
from .steps import all_steps

ROUNDS = 25

# Run in the container: one round, printing Portage's answer between markers.
ROUND = r"""
emerge --pretend --update --deep --newuse {targets} > /tmp/answer 2>&1
echo "=== status $?"
cat /tmp/answer
"""

_CHANGE = re.compile(r"^- (?P<package>[^ ]+?)-(?P<version>\d[^ :]*)::\S+ \(Change USE: (?P<flags>[^)]*)\)")
_LISTED = re.compile(r"^\[(?P<kind>binary|ebuild)[^\]]*\] (?P<package>\S+)")


@dataclasses.dataclass
class Result:
    use: dict[str, list[str]]  # package -> USE flags the recipe needs
    binary: list[str]
    compiled: list[str]
    problem: str  # Portage's answer when it isn't a USE change; empty when all is well

    def toml(self) -> str:
        return "\n".join(f'"{p}" = [{", ".join(f"{chr(34)}{f}{chr(34)}" for f in flags)}]' for p, flags in self.use.items())


def targets(r: Recipe) -> list[str]:
    """What the build installs: @world, the recipe's packages (with those its
    choices add), the kernel."""
    return ["@world", *(s.package for s in all_steps(r) if s.package), f"sys-kernel/{r.kernel}"]


def check(r: Recipe, image: str, shared: Shared) -> Result:
    container = f"lfsbuilder-check-{r.stem}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", container, *shared.args(), image, "sleep", "infinity"],
                   check=True, capture_output=True)
    use: dict[str, list[str]] = {}
    try:
        for _ in range(ROUNDS):
            scratch = "".join(f"{p} {' '.join(f)}\n" for p, f in use.items())
            subprocess.run(["docker", "exec", "-i", container, "sh", "-c", "cat > /etc/portage/package.use/zz-lfsbuilder-check"],
                           input=scratch, text=True, check=True)
            answer = subprocess.run(["docker", "exec", container, "bash", "-lc", ROUND.format(targets=" ".join(targets(r)))],
                                    text=True, capture_output=True).stdout
            lines = answer.splitlines()
            status = int(lines[0].split()[-1]) if lines and lines[0].startswith("=== status") else 1
            change = next((m for m in map(_CHANGE.match, lines) if m), None)
            if change:
                package = change["package"]
                flags = [f.lstrip("+") for f in change["flags"].split()]
                if all(f in use.get(package, []) for f in flags):
                    return Result(use, [], [], "Portage asked twice for the same change:\n" + _problem(lines))
                use.setdefault(package, []).extend(f for f in flags if f not in use.get(package, []))
                continue
            listed = [m for m in map(_LISTED.match, lines) if m]
            binary = [m["package"] for m in listed if m["kind"] == "binary"]
            compiled = [m["package"] for m in listed if m["kind"] == "ebuild"]
            return Result(use, binary, compiled, "" if status == 0 else _problem(lines))
        return Result(use, [], [], f"still asking for changes after {ROUNDS} rounds")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _problem(lines: list[str]) -> str:
    """Portage's complaint, without its progress noise."""
    keep = [l for l in lines[1:] if l.strip() and not l.startswith(("/var/db/repos/gentoo/profiles/updates", "  ", "Performing Global", "(Could take", " * IMPORTANT", " * Use eselect news"))]
    return "\n".join(keep[-25:])
