"""lfsbuilder's command line.

    python3 -m lfsbuilder prepare RECIPE   fetch the book the recipe names and read it
    python3 -m lfsbuilder plan RECIPE      the steps the build would run, and what is still undecided
    python3 -m lfsbuilder fetch RECIPE     download the sources the book lists and check their MD5 sums
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import book, recipe as recipes, sources

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
SOURCES = WORK / "sources"  # shared by every build; a file is here only if its checksum matched
HOST_IMAGE = "lfsbuilder-host:dev"


def book_dir(r: recipes.Recipe) -> Path:
    return WORK / f"book-{r.book}-{r.init}"


def resolved_path(r: recipes.Recipe) -> Path:
    return book_dir(r) / "resolved.xml"


def in_host(*command: str) -> None:
    """Runs a command in the host image, with work/ shared as /work, as the user."""
    subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{WORK}:/work", HOST_IMAGE, *command],
        check=True,
    )


def prepare(r: recipes.Recipe) -> None:
    source = book_dir(r) / "source"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", f"r{r.book}", "https://git.linuxfromscratch.org/lfs.git", str(source)],
            check=True,
        )
    book.write_conditions(source, r.init)
    inside = f"/work/{book_dir(r).name}"
    in_host(*book.resolve_command(f"{inside}/source", f"{inside}/resolved.xml"))
    print(f"read the LFS {r.book} book ({r.init}) into {resolved_path(r).relative_to(ROOT)}")


def plan(r: recipes.Recipe) -> int:
    if not resolved_path(r).exists():
        print(f"the book isn't read yet: python3 -m lfsbuilder prepare {r.path}", file=sys.stderr)
        return 1
    steps = book.read(resolved_path(r), r.init)
    known = r.values()
    undecided: dict[str, list[str]] = {}
    for step in steps:
        kinds = [c.kind or "-" for c in step.commands if r.run_tests or c.kind != "test"]
        if not kinds:
            continue
        print(f"{step.number:>6}  {step.title[:44]:44}  {step.archive[:32]:32}  {len(kinds)} commands")
        for need in step.needs():
            if need not in known:
                undecided.setdefault(need, []).append(step.number)
    if undecided:
        print("\nStill undecided (the book leaves these to you; the recipe doesn't say them yet):")
        for need, where in undecided.items():
            print(f"  {need:40} in {', '.join(where)}")
    return 0


def fetch(r: recipes.Recipe) -> int:
    if not resolved_path(r).exists():
        print(f"the book isn't read yet: python3 -m lfsbuilder prepare {r.path}", file=sys.stderr)
        return 1
    wanted = book.downloads(resolved_path(r), r.init)
    failed = sources.fetch(wanted, SOURCES, r.book, book_dir(r) / "source")
    if failed:
        print(f"\n{len(failed)} of {len(wanted)} sources couldn't be had: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nall {len(wanted)} sources are in {SOURCES.relative_to(ROOT)}, their MD5 sums as the book gives them")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lfsbuilder")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "plan", "fetch"):
        sub.add_parser(name).add_argument("recipe", type=Path)
    args = parser.parse_args(argv)
    try:
        r = recipes.load(args.recipe)
    except recipes.RecipeError as error:
        print(error, file=sys.stderr)
        return 2
    if args.command == "prepare":
        prepare(r)
        return 0
    if args.command == "fetch":
        return fetch(r)
    return plan(r)


if __name__ == "__main__":
    sys.exit(main())
