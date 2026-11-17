"""The recipe: one TOML file that says what the system is to be.

The build reads nothing else about the user's wishes, so a recipe (with the
lock file a build writes next to it) is the whole description of a system.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path


class RecipeError(ValueError):
    pass


@dataclasses.dataclass
class Recipe:
    path: Path
    name: str
    version: str
    book: str
    init: str
    jobs: int
    run_tests: bool
    hostname: str
    domain: str
    locale: str
    timezone: str
    keymap: str
    paper_size: str
    network: dict
    output: dict

    def values(self) -> dict[str, str]:
        """What the book's own placeholders (<replaceable>) become, by their text."""
        language, _, charmap = self.locale.partition(".")
        return {
            "<lfs>": self.hostname,
            "<FQDN>": f"{self.hostname}.{self.domain}",
            "<Your Domain Name>": self.domain,
            "<ll>_<CC>.<charmap><@modifiers>": self.locale,
            "<xxx>": self.timezone,
            "<paper_size>": self.paper_size,
            "<your name here>": self.name,
        }


_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_LOCALE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}(\.[A-Za-z0-9-]+)?(@[a-z]+)?$")
_ZONE = re.compile(r"^[A-Za-z]+(/[A-Za-z0-9_+-]+){0,2}$|^UTC$")


def load(path: Path) -> Recipe:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"{path}: {error}") from error

    def section(name: str) -> dict:
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise RecipeError(f"[{name}] must be a table")
        return value

    dist, base, system, network, output = (section(n) for n in ("distribution", "base", "system", "network", "output"))
    recipe = Recipe(
        path=path,
        name=str(dist.get("name", "LFS")),
        version=str(dist.get("version", "0.1")),
        book=str(base.get("book", "")),
        init=str(base.get("init", "systemd")),
        jobs=int(base.get("jobs", 0)),
        run_tests=bool(base.get("run_tests", False)),
        hostname=str(system.get("hostname", "lfs")),
        domain=str(system.get("domain", "localdomain")),
        locale=str(system.get("locale", "en_US.UTF-8")),
        timezone=str(system.get("timezone", "UTC")),
        keymap=str(system.get("keymap", "us")),
        paper_size=str(system.get("paper_size", "A4")),
        network=dict(network) or {"mode": "dhcp"},
        output=dict(output) or {"format": "qemu"},
    )
    problems = []
    if not re.fullmatch(r"\d+\.\d+(-rc\d+)?", recipe.book):
        problems.append(f"base.book must be a book version like \"13.1\", not {recipe.book!r}")
    if recipe.init not in ("systemd", "sysv"):
        problems.append(f"base.init must be \"systemd\" or \"sysv\", not {recipe.init!r}")
    if recipe.jobs < 0:
        problems.append("base.jobs can't be below 0")
    if not _HOSTNAME.match(recipe.hostname):
        problems.append(f"system.hostname {recipe.hostname!r} isn't a host name (lower-case letters, digits and -)")
    if not _LOCALE.match(recipe.locale):
        problems.append(f"system.locale {recipe.locale!r} isn't a locale like en_US.UTF-8")
    if not _ZONE.match(recipe.timezone):
        problems.append(f"system.timezone {recipe.timezone!r} isn't a time zone like Europe/Athens")
    if recipe.paper_size not in ("A4", "letter"):
        problems.append("system.paper_size must be \"A4\" or \"letter\"")
    if recipe.network.get("mode") not in ("dhcp", "static"):
        problems.append("network.mode must be \"dhcp\" or \"static\"")
    if recipe.output.get("format") not in ("qemu",):
        problems.append("output.format can only be \"qemu\" for now")
    if problems:
        raise RecipeError(f"{path}:\n  " + "\n  ".join(problems))
    return recipe
