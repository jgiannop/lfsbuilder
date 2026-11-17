"""The recipe: one TOML file that says what the system is to be.

The build reads nothing else about the user's wishes, so a recipe is the
whole description of a system: it pins a Gentoo stage3 and a commit of the
Gentoo repository, and says how the system is configured and what is on it.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from pathlib import Path


class RecipeError(ValueError):
    pass


@dataclasses.dataclass
class Account:
    name: str
    full_name: str
    groups: list[str]
    password: str  # a crypt(3) hash, never the password itself; empty: locked


@dataclasses.dataclass
class Recipe:
    path: Path
    name: str
    version: str
    init: str
    stage3: str
    repository: str
    profile: str
    cpu: str
    cflags: str
    binary_packages: bool
    kernel: str
    jobs: int
    use: list[str]
    package_use: dict[str, list[str]]
    packages: list[str]
    hostname: str
    domain: str
    locale: str
    timezone: str
    keymap: str
    network: dict
    output: dict
    root_password: str
    sudo: bool
    accounts: list[Account]
    services: list[str]
    fixes: list = dataclasses.field(default_factory=list)  # fixes.Fix, from <name>.fixes.toml beside the recipe

    @property
    def stem(self) -> str:
        return self.path.stem


INITS = ("systemd", "openrc")
CPUS = ("x86-64", "x86-64-v3")
KERNELS = ("gentoo-kernel-bin", "gentoo-kernel")

_STAGE3 = re.compile(r"^\d{8}T\d{6}Z$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ATOM = re.compile(r"^[a-z0-9][a-z0-9+_.-]*/[A-Za-z0-9+_][A-Za-z0-9+_.-]*$")  # category/package
_FLAG = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9+_@.-]*$")
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_CFLAGS = re.compile(r"^[A-Za-z0-9 =_.,+-]*$")  # no quotes or shell characters: it goes into make.conf
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_LOCALE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}(\.[A-Za-z0-9-]+)?(@[a-z]+)?$")
_ZONE = re.compile(r"^[A-Za-z]+(/[A-Za-z0-9_+-]+){0,2}$|^UTC$")
_KEYMAP = re.compile(r"^[A-Za-z0-9_-]+$")
_SERVICE = re.compile(r"^[A-Za-z0-9@._-]+$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FULL_NAME = re.compile(r"^[^:,\n]*$")
_HASH = re.compile(r"^\$(6|5|y|2[aby])\$[./A-Za-z0-9$]{8,}$")  # sha512, sha256, yescrypt, bcrypt


def load(path: Path) -> Recipe:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"{path}: {error}") from error

    def section(name: str, within: dict | None = None) -> dict:
        value = (data if within is None else within).get(name, {})
        if not isinstance(value, dict):
            raise RecipeError(f"[{name}] must be a table")
        return value

    dist, base, use, packages, system, network, output, users, services = (
        section(n) for n in ("distribution", "base", "use", "packages", "system", "network", "output", "users", "services")
    )
    problems: list[str] = []

    def strings(value, where: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"{where} must be a list of strings")
            return []
        return value

    recipe = Recipe(
        path=path,
        name=str(dist.get("name", "Gentoo")),
        version=str(dist.get("version", "0.1")),
        init=str(base.get("init", "systemd")),
        stage3=str(base.get("stage3", "")),
        repository=str(base.get("repository", "")),
        profile=str(base.get("profile", "")),
        cpu=str(base.get("cpu", "x86-64")),
        cflags=str(base.get("cflags", "-O2 -pipe")),
        binary_packages=bool(base.get("binary_packages", True)),
        kernel=str(base.get("kernel", "gentoo-kernel-bin")),
        jobs=int(base.get("jobs", 0)),
        use=strings(use.get("global", []), "use.global"),
        package_use={str(k): strings(v, f"use.packages.{k}") for k, v in section("packages", use).items()},
        packages=strings(packages.get("install", []), "packages.install"),
        hostname=str(system.get("hostname", "gentoo")),
        domain=str(system.get("domain", "localdomain")),
        locale=str(system.get("locale", "en_US.UTF-8")),
        timezone=str(system.get("timezone", "UTC")),
        keymap=str(system.get("keymap", "us")),
        network=dict(network) or {"mode": "dhcp"},
        output=dict(output) or {"format": "qemu"},
        root_password=str(users.get("root_password", "")),
        sudo=bool(users.get("sudo", True)),
        accounts=[],
        services=strings(services.get("enable", []), "services.enable"),
    )
    recipe.network.setdefault("manager", "networkd" if recipe.init == "systemd" else "netifrc")
    raw_accounts = users.get("accounts", [])
    if not isinstance(raw_accounts, list) or not all(isinstance(a, dict) for a in raw_accounts):
        problems.append("users.accounts must be a list of tables ([[users.accounts]])")
        raw_accounts = []
    for a in raw_accounts:
        recipe.accounts.append(Account(
            name=str(a.get("name", "")),
            full_name=str(a.get("full_name", "")),
            groups=strings(a.get("groups", []), f"users.accounts {a.get('name', '')!r} groups"),
            password=str(a.get("password", "")),
        ))
    if not recipe.profile:
        recipe.profile = f"default/linux/amd64/23.0/{recipe.init}" if recipe.init == "systemd" else "default/linux/amd64/23.0"

    if recipe.init not in INITS:
        problems.append(f"base.init must be one of {', '.join(INITS)}, not {recipe.init!r}")
    if not _STAGE3.match(recipe.stage3):
        problems.append(f"base.stage3 must name a stage3 build like \"20261115T170104Z\", not {recipe.stage3!r}")
    if not _COMMIT.match(recipe.repository):
        problems.append(f"base.repository must be a full commit id (40 hex digits), not {recipe.repository!r}")
    if not _PROFILE.match(recipe.profile):
        problems.append(f"base.profile {recipe.profile!r} isn't a profile path")
    if recipe.cpu not in CPUS:
        problems.append(f"base.cpu must be one of {', '.join(CPUS)}, not {recipe.cpu!r}")
    if not _CFLAGS.match(recipe.cflags) or "-march" in recipe.cflags:
        problems.append("base.cflags may hold compiler options only (no quotes; -march comes from base.cpu)")
    if recipe.kernel not in KERNELS:
        problems.append(f"base.kernel must be one of {', '.join(KERNELS)}, not {recipe.kernel!r}")
    if recipe.jobs < 0:
        problems.append("base.jobs can't be below 0")
    for flag in recipe.use:
        if not _FLAG.match(flag):
            problems.append(f"use.global: {flag!r} isn't a USE flag")
    for atom, flags in recipe.package_use.items():
        if not _ATOM.match(atom):
            problems.append(f"use.packages: {atom!r} isn't a package (category/name)")
        for flag in flags:
            if not _FLAG.match(flag):
                problems.append(f"use.packages.{atom}: {flag!r} isn't a USE flag")
    for atom in recipe.packages:
        if not _ATOM.match(atom):
            problems.append(f"packages.install: {atom!r} isn't a package (category/name)")
    if not _HOSTNAME.match(recipe.hostname):
        problems.append(f"system.hostname {recipe.hostname!r} isn't a host name (lower-case letters, digits and -)")
    if not _DOMAIN.match(recipe.domain):
        problems.append(f"system.domain {recipe.domain!r} isn't a domain name")
    if not _LOCALE.match(recipe.locale):
        problems.append(f"system.locale {recipe.locale!r} isn't a locale like en_US.UTF-8")
    if not _ZONE.match(recipe.timezone):
        problems.append(f"system.timezone {recipe.timezone!r} isn't a time zone like Europe/Athens")
    if not _KEYMAP.match(recipe.keymap):
        problems.append(f"system.keymap {recipe.keymap!r} isn't a keymap name")
    if recipe.network.get("mode") not in ("dhcp", "static"):
        problems.append("network.mode must be \"dhcp\" or \"static\"")
    managers = ("networkd", "networkmanager") if recipe.init == "systemd" else ("netifrc", "networkmanager")
    if recipe.network["manager"] not in managers:
        problems.append(f"network.manager must be one of {', '.join(managers)} with {recipe.init}")
    if recipe.network["manager"] == "networkmanager" and recipe.network.get("mode") == "static":
        problems.append("network.mode = \"static\" is written for networkd and netifrc only, so far")
    for service in recipe.services:
        if not _SERVICE.match(service):
            problems.append(f"services.enable: {service!r} isn't a service name")
    if recipe.output.get("format") not in ("qemu",):
        problems.append("output.format can only be \"qemu\" for now")
    if recipe.root_password and not _HASH.match(recipe.root_password):
        problems.append("users.root_password must be a password hash (python3 -m lfsbuilder password), not a password")
    names = [a.name for a in recipe.accounts]
    for a in recipe.accounts:
        if not _USER.match(a.name) or a.name == "root":
            problems.append(f"users.accounts: {a.name!r} isn't a user name (lower-case letters, digits, _ and -; not root)")
        if names.count(a.name) > 1:
            problems.append(f"users.accounts: {a.name!r} is there twice")
        if not _FULL_NAME.match(a.full_name):
            problems.append(f"users.accounts {a.name!r}: full_name can't hold : , or a new line")
        for g in a.groups:
            if not _USER.match(g):
                problems.append(f"users.accounts {a.name!r}: {g!r} isn't a group name")
        if a.password and not _HASH.match(a.password):
            problems.append(f"users.accounts {a.name!r}: password must be a password hash (python3 -m lfsbuilder password), not a password")
    if problems:
        raise RecipeError(f"{path}:\n  " + "\n  ".join(problems))
    from . import fixes  # (fixes doesn't import recipe; this keeps the two apart)
    try:
        recipe.fixes = fixes.load(path)
    except fixes.FixError as error:
        raise RecipeError(str(error)) from error
    return recipe


def _s(value: str) -> str:
    """A TOML string (JSON's escapes are TOML's)."""
    return json.dumps(value, ensure_ascii=False)


def _list(values: list[str]) -> str:
    return "[" + ", ".join(_s(v) for v in values) + "]"


def dump(r: Recipe) -> str:
    """The recipe as TOML, as load() reads it: what the form saves."""
    out = [
        "# A recipe: everything that makes this system this system (written by lfsbuilder).",
        "",
        "[distribution]",
        f"name = {_s(r.name)}",
        f"version = {_s(r.version)}",
        "",
        "[base]",
        f"init = {_s(r.init)}",
        f"stage3 = {_s(r.stage3)}",
        f"repository = {_s(r.repository)}",
        f"profile = {_s(r.profile)}",
        f"cpu = {_s(r.cpu)}",
        f"cflags = {_s(r.cflags)}",
        f"binary_packages = {str(r.binary_packages).lower()}",
        f"kernel = {_s(r.kernel)}",
        f"jobs = {r.jobs}",
        "",
        "[use]",
        f"global = {_list(r.use)}",
        "",
        "[use.packages]",
        *(f"{_s(atom)} = {_list(flags)}" for atom, flags in r.package_use.items()),
        "",
        "[packages]",
        f"install = {_list(r.packages)}",
        "",
        "[system]",
        f"hostname = {_s(r.hostname)}",
        f"domain = {_s(r.domain)}",
        f"locale = {_s(r.locale)}",
        f"timezone = {_s(r.timezone)}",
        f"keymap = {_s(r.keymap)}",
        "",
        "[network]",
        *(f"{key} = {_list(value) if isinstance(value, list) else _s(str(value))}" for key, value in r.network.items()),
        "",
        "[services]",
        f"enable = {_list(r.services)}",
        "",
        "[users]",
        f"root_password = {_s(r.root_password)}",
        f"sudo = {str(r.sudo).lower()}",
    ]
    for a in r.accounts:
        out += ["", "[[users.accounts]]", f"name = {_s(a.name)}", f"full_name = {_s(a.full_name)}",
                f"groups = {_list(a.groups)}", f"password = {_s(a.password)}"]
    out += ["", "[output]"]
    out += [f"{key} = {value if isinstance(value, int) else _s(str(value))}" for key, value in r.output.items()]
    return "\n".join(out) + "\n"
