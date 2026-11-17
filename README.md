# lfsbuilder

A reproducible, customizable Linux distribution builder on [Gentoo](https://www.gentoo.org/).

A **recipe** (a TOML file, later written by a form with checkboxes) says what the system is to be: systemd or OpenRC, the CPU level and compiler flags, USE flags for the whole system and for single packages, the packages, host name, language, time zone, network, the output. It pins a Gentoo stage3 and a commit of the Gentoo repository, both checked against Gentoo's signing keys, and every step of the build runs inside Docker (no `--privileged`), with a checkpoint after each one, so the same recipe gives the same system on any machine and a change rebuilds only what it affects. Gentoo's official binary packages are used where they match, so only what you customize is compiled.

Claude is only called when a step fails or a decision is needed (planned), and its fixes are stored with the recipe.

**Status:** early. A recipe becomes a Gentoo system in Docker and a disk image that is test-booted in QEMU; the agent, the ISO and the form are next. (The first version, built on Linux From Scratch, is on the `lfs` branch.)

```bash
docker build -t lfsbuilder-host:dev -f docker/host.Dockerfile docker
python3 -m lfsbuilder prepare recipes/example.toml   # fetch and check the stage3 and repository the recipe pins
python3 -m lfsbuilder plan recipes/example.toml      # the steps, and which are done
python3 -m lfsbuilder build recipes/example.toml     # run them in Docker, a checkpoint after each
python3 -m lfsbuilder boot recipes/example.toml      # a disk image of the system, booted in QEMU
```

Needs Docker, Python 3.11 or later (for `tomllib`), and QEMU (with KVM for speed) for the boot test.
