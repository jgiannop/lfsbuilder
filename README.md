# lfsbuilder

A reproducible, customizable [Linux From Scratch](https://www.linuxfromscratch.org/) build: the base for your own distribution.

A **recipe** (a TOML file, later written by a form with checkboxes) says what the system is to be: the LFS book version, systemd or SysVinit, host name, language, time zone, network, what to install, the output. The build reads only the recipe, the book it names and the sources the book lists, and runs every step inside Docker, so the same recipe gives the same system on any machine.

The steps come from the LFS book's own XML source, which marks every command (configure, make, test, install, not-to-be-run) and separates the systemd and SysVinit editions; Claude is only called when a step fails or a decision is needed.

**Status:** early. The book is read and planned and its sources are fetched; the build itself is the next step.

```bash
docker build -t lfsbuilder-host:dev -f docker/host.Dockerfile docker
python3 -m lfsbuilder prepare recipes/example.toml   # fetch and read the book the recipe names
python3 -m lfsbuilder plan recipes/example.toml      # the steps it would run, and what is undecided
python3 -m lfsbuilder fetch recipes/example.toml     # download the sources the book lists, checked by MD5
```

Needs Docker and Python 3.11 or later (for `tomllib`); QEMU for the boot test.
