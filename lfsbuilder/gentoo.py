"""What a recipe pins, fetched and checked: a Gentoo stage3 and a commit of
the Gentoo repository.

Nothing is used unless it is signed by one of Gentoo's keys whose
fingerprints are written here: the stage3 by Release Engineering's
automated key, the repository commit by the repository mirror's key. The
keys themselves come from Gentoo's published key file, but a key file can
say anything, so it is the fingerprint here that decides.

Gentoo removes old stage3 builds from its servers after some months; the
copies in work/gentoo/ are what keep an old recipe buildable.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.request
from pathlib import Path

STAGE3_URL = "https://distfiles.gentoo.org/releases/amd64/autobuilds/{date}/stage3-amd64-{init}-{date}.tar.xz"
REPOSITORY_URL = "https://github.com/gentoo-mirror/gentoo.git"  # the repository with its metadata cache, every commit signed
KEYS_URL = "https://qa-reports.gentoo.org/output/service-keys.gpg"
RELEASE_KEY = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"  # Gentoo Linux Release Engineering (Automated Weekly Release Key)
MIRROR_KEY = "EF9538C9E8E64311A52CDEDFA13D0EF1914E7A72"  # Gentoo repository mirrors (automated git signing key)
AGENT = "lfsbuilder"


class VerifyError(RuntimeError):
    pass


def _get(url: str, to: Path) -> None:
    part = to.with_name(to.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as f:
        while block := response.read(1 << 20):
            f.write(block)
    part.rename(to)


def sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Tools:
    """Commands in the tools image, with work/ at /work, as the user."""

    def __init__(self, image: str, work: Path):
        self.image, self.work = image, work

    def run(self, *command: str, capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
             "-v", f"{self.work}:/work", self.image, *command],
            check=True, text=True, capture_output=capture,
        )

    def inside(self, path: Path) -> str:
        return "/work/" + str(path.relative_to(self.work))


def _signer(status: str) -> str:
    """The primary key's fingerprint from gpg's VALIDSIG status line."""
    for line in status.splitlines():
        parts = line.split()
        if parts[:2] == ["[GNUPG:]", "VALIDSIG"]:
            return parts[-1]
    return ""


def keyring(tools: Tools, into: Path) -> Path:
    """Gentoo's keys, imported into a keyring of lfsbuilder's own."""
    home = into / "gnupg"
    if not (home / "pubring.kbx").exists():
        into.mkdir(parents=True, exist_ok=True)
        _get(KEYS_URL, into / "service-keys.gpg")
        home.mkdir(mode=0o700, exist_ok=True)
        tools.run("gpg", "--homedir", tools.inside(home), "--batch", "--quiet", "--import", tools.inside(into / "service-keys.gpg"), capture=True)
    return home


def stage3(tools: Tools, gnupg: Path, into: Path, date: str, init: str) -> Path:
    """The stage3 archive, downloaded once and checked against its
    signature every time it is fetched."""
    url = STAGE3_URL.format(date=date, init=init)
    archive = into / url.rsplit("/", 1)[1]
    signature = archive.with_name(archive.name + ".asc")
    into.mkdir(parents=True, exist_ok=True)
    if not signature.exists():
        _get(url + ".asc", signature)
    if not archive.exists():
        print(f"downloading {url}", flush=True)
        _get(url, archive)
    result = subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tools.work}:/work", tools.image,
         "gpg", "--homedir", tools.inside(gnupg), "--batch", "--status-fd", "1", "--verify", tools.inside(signature), tools.inside(archive)],
        text=True, capture_output=True,
    )
    if _signer(result.stdout) != RELEASE_KEY:
        raise VerifyError(f"{archive.name} isn't signed by Gentoo's release key {RELEASE_KEY}:\n{result.stdout}{result.stderr}")
    return archive


def repository(tools: Tools, gnupg: Path, into: Path, commit: str) -> Path:
    """The Gentoo repository at one commit, checked against the mirror's
    signature; kept as a plain tree (no .git) for mounting read-only."""
    tree = into / commit
    if (tree / "profiles" / "repo_name").exists():
        return tree
    git = into / f"{commit}.git"
    print(f"fetching the Gentoo repository at {commit}", flush=True)
    tools.run("git", "init", "--quiet", "--bare", tools.inside(git))
    tools.run("git", "-C", tools.inside(git), "fetch", "--quiet", "--depth", "1", REPOSITORY_URL, commit)
    result = subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", f"GNUPGHOME={tools.inside(gnupg)}",
         "-v", f"{tools.work}:/work", tools.image, "git", "-C", tools.inside(git), "verify-commit", "--raw", commit],
        text=True, capture_output=True,
    )
    if _signer(result.stderr) != MIRROR_KEY:
        raise VerifyError(f"commit {commit} isn't signed by Gentoo's repository mirror key {MIRROR_KEY}:\n{result.stderr}")
    part = into / f"{commit}.part"
    tools.run("sh", "-c", f'rm -rf "$1" && mkdir -p "$1" && git -C "$2" archive {commit} | tar -x -C "$1"', "sh", tools.inside(part), tools.inside(git))
    part.rename(tree)
    tools.run("rm", "-rf", tools.inside(git))
    return tree


def stage3_image(archive: Path, name: str) -> str:
    """The stage3 as a Docker image: it is a whole root file system."""
    if subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode != 0:
        print(f"importing {archive.name} as {name}", flush=True)
        subprocess.run(["docker", "import", "-c", 'CMD ["/bin/bash"]', str(archive), name], check=True, capture_output=True)
    return name


def password_hash(tools: Tools, password: str) -> str:
    """The SHA-512 crypt hash of `password`, made by openssl in the tools
    image; the password goes in on its stdin, never on a command line."""
    result = subprocess.run(["docker", "run", "--rm", "-i", tools.image, "openssl", "passwd", "-6", "-stdin"],
                            input=password + "\n", text=True, capture_output=True, check=True)
    return result.stdout.strip()
