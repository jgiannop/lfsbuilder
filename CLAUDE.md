# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

lfsbuilder builds a **reproducible, customizable Linux From Scratch system as the base for the owner's own distribution**. A recipe file says what the system is; the build reads only the recipe, the LFS book it names and the sources the book lists, and runs in Docker. Later a Qt form with checkboxes writes the recipe. Repository: https://github.com/jgiannop/lfsbuilder (branch `master`).

## Decisions taken (with the owner)

- **The book drives, Claude repairs.** Commands come from the LFS book's XML source (git `https://git.linuxfromscratch.org/lfs.git`, tag `r<version>`), never retyped by an AI. Claude is only called when a step fails or a decision is needed, and its fixes are stored as patches/overrides with the recipe so the next build is reproducible without it. Claude never reads whole build logs (they are gigabytes): only the failing step and the end of its log.
- **LFS 13.1, systemd edition** by default (`init` in the recipe can be `sysv`). systemd because GNOME and most desktop software need it.
- **Docker as the build host** (`docker/host.Dockerfile`, Debian 13 with LFS chapter 2's requirements, `docbook-xml` for resolving the book). Chapters 5–6 run as an unprivileged user in it.
- **Chapter 7's chroot without `--privileged`**: import the half-built `$LFS` tree as a Docker image and continue chapters 7–8 in a container of it (Docker supplies /dev, /proc, /sys). A `--privileged` container is not a security boundary, and an agent running commands it chose must be sandboxed; if a step really needs privileges, it goes in a VM instead.
- **Checkpoints after every package**, so a failure restarts from the last good package. Later: a cache keyed by a fingerprint of each package's inputs, so changing one checkbox rebuilds only what depends on it.
- **Making it bootable**: test-boot in QEMU with the kernel given directly; GRUB and a real disk image need loop devices, so that final step goes in a VM or uses tools that don't need them.
- **A package manager is needed for a real distribution** (updates): each package installed into its own DESTDIR and packed; reuse an existing manager (pacman, xbps or dpkg) rather than writing one. Phase 3.
- **The book's `<replaceable>` placeholders** are filled from the recipe (`Recipe.values()`); steps where the book only gives an example the recipe can't fill (static network, fstab partitions, GRUB) get versions written by lfsbuilder from the recipe and the output format. That override mechanism is next.

## Phases

1. **LFS base without the form** (in progress): recipe format and `prepare`/`plan` (done); then download and checksum the sources (`wget-list`, `md5sums` of the book), the runner with checkpoints, chapters 7–8 in the imported image, chapters 9–10 from the recipe, a kernel for QEMU, a boot test in QEMU.
2. **The agent**: Claude called on failure with the step, the book's text for it and the end of the log; proposes a fix, retries, records it.
3. **Packages and caching.**
4. **BLFS groups and the Qt form**, with dependency resolution from BLFS's XML (required/recommended/optional) and estimated build time from the book's SBUs.
5. **ISO, installer, branding.**

## Layout

- `lfsbuilder/book.py`: reads the resolved book into `Step`s (`number`, `title`, `package`, `version`, `url`, `commands` with `kind` = the book's `remap`: pre, configure, make, test, install, adjust, clean). `write_conditions()` writes `conditional.ent`/`version.ent` (what the book's Makefile does: which edition is included); `resolve_command()` is the xmllint call, run in the host image.
- `lfsbuilder/recipe.py`: loads and checks a recipe (TOML, `tomllib`).
- `lfsbuilder/__main__.py`: `python3 -m lfsbuilder prepare|plan RECIPE`. Work files go to `work/` (ignored by git): `work/book-<version>-<init>/source` (the book's git checkout) and `resolved.xml`.
- `recipes/example.toml`: the example recipe.

On LFS 13.1 systemd the reader finds 143 sections from chapter 5 on and 583 commands.

## How to work here

- **Ask the owner before every commit.** Never add Claude attribution or co-author lines to commits or PRs. Commits use the owner's GitHub noreply address (already the git config); the owner's real email address is private.
- Installing packages on the host (`sudo apt install …`) is the owner's to run; suggest the command.
- A full build compiles for hours and needs ~30 GB: say so before starting one, and run it in the background.
- The owner prefers plain-text questions and direct action.
