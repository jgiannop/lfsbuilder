"""The distribution's binary package server: the packages the builds compiled
(Portage's `buildpkg`, in the binpkgs volumes), signed and served over HTTP,
so installed systems get them as updates instead of compiling.

  signing_key()  the distribution's package signing key: the private key in
                 work/binhost/gnupg (never in git; back it up), the public
                 one written beside the recipes for [updates] key
  publish()      a recipe's set of packages copied (only those Portage's
                 index lists: a stopped build can leave half-written files),
                 each signed with the recipe's key, indexed (Packages), and
                 put in place at once: www/<set>/ is never half updated
  publish_repository()
                 the recipe's pinned commit of the Gentoo repository in
                 www/gentoo.git, on a branch named after the recipe: the
                 tree the systems sync to, the one their packages were
                 built against (Gentoo's latest would run ahead of them)
  serve()        an HTTP server of www/: files as they are (the packages
                 are signed, so plain HTTP is enough for them), and
                 gentoo.git through git's own http-backend (shallow clones
                 need git's smart protocol); on a network, a web server
                 with git-http-backend serves the same directory

Systems built with [updates] fetch <server><set>/Packages and the packages,
and check every signature with lfsbuilder-trust's keyring; they sync
<server>gentoo.git and check that its head is signed by Gentoo's repository
mirror key, as they would Gentoo's own (steps.updates).
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
from pathlib import Path

from . import gentoo
from .recipe import Recipe

REPOSITORY = "gentoo.git"  # in www/; a branch per recipe
OVERLAY = "argo.git"  # the distribution's own repository (overlay.py); a branch per recipe

KEY_TYPE = ("ed25519", "sign", "never")  # gpg --quick-gen-key: algorithm, usage, expiry


class BinhostError(RuntimeError):
    pass


def fingerprint(tools, public_key: Path) -> str:
    """The primary key's fingerprint of an armored public key file (read
    by gpg in the tools image from its stdin: the file may be anywhere)."""
    out = subprocess.run(["docker", "run", "--rm", "-i", "-e", "HOME=/tmp", tools.image, "gpg", "--batch", "--show-keys", "--with-colons"],
                         input=public_key.read_text(encoding="ascii"), text=True, capture_output=True, check=True).stdout
    fprs, primary = [], False
    for line in out.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            primary = True
        elif fields[0] == "fpr" and primary:
            fprs.append(fields[9])
            primary = False
    if len(fprs) != 1:
        raise BinhostError(f"{public_key} should hold one key, not {len(fprs)}")
    return fprs[0]


def signing_key(tools, home: Path, public_key: Path, name: str) -> str:
    """Makes the package signing key (once) and writes its public half.
    Returns its fingerprint."""
    if (home / "pubring.kbx").exists():
        raise BinhostError(f"{home} already holds a signing key; it is kept (remove it yourself to make a new one)")
    home.mkdir(parents=True, mode=0o700)
    inside = tools.inside(home)
    tools.run("gpg", "--homedir", inside, "--batch", "--quiet", "--passphrase", "",
              "--quick-gen-key", f"{name} binary packages (lfsbuilder)", *KEY_TYPE)
    public_key.parent.mkdir(parents=True, exist_ok=True)
    armored = tools.run("gpg", "--homedir", inside, "--batch", "--armor", "--export", capture=True).stdout
    public_key.write_text(armored)
    return fingerprint(tools, public_key)


def _publish_script(fpr: str, uid: int, gid: int) -> str:
    return f"""set -euo pipefail
mkdir -p /run/lock
# A copy of the signing keyring the container may write to (gpg's lock and
# seed files), trusting its own key and Gentoo's release keys: indexing
# checks every signature, ours and Gentoo's.
cp -a /key /tmp/gnupg && chmod 700 /tmp/gnupg
gpg --homedir /tmp/gnupg --batch --quiet --import /usr/share/openpgp-keys/gentoo-release.asc 2>/dev/null
{{ echo "{fpr}:6:"
   gpg --homedir /tmp/gnupg --batch --with-colons --show-keys /usr/share/openpgp-keys/gentoo-release.asc |
       awk -F: '$1 == "pub" {{ want = 1 }} $1 == "fpr" && want {{ print $10 ":6:"; want = 0 }}'
}} | gpg --homedir /tmp/gnupg --batch --quiet --import-ownertrust
gpg --homedir /tmp/gnupg --batch --quiet --check-trustdb 2>/dev/null
gpg --homedir /tmp/gnupg --batch --list-secret-keys "{fpr}" >/dev/null || {{ echo "work/binhost/gnupg lacks the private key {fpr}" >&2; exit 1; }}
chmod -R a+rX /tmp/gnupg  # Portage checks signatures as nobody
cd /src
[ -f Packages ] || {{ echo "no packages built in this set yet" >&2; exit 1; }}
# Ours: what Portage's index lists (a stopped build can leave half-written files beside them).
sed -n 's/^PATH: //p' Packages | while read -r path; do
    mkdir -p "/out/${{path%/*}}"
    cp -p "$path" "/out/$path"
