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

1. **LFS base without the form** (in progress): recipe format and `prepare`/`plan` (done); downloading and checking the sources, `fetch` (done); the runner with checkpoints, `build`, chapters 4–6 (done) and 7–8 in the imported image (written, being tested); then chapters 9–10 from the recipe, a kernel for QEMU, a boot test in QEMU.
2. **The agent**: Claude called on failure with the step, the book's text for it and the end of the log; proposes a fix, retries, records it.
3. **Packages and caching.**
4. **BLFS groups and the Qt form**, with dependency resolution from BLFS's XML (required/recommended/optional) and estimated build time from the book's SBUs.
5. **ISO, installer, branding.**

## Layout

- `lfsbuilder/book.py`: reads the resolved book into `Step`s (`number`, `title`, `package`, `version`, `url`, `commands` with `kind` = the book's `remap`: pre, configure, make, test, install, adjust, clean). `write_conditions()` writes `conditional.ent`/`version.ent` (what the book's Makefile does: which edition is included); `resolve_command()` is the xmllint call, run in the host image.
- `book.downloads()` reads chapter 3 the way the book's `wget-list.xsl`/`md5sum.xsl` do, for one edition (systemd 94 files, sysv 98).
- `lfsbuilder/sources.py`: `fetch` into `work/sources/` (shared by all builds); a file is kept only if its MD5 matches; the upstream URL first, then the LFS mirror (`ftp.osuosl.org/pub/lfs/lfs-packages/<version>/`). The SysVinit bootscripts archive is packed from the book's `bootscripts/` reproducibly (fixed owner, time, order), as the book's `make-aux-files.sh` does.
- `lfsbuilder/runner.py`: `build`. Each step runs in a container from the previous step's image and is committed on success: a checkpoint is an image `lfsbuilder-build:<fingerprint>` (a hash chain of the host image id, the book, the sources' MD5s and every step's script; not `jobs`), so a re-run skips steps whose image exists and a changed step rebuilds from there on. The build tree lives only in images. A step's script: unpack its archive in the sources dir, the book's commands each after an `echo '### lfsbuilder: command N (kind)'` marker, delete the tree; `set -e`. Modes: `host-root` (setup 3.0, chapter 4 until `ch-preps-settingenviron`, 7.2–7.3), `host-lfs` (4.4–6, as `lfs` under `env -i` with `~/.bashrc` sourced), `import` (7.4: `copy_tree()` = docker export | rewrite the tar keeping `mnt/lfs/` as `/` | docker import), `chroot` (7.5 on, root, 7.4's `env -i` environment, `--hostname` from the recipe). Left out of the book: `source ~/.bash_profile` (interactive), 7.3's mount commands (Docker mounts /dev, /proc, /sys, /dev/shm), kind `locale-full` (an alternative). Docker's own files: `/etc/mtab` (link to /proc/mounts) is removed before a chroot step so 7.6 can make the book's; `/etc/hosts`, `hostname`, `resolv.conf` are mounted over by Docker, so a step that writes one leaves a copy in `/var/lib/lfsbuilder/etc/`, which the exported system must get (and `ln -sf` onto them fails: chapter 9's resolv.conf link must be done at export). A setup step (3.0) does what chapters 2–3 leave to the reader: `$LFS/sources` with links to `work/sources` (mounted read-only at `/lfs-sources` in every container), host `/etc/bash.bashrc` moved aside. Images deeper than 100 layers are flattened in place before a chroot step (overlay2's limit is ~125). Logs: `work/build-<recipe>/logs/<number>-<id>.log`; on failure `failure_report()` prints the failing command and the log's last 30 lines (what the agent will get).
- `lfsbuilder/recipe.py`: loads and checks a recipe (TOML, `tomllib`).
- `lfsbuilder/__main__.py`: `python3 -m lfsbuilder prepare|plan|fetch|build RECIPE` (`build --until 5.2` stops after a step). Work files go to `work/` (ignored by git): `work/book-<version>-<init>/source` (the book's git checkout) and `resolved.xml`.
- `recipes/example.toml`: the example recipe.

On LFS 13.1 systemd the reader finds 143 sections from chapter 5 on and 581 commands.

## How to work here

- **Ask the owner before every commit.** Never add Claude attribution or co-author lines to commits or PRs. Commits use the owner's GitHub noreply address (already the git config); the owner's real email address is private.
- Installing packages on the host (`sudo apt install …`) is the owner's to run; suggest the command.
- A full build compiles for hours and needs ~30 GB: say so before starting one, and run it in the background.
- The owner prefers plain-text questions and direct action.
