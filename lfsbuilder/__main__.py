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
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

from . import binhost, branding, customize, logo, check as checks, clones, estimate, fixes, overlay, gentoo, image, recipe as recipes, repair, runner, steps

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
GENTOO = WORK / "gentoo"  # keys/, stage3/, repos/: what recipes pin, kept
TOOLS_IMAGE = "lfsbuilder-host:dev"
BINHOST = WORK / "binhost"  # gnupg/: the package signing key (private: back it up); www/: what serve serves
TIMES = WORK / "step-times.json"  # how long each step took (estimate.py)
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


def draw_pictures(r: recipes.Recipe, again: bool = False) -> None:
    """The distribution's pictures, from its drawings and label (branding.py), drawn when they are
    new or out of date: the steps carry them, so they must exist before anything is planned."""
    if again or not branding.current(r):
        out = branding.render(tools(), r, WORK / "branding-render")
        print(f"drew the pictures of {r.label_text} ({r.branding}) into {out.relative_to(ROOT)}; "
              "they belong in git beside the drawings", flush=True)


def customize_package(r: recipes.Recipe, package: str, instruction: str, check: str, keep: bool) -> int:
    """A change to the package's source written by Claude (customize.py): the patch printed, and with
    `keep` saved beside the recipe with its [[customizations]] entry; without, only as a draft."""
    p = planned(r)
    if p is None:
        return 1
    if runner.run(p[:1], gentoo.stage3_image(stage3_path(r), stage3_image(r)), shared(r), WORK / f"build-{r.stem}" / "logs", quiet=True):
        return 1
    work = WORK / "customize" / r.stem / package.replace("/", "_")
    print(f"preparing {package}'s source (the version the build compiles), then asking Claude", flush=True)
    try:
        d = customize.draft(r, p[0].image, shared(r), package, instruction, work)
    except (customize.CustomizeError, subprocess.TimeoutExpired) as error:
        print(f"no change: {error}", file=sys.stderr)
        return 1
    draft = work.with_suffix(".patch")
    draft.write_text(d.patch)
    print(f"source: {d.version}", flush=True)
    print(f"Claude changed: {d.summary} (${d.cost:.2f})", flush=True)
    print(f"draft: {draft.relative_to(ROOT)}", flush=True)
    print("----- the change -----")
    print(d.patch, end="" if d.patch.endswith("\n") else "\n")
    print("----- end of the change -----", flush=True)
    shutil.rmtree(work, ignore_errors=True)  # (the source copies: Firefox's are gigabytes)
    if keep:
        customize.keep(r.path, package, instruction, check, d.patch)
        print(f"kept: {package} is compiled with it from the next build on", flush=True)
    return 0


def system_package_list(r: recipes.Recipe) -> list[str] | None:
    """Every package the recipe's system has (category/name): from its last checkpoint when it is
    built; else what Check says would be installed, with what the starter system has. Kept in
    work/customize/<recipe>/packages-<fingerprint>.txt (Check takes minutes)."""
    p = planned(r)
    if p is None:
        return None
    cache = WORK / "customize" / r.stem / f"packages-{p[-1].fingerprint[:16]}.txt"
    if cache.is_file():
        return cache.read_text().split()
    if runner.image_exists(p[-1].image):
        names = customize.system_packages(p[-1].image)
    else:
        print("the recipe isn't fully built: asking Portage what it installs (2–3 min)", flush=True)
        if runner.run(p[:1], gentoo.stage3_image(stage3_path(r), stage3_image(r)), shared(r), WORK / f"build-{r.stem}" / "logs", quiet=True):
            return None
        result = checks.check(r, p[0].image, shared(r))
        names = sorted(set(customize.system_packages(p[0].image)) |
                       {customize.unversioned(c) for c in [*result.binary, *result.compiled]})
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(names) + "\n")
    return names


def which_program(r: recipes.Recipe, wish: str) -> int:
    """Where a wish belongs (a setting, an extension, or a program's source), asked of Claude with
    the system's package list."""
    names = system_package_list(r)
    if names is None:
        return 1
    desktop = next((d for d, s in (("GNOME", "gdm"), ("KDE Plasma", "sddm"), ("Xfce", "lightdm")) if s in r.services), "")
    print(f"asking Claude where it belongs ({len(names)} packages in the system)", flush=True)
    try:
        answer = customize.which_program(wish, desktop, names)
    except (customize.CustomizeError, subprocess.TimeoutExpired) as error:
        print(f"no answer: {error}", file=sys.stderr)
        return 1
    print(f"kind: {answer.get('kind', 'unknown')}")
    print(f"package: {answer.get('package', '')}")
    print(f"setting: {answer.get('setting', '')}")
    for part in ("setting_kind", "schema", "file", "group", "key", "value"):
        print(f"{part}: {answer.get(part, '')}")
    print(f"explanation: {answer.get('explanation', '')} (${answer['cost']:.2f})", flush=True)
    return 0


