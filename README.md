# lfsbuilder

A reproducible, customizable Linux distribution builder on [Gentoo](https://www.gentoo.org/).

A **recipe** (a TOML file, written by hand or by the form) says what the system is to be: systemd or OpenRC, the CPU level and compiler flags, USE flags for the whole system and for single packages, a desktop, the packages, users, host name, language, time zone, network, the output. It pins a Gentoo stage3 and a commit of the Gentoo repository, both checked against Gentoo's signing keys, and every step of the build runs inside Docker (no `--privileged`), with a checkpoint after each one, so the same recipe gives the same system on any machine and a change rebuilds only what it affects. Gentoo's official binary packages are used where they match, so only what you customize is compiled: a GNOME desktop with Firefox builds in about 40 minutes.

When a step fails, `build --repair` looks for a fix: dependency problems are solved by asking Portage itself (`check`), anything else by Claude (through Claude Code, with no tools: it only reads the failing command and the end of the logs, and answers with a USE flag, a keyword, a build setting or a patch, which lfsbuilder checks before using). Fixes are saved beside the recipe, so the next build doesn't need Claude.

**Status:** early. Recipes build and boot in QEMU (a small systemd system, an OpenRC server with a static address, a GNOME desktop) and become live ISOs that boot on BIOS and UEFI; the form and the repair agent have first versions. Next: an installer on the ISO, a binary package server, branding. The first version, built on Linux From Scratch, is on the `lfs` branch.

```bash
docker build -t lfsbuilder-host:dev -f docker/host.Dockerfile docker
python3 -m lfsbuilder prepare recipes/example.toml   # fetch and check the stage3 and repository the recipe pins
python3 -m lfsbuilder check recipes/example.toml     # ask Portage first: USE changes needed, binaries vs compiled
python3 -m lfsbuilder build recipes/example.toml     # run the steps in Docker, a checkpoint after each (--repair: fix failures)
python3 -m lfsbuilder boot recipes/example.toml      # a disk image, booted in QEMU, with a picture of its screen
python3 -m lfsbuilder boot --console recipes/example.toml   # ... and log in (user demo, password demo; Ctrl-A X quits)
python3 -m lfsbuilder iso recipes/example.toml       # a live ISO (BIOS and UEFI), boot-tested in QEMU
python3 -m lfsbuilder password                       # a password hash for a recipe's [users]
python3 -m lfsbuilder clean                          # old checkpoints no recipe uses (--yes removes them)
```

The form (Qt, PySide6, in a virtual environment):

```bash
python3 -m venv --without-pip .venv && python3 -m pip --python .venv/bin/python install PySide6-Essentials
.venv/bin/python -m lfsbuilder.form recipes/gnome.toml
```

Tests: `python3 -m unittest discover -s tests`.

Needs Docker, Python 3.11 or later (for `tomllib`), QEMU (with KVM for speed) for the boot test, and Claude Code for `--repair`. Run `boot --console` and the form in a real terminal (Ctrl-A X can't reach QEMU through Claude Code's `!`).
