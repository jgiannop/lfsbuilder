"""The LFS book as a list of steps.

The book's XML source marks every command: `remap` says what kind it is
(pre, configure, make, test, install, adjust, clean), `role="nodump"` marks
one that is only shown (entering the chroot, a backup), `revision` keeps a
part to the systemd or the SysVinit edition, and each package section names
its source archive in a `sect1info`. Those markings are what the LFS
project's own jhalfs reads; this is the same idea in Python.

Resolving the book (its includes and the version names, `&gcc-version;`) is
xmllint's job, run in the host image (see `resolve_command`), so this module
only reads the result.
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from pathlib import Path

FIRST_BUILD_CHAPTER = 5  # chapters before it prepare the host; the build starts here
SOURCES_CHAPTER = 3  # "Packages and Patches": what the book's wget-list and md5sums are made from
BOOTSCRIPTS_MD5 = "BOOTSCRIPTS-MD5SUM"  # the book makes this archive from its own tree


@dataclasses.dataclass
class Download:
    url: str
    md5: str

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def from_book(self) -> bool:
        """The LFS bootscripts (SysVinit only): the book packs them from its
        own bootscripts/ directory, so their checksum is only known then."""
        return self.md5 == BOOTSCRIPTS_MD5


@dataclasses.dataclass
class Command:
    kind: str  # pre, configure, make, test, install, adjust, clean, or "" (unmarked)
    text: str
    needs: list[str] = dataclasses.field(default_factory=list)  # values the user must give (<replaceable>)


@dataclasses.dataclass
class Step:
    chapter: int
    number: str  # "5.2", as the book numbers it
    id: str  # the book's id, "ch-tools-binutils-pass1"
    title: str  # "Binutils-2.47 - Pass 1"
    package: str = ""  # "binutils-pass1"
    version: str = ""
    url: str = ""  # its source archive; empty: a step that unpacks nothing
    commands: list[Command] = dataclasses.field(default_factory=list)

    @property
    def archive(self) -> str:
        return self.url.rsplit("/", 1)[-1] if self.url else ""

    def needs(self) -> list[str]:
        return sorted({n for c in self.commands for n in c.needs})


def write_conditions(book: Path, init: str) -> None:
    """The two files the book's own Makefile writes before it is read: which
    edition (systemd or sysv) is included, and the book's version name."""
    if init not in ("systemd", "sysv"):
        raise ValueError(f"init must be systemd or sysv, not {init!r}")
    (book / "conditional.ent").write_text(
        f'<!ENTITY % sysv    "{"INCLUDE" if init == "sysv" else "IGNORE "}">\n'
        f'<!ENTITY % systemd "{"INCLUDE" if init == "systemd" else "IGNORE "}">\n'
    )
    (book / "version.ent").write_text(
        '<![ %sysv; [\n<!ENTITY version "lfsbuilder">\n]]>\n'
        '<![ %systemd; [\n<!ENTITY version "lfsbuilder-systemd">\n]]>\n'
        '<!ENTITY releasedate "">\n<!ENTITY copyrightdate "">\n'
    )


def resolve_command(book_in_container: str, out_in_container: str) -> list[str]:
    """xmllint resolving the book into one file; run in the host image."""
    return ["xmllint", "--nonet", "--loaddtd", "--noent", "--xinclude", "--dropdtd", "--output", out_in_container, f"{book_in_container}/index.xml"]


def _text(element: ET.Element) -> tuple[str, list[str]]:
    """A command as it is to be typed; its <replaceable> parts are returned
    too, since the recipe has to give them."""
    needs: list[str] = []

    def walk(e: ET.Element) -> str:
        out = e.text or ""
        for child in e:
            if child.tag == "replaceable":
                needs.append(" ".join("".join(child.itertext()).split()))
            out += walk(child) + (child.tail or "")
        return out

    return walk(element), needs


def _commands(element: ET.Element, init: str, out: list[Command]) -> None:
    """Every command under `element` in the book's order, leaving out the
    other edition's parts and what is only shown (nodump)."""
    for child in element:
        if child.get("revision") not in (None, init):
            continue
        if child.tag == "screen":
            if child.get("role") == "nodump":
                continue
            for user in child.findall("userinput"):  # a nested userinput (a user name) is part of its command
                text, needs = _text(user)
                if text.strip():
                    out.append(Command(user.get("remap", ""), text.strip("\n"), needs))
        else:
            _commands(child, init, out)


def read(resolved: Path, init: str, first_chapter: int = FIRST_BUILD_CHAPTER) -> list[Step]:
    """The book's build steps (chapter 5 on, unless asked), in order, for one edition."""
    root = ET.parse(resolved).getroot()
    steps: list[Step] = []
    for position, chapter in enumerate(root.iter("chapter"), start=1):
        if chapter.get("revision") not in (None, init):
            continue
        index = 0
        for sect in chapter:
            if sect.tag != "sect1" or sect.get("revision") not in (None, init):
                continue
            index += 1
            if position < first_chapter:
                continue
            title = sect.find("title")
            step = Step(
                chapter=position,
                number=f"{position}.{index}",
                id=sect.get("id", ""),
                title=" ".join("".join(title.itertext()).split()) if title is not None else "",
            )
            info = sect.find("sect1info")
            if info is not None:
                step.package = (info.findtext("productname") or "").strip()
                step.version = (info.findtext("productnumber") or "").strip()
                step.url = (info.findtext("address") or "").strip()
            _commands(sect, init, step.commands)
            steps.append(step)
    return steps


def downloads(resolved: Path, init: str) -> list[Download]:
    """The book's wget-list and md5sums for one edition, as its Makefile
    makes them (stylesheets/wget-list.xsl, md5sum.xsl): every list item of
    chapter 3 with a "Download" link, and the MD5 sum beside it."""
    root = ET.parse(resolved).getroot()
    chapter = list(root.iter("chapter"))[SOURCES_CHAPTER - 1]
    out: list[Download] = []

    def walk(element: ET.Element) -> None:
        for child in element:
            if child.get("revision") not in (None, init) or child.get("condition") == "pdf":
                continue
            if child.tag == "listitem":
                url = md5 = ""
                for para in child.findall("para"):
                    link = para.find("ulink")
                    if link is not None and "Download" in (para.text or ""):
                        url = link.get("url", "").split("?download")[0]
                    if para.find("literal") is not None:
                        md5 = (para.findtext("literal") or "").strip()
                if url:
                    out.append(Download(url, md5))
            else:
                walk(child)

    walk(chapter)
    return out
