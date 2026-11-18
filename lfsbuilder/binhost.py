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
  serve()        a plain HTTP server of www/ (the packages are signed, so
                 plain HTTP is enough for them; on a network, a real web
                 server or a static host serves the same directory)

Systems built with [updates] fetch <server><set>/Packages and the packages,
and check every signature with lfsbuilder-trust's keyring (steps.updates).
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
from pathlib import Path

from .recipe import Recipe

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
# seed files), trusting its own key: indexing checks the new signatures.
cp -a /key /tmp/gnupg && chmod 700 /tmp/gnupg
echo "{fpr}:6:" | gpg --homedir /tmp/gnupg --batch --quiet --import-ownertrust
gpg --homedir /tmp/gnupg --batch --quiet --check-trustdb 2>/dev/null
gpg --homedir /tmp/gnupg --batch --list-secret-keys "{fpr}" >/dev/null || {{ echo "work/binhost/gnupg lacks the private key {fpr}" >&2; exit 1; }}
chmod -R a+rX /tmp/gnupg  # Portage checks signatures as nobody
cd /src
[ -f Packages ] || {{ echo "no packages built in this set yet" >&2; exit 1; }}
sed -n 's/^PATH: //p' Packages | while read -r path; do
    mkdir -p "/out/${{path%/*}}"
    cp -p "$path" "/out/$path"
done
export PKGDIR=/out FEATURES="binpkg-signing" BINPKG_GPG_SIGNING_GPG_HOME=/tmp/gnupg \\
    BINPKG_GPG_SIGNING_KEY="{fpr}" BINPKG_GPG_VERIFY_GPG_HOME=/tmp/gnupg
count=0
while IFS= read -r -d '' package; do
    gpkg-sign "$package" >/dev/null 2>/tmp/sign.log || {{ cat /tmp/sign.log >&2; exit 1; }}  # one file per call: it signs only its first
    count=$((count + 1))
done < <(find /out -name '*.gpkg.tar' -print0)
emaint binhost --fix >/tmp/index.log 2>&1 || {{ cat /tmp/index.log >&2; exit 1; }}
indexed=$(grep -c '^PATH: ' /out/Packages || true)
[ "$indexed" = "$count" ] || {{ cat /tmp/index.log >&2; echo "signed $count packages, but the index lists $indexed" >&2; exit 1; }}
chown -R {uid}:{gid} /out
echo "$count"
"""


def publish(tools, r: Recipe, image: str, volume: str, home: Path, www: Path) -> int:
    """Signs and publishes the recipe's set of binary packages in
    www/<set>/. `image` is any checkpoint of the recipe (it has Portage and
    gpg). Returns how many packages were published."""
    if not r.binhost:
        raise BinhostError(f"{r.path.name} has no [updates] server and key")
    fpr = fingerprint(tools, r.path.parent / r.binhost_key)
    target = www / r.binary_set
    new, old = target.with_name(target.name + ".new"), target.with_name(target.name + ".old")
    for leftover in (new, old):
        shutil.rmtree(leftover, ignore_errors=True)
    new.mkdir(parents=True)
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:/src:ro", "-v", f"{home}:/key:ro",
         "-v", f"{new}:/out", image, "bash", "-c", _publish_script(fpr, os.getuid(), os.getgid())],
        text=True, capture_output=True)
    if result.returncode:
        shutil.rmtree(new, ignore_errors=True)
        raise BinhostError("publishing failed:\n" + "\n".join((result.stdout + result.stderr).splitlines()[-30:]))
    if target.exists():
        target.rename(old)
    new.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return int(result.stdout.split()[-1])


class _Handler(http.server.SimpleHTTPRequestHandler):
    def list_directory(self, path):  # files only, no listings
        self.send_error(404)
        return None


def serve(www: Path, bind: str, port: int) -> None:
    """Serves www/ until Ctrl-C. QEMU's guests reach the host's 127.0.0.1 as 10.0.2.2."""
    handler = functools.partial(_Handler, directory=str(www))
    with http.server.ThreadingHTTPServer((bind, port), handler) as server:
        print(f"serving {www} at http://{bind}:{port}/ (Ctrl-C stops)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