def draw_logo(name: str, description: str, label: str, replace: bool) -> int:
    """A logo drawn by Claude from the description (logo.py), written to branding/<name>/, and its
    pictures drawn with the label, to look at before choosing it."""
    try:
        if (branding.BRANDING_ROOT / name).exists() and not replace:
            raise logo.LogoError(f"branding/{name}/ is there already (choose another name, or replace it)")
        print(f"asking Claude for the logo ({description.strip()[:80]})", flush=True)
        drawing, light, summary, cost = logo.draw(name, description)
        folder = logo.write(name, drawing, light, replace=replace)
    except (logo.LogoError, subprocess.TimeoutExpired) as error:
        print(f"no logo: {error}", file=sys.stderr)
        return 1
    print(f"Claude drew: {summary} (${cost:.2f})", flush=True)
    look = types.SimpleNamespace(branding=name, label=label, label_text=label)
    out = branding.render(tools(), look, WORK / "branding-render")
    print(f"wrote {folder.relative_to(ROOT)}/logo.svg and logo-light.svg", flush=True)
    print(f"pictures: {(out / branding.BACKGROUND).relative_to(ROOT)}", flush=True)
    print(f"icon: {(out / 'argo-256.png').relative_to(ROOT)}", flush=True)
    return 0


def planned(r: recipes.Recipe) -> list[runner.Planned] | None:
    archive, tree = stage3_path(r), repository_path(r)
    if not archive.exists() or not tree.exists():
        print(f"the stage3 and repository aren't fetched yet: python3 -m lfsbuilder prepare {r.path}", file=sys.stderr)
        return None
    seed = f"stage3 {gentoo.sha512(archive)}\nrepository {r.repository}"
    return runner.plan(steps.all_steps(r), seed)


def build_estimate(r: recipes.Recipe, p: list[runner.Planned]) -> estimate.Estimate:
    return estimate.estimate(p, estimate.load(TIMES), estimate.built_images())


DISPLAY_MANAGERS = ("gdm", "sddm", "lightdm")


def screen_problem(r: recipes.Recipe, screenshot: Path) -> str | None:
    """With a display manager, the boot test's screen must be its login, not a text console (the
    serial login comes either way: GDM failing over and over, say, behind it)."""
    manager = next((s for s in r.services if s in DISPLAY_MANAGERS), None)
    if manager and image.black_share(screenshot.read_bytes()) > image.TEXT_CONSOLE:
        return (f"but the screen is a text console, not {manager}'s login ({screenshot.relative_to(ROOT)}); "
                f"`journalctl -b -u {manager}` in the system says why")
    return None


LOGIN_PASSWORD = "LFSBUILDER_LOGIN_PASSWORD"  # the login test's password, if not asked for


def login_for(r: recipes.Recipe, user: str | None) -> image.Login | None:
    """The login test's user and password (asked for: a recipe holds hashes only), or None
    without --login. Exits if the recipe has no display manager or no such user."""
    if user is None:
        return None
    manager = next((s for s in r.services if s in DISPLAY_MANAGERS), None)
    if manager is None:
        raise SystemExit("--login needs a recipe with a display manager (gdm, sddm or lightdm)")
    if user not in [a.name for a in r.accounts]:
        raise SystemExit(f"--login: {user!r} isn't one of the recipe's users.accounts")
    password = os.environ.get(LOGIN_PASSWORD) or getpass.getpass(f"{user}'s password (for the login test): ")
    try:
        image.keys_for(password)
    except ValueError as error:
        raise SystemExit(f"--login: {error}")
    return image.Login(manager, user, password)


def login_passed(login: image.Login | None, screenshot: Path) -> str:
    """What the boot test showed beyond the greeter, for its message."""
    if login is None:
        return ""
    session = screenshot.with_name(screenshot.stem + "-session.png")
    return f"; {login.user} logged in at the greeter and has a desktop session (the screen in {session.relative_to(ROOT)})"


def plan(r: recipes.Recipe) -> int:
    p = planned(r)
    if p is None:
        return 1
    e = build_estimate(r, p)
    for step, seconds, how in e.steps:
        state = ("done" if how == "done" else "no estimate" if seconds is None
                 else f"~{estimate.duration(seconds)}{'?' if how == 'guess' else ''}")
        print(f"{step.number:>3}  {step.step.title[:56]:56}  {state}")
    print(f"\n{e.text()}")
    return 0


