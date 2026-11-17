"""The sources the book lists: downloaded once, checked against the book's
MD5 sums, kept in one directory shared by every build.

A file is only put in place after its checksum matches, so whatever is in
the directory is right. When the upstream address fails (projects move their
archives), the copy of the same file on the LFS project's mirror is tried;
the checksum decides either way.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

from .book import Download

MIRROR = "https://ftp.osuosl.org/pub/lfs/lfs-packages/{book}/{name}"
AGENT = "lfsbuilder (wget-like)"
TIMEOUT = 60


def md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _get(url: str, to: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, to.open("wb") as f:
        while block := response.read(1 << 20):
            f.write(block)


def pack_bootscripts(book_source: Path, name: str, to: Path) -> None:
    """What the book's make-aux-files.sh does, made reproducible: the
    bootscripts/ directory packed as lfs-bootscripts-<version>/, with fixed
    owners, times and order, so the same book gives the same archive."""
    top = name.removesuffix(".tar.xz")
    files = sorted(p for p in (book_source / "bootscripts").rglob("*") if ".svn" not in p.parts)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        for path in [book_source / "bootscripts", *files]:
            info = tar.gettarinfo(str(path), f"{top}/{path.relative_to(book_source / 'bootscripts')}".rstrip("/."))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if info.isfile():
                with path.open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    to.write_bytes(buffer.getvalue())


def fetch_one(d: Download, into: Path, book: str, book_source: Path) -> str:
    """Puts one source in `into`; returns what happened, for the report."""
    target = into / d.name
    if d.from_book:
        pack_bootscripts(book_source, d.name, target)
        return "packed from the book"
    if target.exists():
        if md5(target) == d.md5:
            return "already here"
        target.unlink()
    part = into / f"{d.name}.part"
    errors = []
    for url in (d.url, MIRROR.format(book=book, name=d.name)):
        try:
            _get(url, part)
        except OSError as error:
            errors.append(f"{url}: {error}")
            continue
        got = md5(part)
        if got == d.md5:
            part.rename(target)
            return "downloaded" if url == d.url else "downloaded from the LFS mirror"
        errors.append(f"{url}: MD5 {got}, the book says {d.md5}")
    part.unlink(missing_ok=True)
    raise RuntimeError("; ".join(errors))


def fetch(downloads: list[Download], into: Path, book: str, book_source: Path, jobs: int = 4) -> list[str]:
    """Fetches every source; returns the names that couldn't be had."""
    into.mkdir(parents=True, exist_ok=True)
    failed = []
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(fetch_one, d, into, book, book_source): d for d in downloads}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            d = futures[future]
            try:
                result = future.result()
            except Exception as error:  # one failure mustn't stop the others
                failed.append(d.name)
                result = f"FAILED: {error}"
            print(f"[{done:>3}/{len(downloads)}] {d.name}: {result}", flush=True)
    return failed
