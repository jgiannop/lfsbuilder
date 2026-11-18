"""lfsbuilder's command line.

    python3 -m lfsbuilder prepare RECIPE   fetch and check the stage3 and repository the recipe pins
    python3 -m lfsbuilder plan RECIPE      the steps the build would run, and which are done
    python3 -m lfsbuilder check RECIPE     ask Portage first: USE changes the recipe needs, binary vs compiled
    python3 -m lfsbuilder build RECIPE     run them in Docker, a checkpoint after each
                     build --repair RECIPE   ... and when a step fails, find a fix (check, then Claude) and retry
    python3 -m lfsbuilder boot RECIPE      export the built system to a disk image and boot it in QEMU
                     boot --console RECIPE   ... and stay in its console to log in (Ctrl-A X quits)
    python3 -m lfsbuilder iso RECIPE       a live ISO of the built system (BIOS and UEFI), booted in QEMU
    python3 -m lfsbuilder password         a password hash for the recipe's [users]
    python3 -m lfsbuilder signing-key      the distribution's package signing key (once), for [updates]
    python3 -m lfsbuilder publish RECIPE   the packages its builds compiled, signed, for the package server
    python3 -m lfsbuilder serve            serve the published packages over HTTP (127.0.0.1:8080; QEMU: 10.0.2.2)
    python3 -m lfsbuilder clean [--yes]    remove checkpoints no recipe in recipes/ uses (lists them without --yes)
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from pathlib import Path

from . import binhost, check as checks, fixes, gentoo, image, recipe as recipes, repair, runner, steps

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
GENTOO = WORK / "gentoo"  # keys/, stage3/, repos/: what recipes pin, kept
TOOLS_IMAGE = "lfsbuilder-host:dev"
BINHOST = WORK / "binhost"  # gnupg/: the package signing key (private: back it up); www/: what serve serves
PUBLIC_KEY = ROOT / "recipes" / "keys" / "binhost.asc"


def tools() -> gentoo.Tools:
    if subprocess.run(["docker", "image", "inspect", TOOLS_IMAGE], capture_output=True).returncode != 0:
        raise SystemExit(f"the tools image isn't built yet: docker build -t {TOOLS_IMAGE} -f docker/host.Dockerfile docker")
    return gentoo.Tools(TOOLS_IMAGE, WORK)


def stage3_path(r: recipes.Recipe) -> Path:
    return GENTOO / "stage3" / f"stage3-amd64-{r.init}-{r.stage3}.tar.xz"


def repository_path(r: recipes.Recipe) -> Path:
    return GENTOO / "repos" / r.repository


def stage3_image(r: recipes.Recipe) -> str:
    return f"lfsbuilder-stage3:{r.init}-{r.stage3}"


def prepare(r: recipes.Recipe) -> int:
    t = tools()
    try:
        keys = gentoo.keyring(t, GENTOO / "keys")
        archive = gentoo.stage3(t, keys, GENTOO / "stage3", r.stage3, r.init)
        print(f"{archive.name}: signed by Gentoo Release Engineering")
        tree = gentoo.repository(t, keys, GENTOO / "repos", r.repository)
        print(f"the Gentoo repository at {r.repository}: signed by Gentoo's repository mirror, in {tree.relative_to(ROOT)}")
    except gentoo.VerifyError as error:
        print(error, file=sys.stderr)
        return 1
    gentoo.stage3_image(archive, stage3_image(r))
    return 0


def planned(r: recipes.Recipe) -> list[runner.Planned] | None:
    archive, tree = stage3_path(r), repository_path(r)
    if not archive.exists() or not tree.exists():
        print(f"the stage3 and repository aren't fetched yet: python3 -m lfsbuilder prepare {r.path}", file=sys.stderr)
        return None
    seed = f"stage3 {gentoo.sha512(archive)}\nrepository {r.repository}"
    return runner.plan(steps.all_steps(r), seed)


def plan(r: recipes.Recipe) -> int:
    p = planned(r)
    if p is None:
        return 1
    for step in p:
        state = "done" if runner.image_exists(step.image) else ""
        print(f"{step.number:>3}  {step.step.title[:56]:56}  {state}")
    return 0


def shared(r: recipes.Recipe) -> runner.Shared:
    # Binary packages are kept apart for each CPU level and compiler flags:
    # a package built for one would be wrong for another.
    return runner.Shared(repository_path(r), "lfsbuilder-distfiles", f"lfsbuilder-binpkgs-{r.binary_set}", r.jobs)


def check(r: recipes.Recipe) -> int:
    p = planned(r)
    if p is None:
        return 1
    if runner.run(p[:1], gentoo.stage3_image(stage3_path(r), stage3_image(r)), shared(r), WORK / f"build-{r.stem}" / "logs", quiet=True):
        return 1  # (the portage step failed; the runner said why)
    print("asking Portage what the recipe would build ...", flush=True)
    result = checks.check(r, p[0].image, shared(r))
    if result.use:
        print("\nThe recipe needs these USE changes; add them to [use.packages]:\n")
        print(result.toml())
    if result.problem:
        print(f"\nPortage can't build the recipe yet:\n{result.problem}", file=sys.stderr)
        return 1
    print(f"\n{len(result.binary) + len(result.compiled)} packages: {len(result.binary)} as binaries, {len(result.compiled)} compiled")
    if result.compiled:
        print("compiled: " + " ".join(result.compiled))
    return 1 if result.use else 0


REPAIR_ATTEMPTS = 3


def build(r: recipes.Recipe, until: str | None, repair_failures: bool = False) -> int:
    attempts = 0
    while True:
        p = planned(r)
        if p is None:
            return 1
        if until:
            names = [s.step.name for s in p]
            if until not in names:
                print(f"there's no step {until!r} to stop at ({', '.join(names)})", file=sys.stderr)
                return 2
            p = p[: names.index(until) + 1]
        base = gentoo.stage3_image(stage3_path(r), stage3_image(r))
        logs = WORK / f"build-{r.stem}" / "logs"
        failed = runner.run(p, base, shared(r), logs)
        if failed is None:
            return 0
        if not repair_failures:
            return 1
        if attempts == REPAIR_ATTEMPTS:
            print(f"\nstill failing after {REPAIR_ATTEMPTS} repairs; the fixes tried are in {fixes.paths(r.path)[0]}", file=sys.stderr)
            return 1
        attempts += 1
        print(f"\nrepair {attempts}/{REPAIR_ATTEMPTS}: looking for a fix ...", flush=True)
        answer = repair.diagnose(r, failed, logs, p[0].image, shared(r))
        cost = f" (${answer.cost_usd:.2f} of Claude Code use)" if answer.cost_usd else ""
        print(f"{answer.by}: {answer.diagnosis}{cost}")
        have = {(f.step, f.kind, f.package, tuple(f.values)) for f in r.fixes}
        new = [f for f in answer.fixes if (f.step, f.kind, f.package, tuple(f.values)) not in have]
        if not new:
            print("no new fix to try; stopping", file=sys.stderr)
            return 1
        for f in new:
            print(f"  fix for step {f.step}: {f.kind} {f.package} {' '.join(f.values)}  ({f.why})")
        fixes.save(r.path, r.fixes + new)
        r = recipes.load(r.path)
        print(f"saved in {fixes.paths(r.path)[0].relative_to(ROOT) if fixes.paths(r.path)[0].is_relative_to(ROOT) else fixes.paths(r.path)[0]}; trying again\n", flush=True)


def clean(yes: bool) -> int:
    """Removes the checkpoint images no recipe in recipes/ uses now (old
    versions of steps). Without `yes`, only says what it would remove."""
    keep: set[str] = set()
    for path in sorted((ROOT / "recipes").glob("*.toml")):
        if path.name.endswith(".fixes.toml"):
            continue
        try:
            p = planned(recipes.load(path))
        except (recipes.RecipeError, fixes.FixError) as error:
            print(f"{path.name}: {error}; its checkpoints are kept", file=sys.stderr)
            return 1
        if p is None:
            continue
        keep.update(s.image for s in p)
    listed = subprocess.run(["docker", "images", runner.IMAGE, "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"],
                            check=True, text=True, capture_output=True).stdout.split("\n")
    old = [line.split() for line in listed if line and line.split()[0] not in keep]
    if not old:
        print(f"nothing to remove: all {len(keep)} checkpoints are used by a recipe")
        return 0
    for name, size in old:
        print(f"{'removing' if yes else 'would remove'} {name} ({size})")
    if not yes:
        print(f"\n{len(old)} old checkpoints; python3 -m lfsbuilder clean --yes removes them")
        return 0
    subprocess.run(["docker", "rmi", *(name for name, _ in old)], check=True, capture_output=True)
    print(f"removed {len(old)}; Docker frees the space of layers no image uses any more")
    return 0


def iso(r: recipes.Recipe) -> int:
    p = planned(r)
    if p is None:
        return 1
    missing = [s.step.name for s in p if not runner.image_exists(s.image)]
    if missing:
        print(f"the build isn't finished ({', '.join(missing)} still to do): python3 -m lfsbuilder build {r.path}", file=sys.stderr)
        return 1
    out = WORK / f"build-{r.stem}"
    print(f"exporting {p[-1].image} as a live system", flush=True)
    kernel, _ = image.export(p[-1].image, r, out / "rootfs.tar", out / "boot", live=True)
    version = kernel.name.split("-", 1)[1]
    print("making the live initramfs (dracut, dmsquash-live)", flush=True)
    image.live_initramfs(p[-1].image, shared(r).args(), version, out / "boot" / "initramfs-live.img")
    target = out / f"{r.stem}.iso"
    print(f"making {target.relative_to(ROOT)} (SquashFS, GRUB for BIOS and UEFI)", flush=True)
    image.iso(tools(), out / "rootfs.tar", kernel, out / "boot" / "initramfs-live.img", out, target, r)
    (out / "rootfs.tar").unlink()
    print(f"{target.relative_to(ROOT)}: {target.stat().st_size / 2**30:.1f} GB; booting it in QEMU", flush=True)
    firmwares = ["bios", *(["uefi"] if image.OVMF.exists() else [])]
    for firmware in firmwares:
        log, screenshot = out / "logs" / f"boot-iso-{firmware}.log", out / f"screen-iso-{firmware}.png"
        if image.boot_test(None, None, target, log, screenshot, timeout=600, cdrom=True, uefi=firmware == "uefi"):
            print(f"{firmware.upper()}: it boots, the login prompt came (the screen in {screenshot.relative_to(ROOT)})", flush=True)
            continue
        print(f"{firmware.upper()}: no login prompt; the end of the console ({log}):", file=sys.stderr)
        for line in log.read_text(errors="replace").splitlines()[-30:]:
            print(f"  | {line}", file=sys.stderr)
        return 1
    if "uefi" not in firmwares:
        print(f"(UEFI not tested: no {image.OVMF}; the ovmf package provides it)")
    if r.init != "systemd":
        print("(the install test needs systemd's automatic-install service; not run for OpenRC)")
        return 0
    print("installing it on an empty disk in QEMU (lfsbuilder-install, automatically)", flush=True)
    installed, log = out / "installed.img", out / "logs" / "install.log"
    if not image.install_test(kernel, out / "boot" / "initramfs-live.img", target, image.volume_id(r), installed, log):
        print(f"the install didn't finish; the end of its console ({log}):", file=sys.stderr)
        for line in log.read_text(errors="replace").splitlines()[-30:]:
            print(f"  | {line}", file=sys.stderr)
        return 1
    for firmware in firmwares:
        log, screenshot = out / "logs" / f"boot-installed-{firmware}.log", out / f"screen-installed-{firmware}.png"
        if image.boot_test(None, None, installed, log, screenshot, uefi=firmware == "uefi"):
            print(f"the installed disk boots with {firmware.upper()} (its own GRUB), the login prompt came", flush=True)
            continue
        print(f"the installed disk doesn't boot with {firmware.upper()}; the end of its console ({log}):", file=sys.stderr)
        for line in log.read_text(errors="replace").splitlines()[-30:]:
            print(f"  | {line}", file=sys.stderr)
        return 1
    installed.unlink()
    return 0


def password() -> int:
    """Asks for a password twice and prints its SHA-512 crypt hash, made by
    openssl in the tools image; the password goes in on its stdin, never on
    a command line."""
    first = getpass.getpass("password: ")
    if first != getpass.getpass("again: "):
        print("they differ", file=sys.stderr)
        return 1
    if not first:
        print("an empty password would let anyone in; leave the hash empty instead to lock the account", file=sys.stderr)
        return 1
    print(gentoo.password_hash(tools(), first))
    return 0


def signing_key(name: str) -> int:
    try:
        fpr = binhost.signing_key(tools(), BINHOST / "gnupg", PUBLIC_KEY, name)
    except binhost.BinhostError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"made the package signing key {fpr}\n"
          f"  private: {(BINHOST / 'gnupg').relative_to(ROOT)} (no passphrase; never in git; back it up: without it,\n"
          f"           installed systems can't take new packages until they get a new key in an update)\n"
          f"  public:  {PUBLIC_KEY.relative_to(ROOT)}, for a recipe's [updates]:\n\n"
          f"[updates]\nserver = \"http://10.0.2.2:8080/\"   # QEMU's address of this machine; a real server's URL later\n"
          f"key = \"{PUBLIC_KEY.relative_to(ROOT / 'recipes')}\"")
    return 0


def publish(r: recipes.Recipe) -> int:
    p = planned(r)
    if p is None:
        return 1
    if not runner.image_exists(p[0].image):
        print(f"nothing built yet: python3 -m lfsbuilder build {r.path}", file=sys.stderr)
        return 1
    try:
        count = binhost.publish(tools(), r, p[0].image, shared(r).binpkgs, BINHOST / "gnupg", BINHOST / "www")
    except binhost.BinhostError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"published {count} packages, signed, in {(BINHOST / 'www' / r.binary_set).relative_to(ROOT)}; "
          f"systems of {r.path.name} fetch them from {r.binhost}{r.binary_set}/")
    return 0


def boot(r: recipes.Recipe, console: bool = False) -> int:
    p = planned(r)
    if p is None:
        return 1
    missing = [s.step.name for s in p if not runner.image_exists(s.image)]
    if missing:
        print(f"the build isn't finished ({', '.join(missing)} still to do): python3 -m lfsbuilder build {r.path}", file=sys.stderr)
        return 1
    out = WORK / f"build-{r.stem}"
    print(f"exporting {p[-1].image}", flush=True)
    kernel, initramfs = image.export(p[-1].image, r, out / "rootfs.tar", out / "boot")
    print(f"making {(out / 'disk.img').relative_to(ROOT)} ({r.output.get('size_gb', 12)} GB, sparse)", flush=True)
    image.disk(tools(), out / "rootfs.tar", out / "disk.img", int(r.output.get("size_gb", 12)))
    (out / "rootfs.tar").unlink()  # the disk image holds the same, and it is gigabytes
    if console:
        print(f"starting it in QEMU with {kernel.name}; Ctrl-A X quits", flush=True)
        image.console(kernel, initramfs, out / "disk.img")
    print(f"booting it in QEMU with {kernel.name}", flush=True)
    log = out / "logs" / "boot.log"
    screenshot = out / "screen.png"
    if image.boot_test(kernel, initramfs, out / "disk.img", log, screenshot):
        print(f"it boots: the login prompt came (console in {log.relative_to(ROOT)}, the screen in {screenshot.relative_to(ROOT)})")
        return 0
    print(f"no login prompt; the end of the console ({log}):", file=sys.stderr)
    for line in log.read_text(errors="replace").splitlines()[-30:]:
        print(f"  | {line}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lfsbuilder")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("password")
    sub.add_parser("signing-key").add_argument("--name", default="lfsbuilder", help="whose packages, in the key's name (the distribution's)")
    serve = sub.add_parser("serve")
    serve.add_argument("--bind", default="127.0.0.1", help="the address to listen on (0.0.0.0: every network the machine is on)")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("clean").add_argument("--yes", action="store_true", help="remove them (without it, only list)")
    for name in ("prepare", "plan", "check", "build", "boot", "iso", "publish"):
        command = sub.add_parser(name)
        command.add_argument("recipe", type=Path)
        if name == "boot":
            command.add_argument("--console", action="store_true", help="stay in the system's console to log in (Ctrl-A X quits)")
        if name == "build":
            command.add_argument("--until", metavar="STEP", help="stop after this step (its name, as plan shows: portage, world, ...)")
            command.add_argument("--repair", action="store_true",
                                 help=f"when a step fails, look for a fix (check, then Claude Code) and try again, up to {REPAIR_ATTEMPTS} times")
    args = parser.parse_args(argv)
    if args.command == "password":
        return password()
    if args.command == "clean":
        return clean(args.yes)
    if args.command == "signing-key":
        return signing_key(args.name)
    if args.command == "serve":
        (BINHOST / "www").mkdir(parents=True, exist_ok=True)
        binhost.serve(BINHOST / "www", args.bind, args.port)
        return 0
    try:
        r = recipes.load(args.recipe)
    except recipes.RecipeError as error:
        print(error, file=sys.stderr)
        return 2
    if args.command == "prepare":
        return prepare(r)
    if args.command == "plan":
        return plan(r)
    if args.command == "check":
        return check(r)
    if args.command == "iso":
        return iso(r)
    if args.command == "publish":
        return publish(r)
    if args.command == "boot":
        return boot(r, args.console)
    return build(r, args.until, args.repair)


if __name__ == "__main__":
    sys.exit(main())