def shared(r: recipes.Recipe) -> runner.Shared:
    # Binary packages are kept apart for each CPU level and compiler flags:
    # a package built for one would be wrong for another.
    # The argo repository is written from the recipe each time (overlay.write: at once, and the same
    # recipe writes the same files); the steps' fingerprints hold its digests.
    tree = overlay.write(r, WORK / "overlay" / r.stem)
    return runner.Shared(repository_path(r), "lfsbuilder-distfiles", f"lfsbuilder-binpkgs-{r.binary_set}", r.jobs,
                         f"lfsbuilder-binhost-{r.binary_set}", tree)


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
        print(f"estimate: {build_estimate(r, p).text()}\n", flush=True)
        failed = runner.run(p, base, shared(r), logs, times=TIMES)
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


def repository_clones(r: recipes.Recipe, out: Path) -> dict[str, Path] | None:
    """The checked clones of the published repositories for the system (clones.py); None: refused."""
    try:
        repos = clones.clones(tools(), r, shared(r).overlay, GENTOO / "keys" / "gnupg", BINHOST / "www", out / "repos")
    except clones.CloneError as error:
        print(error, file=sys.stderr)
        return None
    if repos:
        print("the system gets the published gentoo and argo repositories (checked), so its first sync is a fetch", flush=True)
    return repos


