"""Runs the book's steps in Docker, with a checkpoint after every one.

Each step runs in a container started from the image the step before it
left, and when it succeeds the container is committed as a new image: the
checkpoint is an image layer, holding only what that step changed. A failure
leaves nothing behind but its log; the next run starts from the last good
image.

Every image is named by a fingerprint of everything that went into it: the
host image, the book, every step's script up to it. So a run finds its way
back by itself (the steps whose images exist are done), and changing a step
changes the fingerprints from that step on, so only those are built again.

The build tree lives only in the images, never in a folder of the user's:
nothing the book runs touches the machine the build runs on.

Where each step runs, as the book says:
  host-root  in the host image as root: chapter 4's set-up, chapter 7.2-7.3
  host-lfs   in the host image as "lfs", with chapter 4's environment: 4.4-6
  import     7.4, entering the chroot: the $LFS tree is copied out of the
             last image into an image of its own (Docker gives it /dev,
             /proc, /sys, so no mounting and no --privileged)
  chroot     a container of that image as root, with 7.4's environment: 7.5 on
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shlex
import subprocess
import tarfile
import time
from pathlib import Path

from .book import Download, Step

IMAGE = "lfsbuilder-build"  # every checkpoint is IMAGE:<fingerprint>
CONTAINER = "lfsbuilder-step"
SOURCES_IN_CONTAINER = "/lfs-sources"  # work/sources, read-only
LFS = "/mnt/lfs"
FINGERPRINT_VERSION = "1"  # raised when the way steps are run changes, so old images aren't reused
MAX_LAYERS = 100  # overlay2 stops near 125; a deeper image is flattened into one layer

# Who runs a step, where the book says. Chapter 4 is root's (making $LFS and
# the user) up to "su - lfs"; from setting up lfs's environment on, lfs.
# Chapter 7 is root's again, on the host until the chroot, then inside it.
LFS_USER_FROM = "ch-preps-settingenviron"
HOST_ROOT_FROM = "ch-tools-changingowner"
CHROOT_AT = "ch-tools-chroot"

# The book's commands that are for a person at a terminal: they would start
# an interactive shell. The runner does what they are for itself (it loads
# ~/.bashrc before every step of lfs's).
INTERACTIVE = {"source ~/.bash_profile"}

# Mounting the virtual file systems (7.3) is what Docker does for every
# container; the commands that mount are left out, the rest (mkdir) run.
DOCKER_MOUNTS = "ch-tools-kernfs"

# Kinds of command the book gives as an alternative, not to run by default:
# "locale-full" installs every locale instead of the list before it.
OPTIONAL_KINDS = {"locale-full"}

# Docker puts its own /etc/hosts, /etc/hostname and /etc/resolv.conf over
# the container's, so what a step writes there would be lost at the commit.
# A step that writes one leaves a copy in KEPT, and the system made from the
# images gets those copies.
DOCKER_FILES = ("hosts", "hostname", "resolv.conf")
KEPT = "/var/lib/lfsbuilder/etc"

# Docker also makes /etc/mtab a link to /proc/mounts in every container; the
# book makes its own (to /proc/self/mounts) in 7.6, so Docker's is removed
# first. The book's, once made, is the image's and Docker leaves it be.
DOCKER_MTAB = "if [ \"$(readlink /etc/mtab)\" = /proc/mounts ]; then rm -f /etc/mtab; fi"


@dataclasses.dataclass
class Planned:
    step: Step
    mode: str  # host-root, host-lfs, import, chroot
    script: str
    fingerprint: str = ""
    to_hash: str = ""  # the script as fingerprinted (without the machine-dependent parts)

    @property
    def user(self) -> str:
        return "lfs" if self.mode == "host-lfs" else "root"

    @property
    def image(self) -> str:
        return f"{IMAGE}:{self.fingerprint[:24]}"

    @property
    def log_name(self) -> str:
        return f"{self.step.number}-{self.step.id}.log"


def _docker(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, **kw)


def image_exists(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def _block(number: int, kind: str, text: str) -> str:
    """One command, with a marker in the log before it, so the failing
    command is the last marker before the error."""
    marker = f"### lfsbuilder: command {number} ({kind or 'unmarked'})"
    return f"echo {shlex.quote(marker)}\n{text}\n"


def commands(step: Step, run_tests: bool) -> list:
    out = []
    for c in step.commands:
        if c.kind == "test" and not run_tests or c.kind in OPTIONAL_KINDS or c.text.strip() in INTERACTIVE:
            continue
        if step.id == DOCKER_MOUNTS and "mount" in c.text:
            continue
        out.append(c)
    return out


def script(step: Step, mode: str, run_tests: bool, jobs: int) -> str:
    """The shell script for one step: the book's commands, in a fresh
    unpacking of its source as the book's general instructions say (unpack
    in $LFS/sources, enter the directory, build, leave and delete it)."""
    head = ["set -e"]
    sources = "$LFS/sources"
    tail = ""
    if mode == "host-root":
        head += [f"export LFS={LFS}", "umask 022"]
    elif mode == "host-lfs":
        head = ["if [ -f ~/.bashrc ]; then source ~/.bashrc; fi", *head]  # chapter 4.4 writes it
        if jobs:
            head.append(f"export MAKEFLAGS=-j{jobs}")
    else:  # chroot: the sources are at /sources, and Docker's files are watched
        sources = "/sources"
        head += [DOCKER_MTAB, f"for f in {' '.join(DOCKER_FILES)}; do cat /etc/$f > /dev/shm/lfsbuilder-$f 2>/dev/null || true; done"]
        tail = (
            f"for f in {' '.join(DOCKER_FILES)}; do\n"
            f"  if [ -e /etc/$f ] && ! cmp -s /etc/$f /dev/shm/lfsbuilder-$f; then mkdir -p {KEPT}; cat /etc/$f > {KEPT}/$f; fi\n"
            "done\n"
        )
    body = "".join(_block(i, c.kind, c.text) for i, c in enumerate(commands(step, run_tests), start=1))
    if not step.archive:
        return "\n".join(head) + "\n" + body + tail
    archive = shlex.quote(step.archive)
    return (
        "\n".join(head)
        + f"\ncd {sources}\ndir=$(tar -tf {archive} | head -n1); dir=${{dir%%/*}}\n"
        + 'rm -rf "$dir"\n'
        + f'tar -xf {archive}\ncd "$dir"\n'
        + body
        + f'cd {sources}\nrm -rf "$dir"\n'
        + tail
    )


def setup_script(downloads: list[Download]) -> str:
    """What the chapters before 4 leave to the reader: $LFS exists (2.7),
    the sources are in $LFS/sources (3.1), and the host's /etc/bash.bashrc
    is out of lfs's way (4.4). The sources are links to the read-only
    folder, since only the archives are needed and they are big."""
    names = " ".join(shlex.quote(d.name) for d in downloads)
    return (
        f"set -e\nexport LFS={LFS}\numask 022\n"
        "mkdir -pv $LFS/sources\nchmod -v a+wt $LFS/sources\n"
        f"for f in {names}; do ln -sfn {SOURCES_IN_CONTAINER}/$f $LFS/sources/$f; done\n"
        "[ ! -e /etc/bash.bashrc ] || mv -v /etc/bash.bashrc /etc/bash.bashrc.NOUSE\n"
    )


def plan(steps: list[Step], downloads: list[Download], base_image: str, seed: str, run_tests: bool, jobs: int, hostname: str) -> tuple[Planned, list[Planned]]:
    """The setup and every step with its script and fingerprint. `seed`
    names the book (version and edition); the archives' MD5 sums are in it
    too, so a different source is a different build. The host name the
    chroot's containers get is in it from the chroot on. The number of make
    jobs is not: it changes how fast, not what."""
    base_id = _docker("image", "inspect", "-f", "{{.Id}}", base_image).stdout.strip()
    digest = hashlib.sha256(f"{FINGERPRINT_VERSION}\n{base_id}\n{seed}\n".encode())
    for d in downloads:
        digest.update(f"{d.md5} {d.name}\n".encode())
    setup_text = setup_script(downloads)
    setup = Planned(Step(chapter=3, number="3.0", id="lfsbuilder-setup", title="Sources and $LFS"), "host-root", setup_text, to_hash=setup_text)
    planned = [setup]
    mode = "host-root"
    for step in steps:
        if step.id == LFS_USER_FROM:
            mode = "host-lfs"
        elif step.id == HOST_ROOT_FROM:
            mode = "host-root"
        elif step.id == CHROOT_AT:
            mode = "chroot"
            planned.append(Planned(step, "import", "", to_hash=f"import {LFS}\nhostname {hostname}"))
            continue
        if not commands(step, run_tests):
            continue  # a section with nothing to run
        planned.append(Planned(step, mode, script(step, mode, run_tests, jobs), to_hash=script(step, mode, run_tests, 0)))
    for p in planned:
        digest.update(f"{p.step.id}\n{p.user}\n{p.to_hash}\n".encode())
        p.fingerprint = digest.copy().hexdigest()
    return setup, planned[1:]


def _labels(p: Planned) -> list[str]:
    return ["-c", f"LABEL lfsbuilder.step={p.step.number} lfsbuilder.id={p.step.id}", "-c", 'CMD ["/bin/bash"]']


def _retar(source, target, prefix: str = "") -> None:
    """Copies a tar stream, keeping only what is under `prefix` and putting
    it at the top (hard links' targets too)."""
    with tarfile.open(fileobj=source, mode="r|") as tin, tarfile.open(fileobj=target, mode="w|", format=tarfile.PAX_FORMAT) as tout:
        for member in tin:
            if prefix:
                name = member.name.removeprefix("./")
                if name != prefix and not name.startswith(prefix + "/"):
                    continue
                member.name = name[len(prefix) + 1 :] or "."
                if member.islnk():
                    member.linkname = member.linkname.removeprefix("./")[len(prefix) + 1 :]
            tout.addfile(member, tin.extractfile(member) if member.isreg() else None)


def copy_tree(parent: str, image: str, changes: list[str], prefix: str = "") -> None:
    """A new image of one layer from `parent`'s files (only those under
    `prefix`, made its root, if given): docker export | docker import."""
    container = _docker("create", parent, "/bin/true").stdout.strip()
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        load = subprocess.Popen(["docker", "import", *changes, "-", image], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        try:
            _retar(export.stdout, load.stdin, prefix)
        finally:
            load.stdin.close()
            export.stdout.close()
        if export.wait() or load.wait():
            raise RuntimeError(f"copying {parent} into {image} failed")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _layers(image: str) -> int:
    return len(_docker("image", "inspect", "-f", "{{json .RootFS.Layers}}", image).stdout.split(","))


def _run_one(p: Planned, parent: str, sources: Path, logs: Path, hostname: str, jobs: int) -> bool:
    log = logs / p.log_name
    if p.mode == "import":
        with log.open("w") as out:
            out.write(f"copying {LFS} of {parent} into {p.image} as its root\n")
        copy_tree(parent, p.image, _labels(p), LFS.lstrip("/"))
        return True
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    if p.mode == "host-root":
        command = ["bash", "-c", p.script]
    elif p.mode == "host-lfs":  # as chapter 4's ~/.bash_profile does: a clean environment
        command = ["env", "-i", "HOME=/home/lfs", "TERM=dumb", "/bin/bash", "-c", p.script]
    else:  # as chapter 7.4 enters the chroot
        n = jobs or os.cpu_count() or 1
        command = ["/usr/bin/env", "-i", "HOME=/root", "TERM=dumb", "PATH=/usr/bin:/usr/sbin",
                   f"MAKEFLAGS=-j{n}", f"TESTSUITEFLAGS=-j{n}", "/bin/bash", "--login", "-c", p.script]
    with log.open("w") as out:
        result = subprocess.run(
            ["docker", "run", "--name", CONTAINER, "--hostname", hostname, "-u", p.user, "-w", "/",
             "-v", f"{sources}:{SOURCES_IN_CONTAINER}:ro", parent, *command],
            stdout=out, stderr=subprocess.STDOUT,
        )
    try:
        if result.returncode != 0:
            return False
        _docker("commit", *_labels(p), CONTAINER, p.image)
        return True
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def failure_report(p: Planned, logs: Path, lines: int = 30) -> str:
    """The failing command and the end of its log: what a person (or later
    Claude) needs, never the whole log."""
    text = (logs / p.log_name).read_text(errors="replace").splitlines()
    marker = next((l for l in reversed(text) if l.startswith("### lfsbuilder: command")), "")
    command = ""
    if marker:
        number = int(marker.split()[3])
        blocks = p.script.split("echo '### lfsbuilder: command ")
        command = next((b.split("\n", 1)[1] for b in blocks[1:] if b.startswith(f"{number} ")), "")
    out = [f"step {p.step.number} {p.step.title} failed"]
    if command:
        out += [f"at {marker.removeprefix('### lfsbuilder: ')}:", *("    " + l for l in command.rstrip().splitlines())]
    out += [f"the end of {logs / p.log_name}:", *("  | " + l for l in text[-lines:])]
    return "\n".join(out)


def run(setup: Planned, planned: list[Planned], base_image: str, sources: Path, logs: Path, hostname: str, jobs: int) -> int:
    logs.mkdir(parents=True, exist_ok=True)
    parent = base_image
    for p in [setup, *planned]:
        name = f"{p.step.number:>6}  {p.step.title[:44]:44}"
        if image_exists(p.image):
            print(f"{name}  done before", flush=True)
            parent = p.image
            continue
        if parent != base_image and p.mode == "chroot" and _layers(parent) > MAX_LAYERS:
            print(f"{'':>6}  (flattening {parent}: too many layers)", flush=True)
            copy_tree(parent, parent, ["-c", 'CMD ["/bin/bash"]'])
        print(f"{name}  ...", end="", flush=True)
        started = time.monotonic()
        try:
            ok = _run_one(p, parent, sources, logs, hostname, jobs)
        except (subprocess.CalledProcessError, RuntimeError) as error:
            (logs / p.log_name).open("a").write(f"\nlfsbuilder: {error}\n{getattr(error, 'stderr', '') or ''}")
            ok = False
        took = time.monotonic() - started
        if not ok:
            print(f"  FAILED after {took:.0f}s\n")
            print(failure_report(p, logs))
            return 1
        print(f"  {took:.0f}s", flush=True)
        parent = p.image
    print(f"\nthe last checkpoint: {parent}")
    return 0
