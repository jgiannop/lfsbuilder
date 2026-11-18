"""The repositories a system starts with: clones of what the server publishes,
put into the system when it is exported (image.export), so that

  - its first `emerge --sync` is a fetch, whose head Portage checks before
    merging it (a first sync is a clone, checked only after cloning: a
    refused clone would stay on disk);
  - it can install packages before it has synced at all (an empty tree is
    an invalid profile).

Each clone is of the recipe's branch, its last commit only, with origin set
to the server's URL (the systems' sync-uri), and is checked first: Gentoo's
must be the commit the recipe pins, signed by Gentoo's repository mirror
key; argo's must be signed by the package signing key and hold exactly the
argo repository written from the recipe now (else: publish first).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import binhost, gentoo
from .recipe import Recipe


class CloneError(RuntimeError):
    pass


def _signer(tools, git: Path, home: Path) -> str:
    result = subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", f"GNUPGHOME={tools.inside(home)}",
         "-v", f"{tools.work}:/work", tools.image, "git", "-C", tools.inside(git), "verify-commit", "--raw", "HEAD"],
        text=True, capture_output=True)
    return gentoo._signer(result.stderr)


def _clone(tools, source: Path, branch: str, target: Path, url: str) -> None:
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    tools.run("git", "clone", "--quiet", "--depth", "1", "--branch", branch, "--single-branch",
              f"file://{tools.inside(source)}", tools.inside(target))
    tools.run("git", "-C", tools.inside(target), "remote", "set-url", "origin", url)


def _tree_of(tools, directory: Path) -> str:
    """The git tree id `directory`'s files would have (as publish_overlay commits them)."""
    script = ('set -e; git_dir=$(mktemp -d); git init --quiet --bare "$git_dir"\n'
              'export GIT_DIR="$git_dir" GIT_WORK_TREE="$1" GIT_INDEX_FILE=$(mktemp -u)\n'
              'cd "$1"; git add -A; git write-tree')
    return tools.run("bash", "-c", script, "bash", tools.inside(directory), capture=True).stdout.split()[-1]


def clones(tools, r: Recipe, overlay_tree: Path, gentoo_keys: Path, www: Path, into: Path) -> dict[str, Path]:
    """Clones of the published gentoo and argo repositories for the recipe,
    checked: {path in the system: local clone}. Empty without [updates]."""
    if not r.binhost:
        return {}
    for name in (binhost.REPOSITORY, binhost.OVERLAY):
        known = subprocess.run(["git", "-C", str(www / name), "rev-parse", "-q", "--verify", f"refs/heads/{r.stem}"],
                               capture_output=True).returncode == 0 if (www / name / "HEAD").exists() else False
        if not known:
            raise CloneError(f"{name} has no branch {r.stem} yet: python3 -m lfsbuilder publish {r.path}")
    gentoo_clone, argo_clone = into / "gentoo", into / "argo"

    _clone(tools, www / binhost.REPOSITORY, r.stem, gentoo_clone, f"{r.binhost}{binhost.REPOSITORY}")
    head = tools.run("git", "-C", tools.inside(gentoo_clone), "rev-parse", "HEAD", capture=True).stdout.strip()
    if head != r.repository:
        raise CloneError(f"the published gentoo.git has {head[:12]} for {r.stem}, the recipe pins {r.repository[:12]}: "
                         f"python3 -m lfsbuilder publish {r.path}")
    if _signer(tools, gentoo_clone, gentoo_keys) != gentoo.MIRROR_KEY:
        raise CloneError(f"the published gentoo.git's {head[:12]} isn't signed by Gentoo's repository mirror key")

    _clone(tools, www / binhost.OVERLAY, r.stem, argo_clone, f"{r.binhost}{binhost.OVERLAY}")
    published = tools.run("git", "-C", tools.inside(argo_clone), "rev-parse", "HEAD^{tree}", capture=True).stdout.strip()
    # What publish adds to the recipe's tree: the date of publishing.
    expected = into / "argo-expected"
    shutil.rmtree(expected, ignore_errors=True)
    shutil.copytree(overlay_tree, expected)
    shutil.copy2(argo_clone / "metadata" / "timestamp.chk", expected / "metadata" / "timestamp.chk")
    same = published == _tree_of(tools, expected)
    shutil.rmtree(expected, ignore_errors=True)
    if not same:
        raise CloneError(f"the published argo.git isn't the argo repository of the recipe as it is now: python3 -m lfsbuilder publish {r.path}")
    home = into / "argo-keyring"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(mode=0o700, parents=True)
    key = r.path.parent / r.binhost_key
    subprocess.run(["docker", "run", "--rm", "-i", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tools.work}:/work",
                    tools.image, "gpg", "--homedir", tools.inside(home), "--batch", "--quiet", "--import"],
                   input=key.read_text(), text=True, check=True, capture_output=True)
    if _signer(tools, argo_clone, home) != binhost.fingerprint(tools, key):
        raise CloneError("the published argo.git's head isn't signed by the package signing key")
    shutil.rmtree(home, ignore_errors=True)
    return {"var/db/repos/gentoo": gentoo_clone, "var/db/repos/argo": argo_clone}