def iso(r: recipes.Recipe, login: image.Login | None = None) -> int:
    p = planned(r)
    if p is None:
        return 1
    missing = [s.step.name for s in p if not runner.image_exists(s.image)]
    if missing:
        print(f"the build isn't finished ({', '.join(missing)} still to do): python3 -m lfsbuilder build {r.path}", file=sys.stderr)
        return 1
    out = WORK / f"build-{r.stem}"
    repos = repository_clones(r, out)
    if repos is None:
        return 1
    print(f"exporting {p[-1].image} as a live system", flush=True)
    kernel, _ = image.export(p[-1].image, r, out / "rootfs.tar", out / "boot", live=True, repos=repos)
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
            if problem := screen_problem(r, screenshot):
                print(f"{firmware.upper()}: it boots, {problem}", file=sys.stderr)
                return 1
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
    if problem := image.root_problem(*image.ext4_root(tools(), installed, image.root_partition_offset(installed))):
        print(f"the installed system is wrong: {problem} (installed.img is kept, to look into)", file=sys.stderr)
        return 1
    print("the installed system's / is root's", flush=True)
    for firmware in firmwares:
        log, screenshot = out / "logs" / f"boot-installed-{firmware}.log", out / f"screen-installed-{firmware}.png"
        try:
            booted = image.boot_test(None, None, installed, log, screenshot, uefi=firmware == "uefi", login=login)
        except image.LoginFailed as error:
            print(f"the installed disk boots with {firmware.upper()}, but {error}", file=sys.stderr)
            return 1  # (installed.img is kept, to look into)
        if booted:
            if problem := screen_problem(r, screenshot):
                print(f"the installed disk boots with {firmware.upper()}, {problem}", file=sys.stderr)
                return 1  # (installed.img is kept, to look into)
            print(f"the installed disk boots with {firmware.upper()} (its own GRUB), the login prompt came"
                  f"{login_passed(login, screenshot)}", flush=True)
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
        ours, gentoos = binhost.publish(tools(), r, p[0].image, shared(r).binpkgs, shared(r).binhost_cache,
                                        BINHOST / "gnupg", BINHOST / "www")
        print(f"published {ours} packages built here (signed with our key) and {gentoos} of Gentoo's (with Gentoo's "
              f"signatures) in {(BINHOST / 'www' / r.binary_set).relative_to(ROOT)}; "
              f"systems of {r.path.name} fetch them from {r.binhost}{r.binary_set}/", flush=True)
        binhost.publish_repository(tools(), r, GENTOO / "keys" / "gnupg", BINHOST / "www")
        commit = binhost.publish_overlay(tools(), r, shared(r).overlay, BINHOST / "gnupg", BINHOST / "www")
        print(f"published the argo repository ({r.version}) as the branch {r.stem} of "
              f"{(BINHOST / 'www' / binhost.OVERLAY).relative_to(ROOT)} (commit {commit[:12]}, signed)", flush=True)
        print(f"published the repository at {r.repository[:12]} as the branch {r.stem} of "
              f"{(BINHOST / 'www' / binhost.REPOSITORY).relative_to(ROOT)}; they sync {r.binhost}{binhost.REPOSITORY}")
    except (binhost.BinhostError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def boot(r: recipes.Recipe, console: bool = False, login: image.Login | None = None) -> int:
    p = planned(r)
    if p is None:
        return 1
    missing = [s.step.name for s in p if not runner.image_exists(s.image)]
    if missing:
        print(f"the build isn't finished ({', '.join(missing)} still to do): python3 -m lfsbuilder build {r.path}", file=sys.stderr)
        return 1
    out = WORK / f"build-{r.stem}"
    repos = repository_clones(r, out)
    if repos is None:
        return 1
    print(f"exporting {p[-1].image}", flush=True)
    kernel, initramfs = image.export(p[-1].image, r, out / "rootfs.tar", out / "boot", repos=repos)
    print(f"making {(out / 'disk.img').relative_to(ROOT)} ({r.output.get('size_gb', 12)} GB, sparse)", flush=True)
    image.disk(tools(), out / "rootfs.tar", out / "disk.img", int(r.output.get("size_gb", 12)))
    (out / "rootfs.tar").unlink()  # the disk image holds the same, and it is gigabytes
    if console:
        print(f"starting it in QEMU with {kernel.name}; Ctrl-A X quits", flush=True)
        image.console(kernel, initramfs, out / "disk.img")
    print(f"booting it in QEMU with {kernel.name}", flush=True)
    log = out / "logs" / "boot.log"
    screenshot = out / "screen.png"
    try:
        booted = image.boot_test(kernel, initramfs, out / "disk.img", log, screenshot, login=login)
    except image.LoginFailed as error:
        print(f"it boots, but {error}", file=sys.stderr)
        return 1
    if booted:
        if problem := screen_problem(r, screenshot):
            print(f"it boots, {problem}", file=sys.stderr)
            return 1
        print(f"it boots: the login prompt came (console in {log.relative_to(ROOT)}, the screen in {screenshot.relative_to(ROOT)})"
              f"{login_passed(login, screenshot)}")
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
    draw = sub.add_parser("draw-logo", help="a logo drawn by Claude from a description, into branding/NAME/")
    draw.add_argument("name", help="the logo's folder name (lower-case letters, digits, _ and -)")
    draw.add_argument("description", help="what the logo should show")
    draw.add_argument("--label", default="Argo Linux", help="the words beside the logo in the pictures")
    draw.add_argument("--replace", action="store_true", help="replace branding/NAME/ if it is there")
    for name in ("prepare", "plan", "check", "build", "boot", "iso", "publish", "branding", "customize", "packages",
                 "which-program"):
        command = sub.add_parser(name)
        command.add_argument("recipe", type=Path)
        if name == "boot":
            command.add_argument("--console", action="store_true", help="stay in the system's console to log in (Ctrl-A X quits)")
        if name == "which-program":
            command.add_argument("wish", help="what you want changed, in words")
        if name == "customize":
            command.add_argument("package", help="the package to change (one of the recipe's)")
            command.add_argument("instruction", help="what to change, in words")
            command.add_argument("--check", default="", help="a shell command run after it is built; the step fails if it does")
            command.add_argument("--keep", action="store_true", help="save the change beside the recipe (without: a draft only)")
        if name in ("boot", "iso"):
            command.add_argument("--login", metavar="USER",
                                 help=f"log in as USER at the display manager's greeter and check a desktop session starts "
                                      f"(the password is asked for, or taken from ${LOGIN_PASSWORD}); iso: on the installed disk")
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
    if args.command == "draw-logo":
        return draw_logo(args.name, args.description, args.label, args.replace)
    if args.command == "serve":
        (BINHOST / "www").mkdir(parents=True, exist_ok=True)
        binhost.serve(BINHOST / "www", args.bind, args.port)
        return 0
    try:
        r = recipes.load(args.recipe)
    except recipes.RecipeError as error:
        print(error, file=sys.stderr)
        return 2
    if args.command == "branding":
        draw_pictures(r, again=True)
        return 0
    draw_pictures(r)
    if args.command == "packages":
        names = system_package_list(r)
        if names is None:
            return 1
        print("\n".join(names))
        return 0
    if args.command == "which-program":
        return which_program(r, args.wish)
    if args.command == "customize":
        return customize_package(r, args.package, args.instruction, args.check, args.keep)
    if args.command == "prepare":
        return prepare(r)
    if args.command == "plan":
        return plan(r)
    if args.command == "check":
        return check(r)
    if args.command == "iso":
        return iso(r, login_for(r, args.login))
    if args.command == "publish":
        return publish(r)
    if args.command == "boot":
        return boot(r, args.console, login_for(r, args.login))
    return build(r, args.until, args.repair)


if __name__ == "__main__":
    sys.exit(main())
