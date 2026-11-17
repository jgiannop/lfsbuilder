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
            for user in child.iter("userinput"):
                text, needs = _text(user)
                if text.strip():
                    out.append(Command(user.get("remap", ""), text.strip("\n"), needs))
        else:
            _commands(child, init, out)


def read(resolved: Path, init: str) -> list[Step]:
    """The book's build steps (chapter 5 on), in order, for one edition."""
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
            if position < FIRST_BUILD_CHAPTER:
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
