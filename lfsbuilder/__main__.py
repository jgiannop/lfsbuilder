"""lfsbuilder's command line.

    python3 -m lfsbuilder prepare RECIPE   fetch and check the stage3 and repository the recipe pins
    python3 -m lfsbuilder plan RECIPE      the steps the build would run, and which are done
    python3 -m lfsbuilder build RECIPE     run them in Docker, a checkpoint after each
    python3 -m lfsbuilder boot RECIPE      export the built system to a disk image and boot it in QEMU
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from . import gentoo, image, recipe as recipes, runner, steps

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
GENTOO = WORK / "gentoo"  # keys/, stage3/, repos/: what recipes pin, kept
TOOLS_IMAGE = "lfsbuilder-host:dev"


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
    flags = hashlib.sha256(f"{r.cpu} {r.cflags}".encode()).hexdigest()[:12]
    return runner.Shared(repository_path(r), "lfsbuilder-distfiles", f"lfsbuilder-binpkgs-{r.cpu}-{flags}", r.jobs)


def build(r: recipes.Recipe, until: str | None) -> int:
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
    return runner.run(p, base, shared(r), WORK / f"build-{r.stem}" / "logs")


def boot(r: recipes.Recipe) -> int:
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
    print(f"booting it in QEMU with {kernel.name}", flush=True)
    log = out / "logs" / "boot.log"
    if image.boot_test(kernel, initramfs, out / "disk.img", log):
        print(f"it boots: the login prompt came (console in {log.relative_to(ROOT)})")
        return 0
    print(f"no login prompt; the end of the console ({log}):", file=sys.stderr)
    for line in log.read_text(errors="replace").splitlines()[-30:]:
        print(f"  | {line}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lfsbuilder")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "plan", "build", "boot"):
        command = sub.add_parser(name)
        command.add_argument("recipe", type=Path)
        if name == "build":
            command.add_argument("--until", metavar="STEP", help="stop after this step (its name, as plan shows: portage, world, ...)")
    args = parser.parse_args(argv)
    try:
        r = recipes.load(args.recipe)
    except recipes.RecipeError as error:
        print(error, file=sys.stderr)
        return 2
    if args.command == "prepare":
        return prepare(r)
    if args.command == "plan":
        return plan(r)
    if args.command == "boot":
        return boot(r)
    return build(r, args.until)


if __name__ == "__main__":
    sys.exit(main())
