"""Runs the steps in Docker, with a checkpoint after every one.

Each step runs in a container started from the image the step before it
left, and when it succeeds the container is committed as a new image: the
checkpoint is an image layer, holding only what that step changed. A failure
leaves nothing behind but its log; the next run starts from the last good
image.

Every image is named by a fingerprint of everything that went into it: the
stage3 (its SHA512), the repository commit, every step's script up to it.
So a run finds its way back by itself (the steps whose images exist are
done), and changing a step changes the fingerprints from that step on, so
only those are built again. The number of make jobs is not in it: it
changes how fast, not what.

What the containers share, besides the image: the repository, read-only at
/var/db/repos/gentoo; the downloaded sources and the binary packages, each
in a Docker volume, so a package built once is never built again for the
same settings.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from .steps import EMERGE_OPTS, Step

IMAGE = "lfsbuilder-build"  # every checkpoint is IMAGE:<fingerprint>
CONTAINER = "lfsbuilder-step"  # + the step's fingerprint, so two builds at once never touch each other's container
FINGERPRINT_VERSION = "gentoo-1"  # raised when the way steps are run changes, so old images aren't reused
MAX_LAYERS = 100  # overlay2 stops near 125; a deeper image is flattened into one layer
MARKER = "### lfsbuilder:"


@dataclasses.dataclass
class Planned:
    number: int
    step: Step
    fingerprint: str

    @property
    def image(self) -> str:
        return f"{IMAGE}:{self.fingerprint[:24]}"

    @property
    def log_name(self) -> str:
        return f"{self.number:02}-{self.step.name}.log"


@dataclasses.dataclass
class Shared:
    """What every container gets mounted, and how many jobs."""
    repository: Path
    distfiles: str  # volume names
    binpkgs: str
    jobs: int

    def args(self) -> list[str]:
        n = self.jobs or os.cpu_count() or 1
        return [
            "-v", f"{self.repository}:/var/db/repos/gentoo:ro",
            "-v", f"{self.distfiles}:/var/cache/distfiles",
            "-v", f"{self.binpkgs}:/var/cache/binpkgs",
            "-e", f"MAKEOPTS=-j{n} -l{n}",
            # Several packages at once (binary packages are mostly unpacking), held back
            # by the load; in the environment, so it isn't in make.conf or the fingerprints.
            "-e", f"EMERGE_DEFAULT_OPTS={EMERGE_OPTS} --jobs={max(2, n // 2)} --load-average={n}",
        ]


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True)


def image_exists(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def plan(steps: list[Step], seed: str) -> list[Planned]:
    digest = hashlib.sha256(f"{FINGERPRINT_VERSION}\n{seed}\n".encode())
    planned = []
    for number, step in enumerate(steps, start=1):
        digest.update(f"{step.name}\n{step.script}\n".encode())
        planned.append(Planned(number, step, digest.copy().hexdigest()))
    return planned


def flatten(image: str) -> None:
    """The same files as one layer, under the same name: docker export | docker import."""
    container = _docker("create", image, "/bin/true").stdout.strip()
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        load = subprocess.run(["docker", "import", "-c", 'CMD ["/bin/bash"]', "-", image], stdin=export.stdout, capture_output=True)
        export.stdout.close()
        if export.wait() or load.returncode:
            raise RuntimeError(f"flattening {image} failed: {load.stderr.decode(errors='replace')}")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _layers(image: str) -> int:
    return len(_docker("image", "inspect", "-f", "{{json .RootFS.Layers}}", image).stdout.split(","))


def _script(step: Step) -> str:
    """The step's script, with bash printing each command (after MARKER)
    before it runs it, so the log shows which one failed."""
    return f"PS4='{MARKER} '\nset -x\n{step.script}"


def _run_one(p: Planned, parent: str, shared: Shared, logs: Path) -> bool:
    container = f"{CONTAINER}-{p.fingerprint[:12]}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    with (logs / p.log_name).open("w") as out:
        # The script comes on stdin, not as an argument: one argument can't exceed 128 KB (Linux's
        # MAX_ARG_STRLEN), and a step carrying pictures (branding) does. The outer bash reads it whole
        # and runs it as a file (descriptor 3) with stdin /dev/null, so no command in it can read the
        # script's text, and the trace lines are as they were (eval would double PS4's first #).
        result = subprocess.run(
            ["docker", "run", "-i", "--name", container, "-w", "/", *shared.args(), parent,
             "/bin/bash", "-c", 'script=$(cat) && exec /bin/bash -l /dev/fd/3 3<<<"$script" </dev/null'],
            input=_script(p.step).encode(), stdout=out, stderr=subprocess.STDOUT,
        )
    try:
        if result.returncode != 0:
            _keep_portage_log(container, logs / p.log_name)
            return False
        _docker("commit", "-c", f"LABEL lfsbuilder.step={p.step.name}", "-c", 'CMD ["/bin/bash"]', container, p.image)
        return True
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


_PORTAGE_LOG = re.compile(r"'(/var/tmp/portage/[^']+/temp/build\.log)'")


def portage_log(step_log: Path) -> Path:
    return step_log.with_suffix(".portage.log")


def _keep_portage_log(container: str, step_log: Path) -> None:
    """Portage's own log of a package that failed (named in the step's log),
    copied out of the container before it goes."""
    found = _PORTAGE_LOG.findall(step_log.read_text(errors="replace"))
    target = portage_log(step_log)
    target.unlink(missing_ok=True)
    if found:
        subprocess.run(["docker", "cp", f"{container}:{found[-1]}", str(target)], capture_output=True)


def failure_report(p: Planned, logs: Path, lines: int = 40) -> str:
    """The failing step's script and the end of its log: what a person (or
    later Claude) needs, never the whole log. Portage's own build log for a
    failed package is named in that end."""
    text = (logs / p.log_name).read_text(errors="replace").splitlines()
    command = next((l.removeprefix(MARKER).strip() for l in reversed(text) if l.startswith(MARKER)), "")
    out = [f"step {p.number} ({p.step.title}) failed" + (f" at: {command}" if command else "")]
    out += [f"the end of {logs / p.log_name}:", *("  | " + l for l in text[-lines:])]
    return "\n".join(out)


def run(planned: list[Planned], base: str, shared: Shared, logs: Path, quiet: bool = False) -> Planned | None:
    """Runs the steps not done yet; returns the one that failed, or None."""
    logs.mkdir(parents=True, exist_ok=True)
    for volume in (shared.distfiles, shared.binpkgs):
        _docker("volume", "create", volume)
    parent = base
    for p in planned:
        name = f"{p.number:>3}  {p.step.title[:56]:56}"
        if image_exists(p.image):
            if not quiet:
                print(f"{name}  done before", flush=True)
            parent = p.image
            continue
        if parent != base and _layers(parent) > MAX_LAYERS:
            print(f"     (flattening {parent}: too many layers)", flush=True)
            flatten(parent)
        print(f"{name}  ...", end="", flush=True)
        started = time.monotonic()
        try:
            ok = _run_one(p, parent, shared, logs)
        except subprocess.CalledProcessError as error:
            with (logs / p.log_name).open("a") as log:
                log.write(f"\nlfsbuilder: {error}\n{error.stderr or ''}")
            ok = False
        took = time.monotonic() - started
        if not ok:
            print(f"  FAILED after {took:.0f}s\n")
            print(failure_report(p, logs))
            return p
        print(f"  {took:.0f}s", flush=True)
        parent = p.image
    if not quiet:
        print(f"\nthe last checkpoint: {parent}")
    return None