done
# Gentoo's, as the builds downloaded them (signed by Gentoo), less what we built ourselves:
# the one we built is the one installed.
gentoos=0
if [ -d /cache/gentoobinhost ]; then
    cd /cache/gentoobinhost
    while IFS= read -r -d '' path; do
        path=${{path#./}}
        name=${{path##*/}}
        cpv="${{path%%/*}}/${{name%-*.gpkg.tar}}"
        grep -qxF "CPV: $cpv" /src/Packages && continue
        [ -e "/out/$path" ] && continue
        mkdir -p "/out/${{path%/*}}"
        cp -p "$path" "/out/$path"
        gentoos=$((gentoos + 1))
    done < <(find . -name '*.gpkg.tar' -print0)
fi
export PKGDIR=/out FEATURES="binpkg-signing" BINPKG_GPG_SIGNING_GPG_HOME=/tmp/gnupg \\
    BINPKG_GPG_SIGNING_KEY="{fpr}" BINPKG_GPG_VERIFY_GPG_HOME=/tmp/gnupg
ours=0
while IFS= read -r -d '' package; do
    case "$(tar -tf "$package")" in *image.tar.*.sig*) continue ;; esac  # signed already (Gentoo's); no pipe: pipefail
    gpkg-sign "$package" >/dev/null 2>/tmp/sign.log || {{ cat /tmp/sign.log >&2; exit 1; }}  # one file per call: it signs only its first
    ours=$((ours + 1))
done < <(find /out -name '*.gpkg.tar' -print0)
emaint binhost --fix >/tmp/index.log 2>&1 || {{ cat /tmp/index.log >&2; exit 1; }}
indexed=$(grep -c '^PATH: ' /out/Packages || true)
total=$(find /out -name '*.gpkg.tar' | wc -l)
[ "$indexed" = "$total" ] || {{ cat /tmp/index.log >&2; echo "$total packages, but the index lists $indexed (a signature that didn't check?)" >&2; exit 1; }}
chown -R {uid}:{gid} /out
echo "$ours $gentoos"
"""


def publish(tools, r: Recipe, image: str, volume: str, cache: str, home: Path, www: Path) -> tuple[int, int]:
    """Publishes the recipe's set of binary packages in www/<set>/: those
    built here (the binpkgs volume), signed with our key, and Gentoo's that
    the builds downloaded (the binhost cache volume), with Gentoo's
    signatures. `image` is any checkpoint of the recipe (it has Portage and
    gpg). Returns how many of each were published."""
    if not r.binhost:
        raise BinhostError(f"{r.path.name} has no [updates] server and key")
    fpr = fingerprint(tools, r.path.parent / r.binhost_key)
    target = www / r.binary_set
    new, old = target.with_name(target.name + ".new"), target.with_name(target.name + ".old")
    for leftover in (new, old):
        shutil.rmtree(leftover, ignore_errors=True)
    new.mkdir(parents=True)
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:/src:ro", "-v", f"{cache}:/cache:ro", "-v", f"{home}:/key:ro",
         "-v", f"{new}:/out", image, "bash", "-c", _publish_script(fpr, os.getuid(), os.getgid())],
        text=True, capture_output=True)
    if result.returncode:
        shutil.rmtree(new, ignore_errors=True)
        raise BinhostError("publishing failed:\n" + "\n".join((result.stdout + result.stderr).splitlines()[-30:]))
    if target.exists():
        target.rename(old)
    new.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    ours, gentoos = result.stdout.split()[-2:]
    return int(ours), int(gentoos)


def publish_repository(tools, r: Recipe, keyring: Path, www: Path) -> None:
    """Puts the recipe's pinned commit in www/gentoo.git as the branch
    <recipe name>, after checking again that Gentoo's mirror key signed it.
    The commit is fetched alone (depth 1); a system syncing the branch gets
    that tree, and when the recipe pins a newer commit and is published
    again, that one."""
    git = www / REPOSITORY
    inside = tools.inside(git)
    if not (git / "HEAD").exists():
        www.mkdir(parents=True, exist_ok=True)
        tools.run("git", "init", "--quiet", "--bare", inside)
    tools.run("git", "-C", inside, "fetch", "--quiet", "--depth", "1", gentoo.REPOSITORY_URL, r.repository)
    result = subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", f"GNUPGHOME={tools.inside(keyring)}",
         "-v", f"{tools.work}:/work", tools.image, "git", "-C", inside, "verify-commit", "--raw", r.repository],
        text=True, capture_output=True)
    if gentoo._signer(result.stderr) != gentoo.MIRROR_KEY:
        raise BinhostError(f"commit {r.repository} isn't signed by Gentoo's repository mirror key:\n{result.stderr}")
    tools.run("git", "-C", inside, "update-ref", f"refs/heads/{r.stem}", r.repository)
    tools.run("git", "-C", inside, "symbolic-ref", "HEAD", f"refs/heads/{r.stem}")  # some branch HEAD must name
    tools.run("git", "-C", inside, "gc", "--quiet", "--prune=now")  # what no branch needs any more


def publish_overlay(tools, r: Recipe, tree: Path, home: Path, www: Path) -> str:
    """Commits the recipe's argo repository (`tree`, written by overlay.write)
    to www/argo.git as the branch <recipe name>, signed with the package
    signing key (systems check it: steps.updates). A new commit only when
    the tree changed. Returns the branch's commit."""
    git = www / OVERLAY
    if not (git / "HEAD").exists():
        www.mkdir(parents=True, exist_ok=True)
        tools.run("git", "init", "--quiet", "--bare", tools.inside(git))
    fpr = fingerprint(tools, r.path.parent / r.binhost_key)
    script = f"""set -euo pipefail
export GIT_DIR="$1" GIT_WORK_TREE="$2" GIT_INDEX_FILE=/tmp/index GNUPGHOME="$3"
git read-tree --empty
git add -A
tree=$(git write-tree)
parent=$(git rev-parse -q --verify "refs/heads/{r.stem}" || true)
if [ -n "$parent" ] && [ "$(git rev-parse "$parent^{{tree}}")" = "$tree" ]; then
    echo "$parent"; exit 0
fi
commit=$(git -c user.name="{r.name}" -c user.email=packages@argo-linux.invalid commit-tree -S{fpr} "$tree" \
    ${{parent:+-p "$parent"}} -m "{r.name} {r.version} ({r.stem})")
git update-ref "refs/heads/{r.stem}" "$commit"
git symbolic-ref HEAD "refs/heads/{r.stem}"
git verify-commit "$commit" >/dev/null 2>&1
echo "$commit"
"""
    out = tools.run("bash", "-c", script, "bash", tools.inside(git), tools.inside(tree), tools.inside(home), capture=True).stdout
    return out.split()[-1]


GIT_HTTP_BACKEND = Path(subprocess.run(["git", "--exec-path"], text=True, capture_output=True).stdout.strip() or "/usr/lib/git-core") / "git-http-backend"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def list_directory(self, path):  # files only, no listings
        self.send_error(404)
        return None

    def _is_git(self) -> bool:
        return self.path.startswith((f"/{REPOSITORY}/", f"/{OVERLAY}/"))

    def do_GET(self):
        if self._is_git():
            return self._git()
        return super().do_GET()

    def do_POST(self):
        if self._is_git():
            return self._git()
        self.send_error(405)

    def _git(self) -> None:
        """git's smart HTTP protocol: git http-backend as a CGI program
        (read only: it refuses pushes unless told otherwise)."""
        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {
            "GIT_PROJECT_ROOT": self.directory, "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
            "QUERY_STRING": query, "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": self.client_address[0], "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
        }
        result = subprocess.run([str(GIT_HTTP_BACKEND)], input=body, env=env, capture_output=True)
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        if not _:
            self.send_error(500, "git http-backend gave no answer")
            return
        status, headers = 200, []
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(www: Path, bind: str, port: int) -> None:
    """Serves www/ until Ctrl-C. QEMU's guests reach the host's 127.0.0.1 as 10.0.2.2."""
    handler = functools.partial(_Handler, directory=str(www))
    with http.server.ThreadingHTTPServer((bind, port), handler) as server:
        print(f"serving {www} at http://{bind}:{port}/ (Ctrl-C stops)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
