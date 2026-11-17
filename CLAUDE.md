# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

lfsbuilder is a **Linux distribution builder on Gentoo**: a recipe file says what the system is (init system, CPU level, compiler flags, USE flags, packages, language, network, output), and the build turns it into a system, in Docker, reproducibly. Later a Qt form with checkboxes writes the recipe, and the output becomes the owner's own distribution (ISO, installer, branding, a binary package server for updates). Repository: https://github.com/jgiannop/lfsbuilder (branch `master`). The name stays for now (owner's decision).

The project started on Linux From Scratch; that work (the LFS book read from its XML, the sources fetched, chapters 4–8 run in Docker) is on the branch `lfs`, kept for reference.

## Decisions taken (with the owner)

- **Gentoo, not LFS, as the base** (2026-11-17): Portage gives dependency resolution, updates and binary packages; USE flags are the form's checkboxes; Gentoo's official binary packages make builds fast; user patches (`/etc/portage/patches`) are where the agent's fixes go. The price: the start is Gentoo's prebuilt stage3, and the result is a Gentoo derivative (rebranded, per Gentoo's name and logo guidelines).
- **Everything pinned and checked**: the recipe names a stage3 build (date) and a commit of `github.com/gentoo-mirror/gentoo` (the repository with its metadata cache, every commit signed). The stage3 must be signed by Release Engineering's automated key `13EBBDBEDE7A12775DFDB1BABB572E0E2D182910`, the commit by the repository mirror key `EF9538C9E8E64311A52CDEDFA13D0EF1914E7A72`; the fingerprints are in `gentoo.py` and decide (the key file from qa-reports.gentoo.org is only where the keys come from). Gentoo deletes old stage3s after some months: the copies in `work/gentoo/` keep old recipes buildable (later: an archive the owner chooses).
- **systemd by default** (GNOME needs it); `openrc` is a recipe option (written, not yet tested).
- **Gentoo's binary packages on by default**, required to be signed (`verify-signature = true` in `binrepos.conf`; not `FEATURES=binpkg-request-signature`, which also rejects our own unsigned packages). A package is compiled when its USE flags differ. Custom `cflags` don't stop binary packages being used: for compiler flags to apply everywhere, `binary_packages = false`.
- **Docker as the build host, without `--privileged`**: a `--privileged` container is not a security boundary, and an agent running commands it chose must be sandboxed. Portage's namespace sandboxes are off (they need unshare); its LD_PRELOAD sandbox stays.
- **Checkpoints after every step** as Docker images named by a fingerprint (hash chain of the stage3's SHA512, the repository commit and every step's script); each package of the recipe is its own step. Portage's binary packages (`buildpkg`) in a volume per CPU level and flags are the package-level cache.
- **The agent repairs, the recipe stays the truth**: Claude is only called when a step fails or a decision is needed, with the failing command and the end of the log, never whole logs (gigabytes); its fixes are stored with the recipe (USE changes, `package.accept_keywords`, user patches) so the next build is reproducible without it.
- **Making it bootable**: QEMU with the kernel and initramfs given directly; the disk image is made by `mke2fs -d` from a tar (no loop devices, no root). GRUB, partitions and the ISO come later. A running VirtualBox VM holds VT-x on the owner's i7-3770, and KVM guests then die with `KVM: entry failed, hardware error 0x0`; `boot_test()` sees that and boots again without KVM (slower).

## Phases

1. **A system that boots** (done 2026-11-17): recipe, `prepare` (stage3 + repository, checked), `build` (portage set-up, @world, packages, kernel, system configuration), `boot` (disk image, QEMU test). The example recipe builds in about 7 minutes with binary packages (the kernel step's dracut is most of it) and boots to `login:` in ~7 s under KVM. Users (`[users]`: root's password hash, `[[users.accounts]]`, sudo for wheel) added. Next: static network and OpenRC tested.
2. **The agent**: Claude called on failure with the step, the failing command and the end of the log; proposes a fix, retries, records it with the recipe.
3. **Distribution**: own binary package server (the binpkgs), users, root password, ISO with installer, branding, GRUB/systemd-boot.
4. **The Qt form**: writes the recipe; shows what a choice pulls in (Portage's dependency resolution) and how long it builds.

## Layout

- `lfsbuilder/recipe.py`: loads and checks a recipe (TOML, `tomllib`). Everything that ends up in a shell script or `make.conf` is checked against a pattern first. Passwords are only ever crypt hashes (`$6$`, `$y$`, ...), made by `python3 -m lfsbuilder password` (openssl in the tools image, the password on stdin); recipes go into git. The example's `demo` user has the hash of "demo", for QEMU only.
- `lfsbuilder/gentoo.py`: `keyring()`, `stage3()` (download, verify `.asc`), `repository()` (git fetch of one commit, `verify-commit`, `git archive` to a plain tree), `stage3_image()` (`docker import` of the stage3). gpg and git run in the tools image (`docker/host.Dockerfile`, `lfsbuilder-host:dev`).
- `lfsbuilder/steps.py`: the steps as shell scripts written from the recipe, least-changing first (so adding a package doesn't redo the kernel): `portage` (make.conf, package.use, binrepos.conf, dracut config, profile), `world`, `kernel` (`gentoo-kernel-bin` with `installkernel[dracut]`), `system` (time zone, locale, keymap, network, services), one per package (plus `app-admin/sudo` if `users.sudo`), `users` (accounts; hashes set under `set +x` so they stay out of logs; locked when empty). /etc/hostname, hosts, resolv.conf are Docker's in a container: they are written at export.
- `lfsbuilder/runner.py`: runs steps (`bash -l`, `set -x` with `PS4='### lfsbuilder: '` so the log shows the failing command), commits each as `lfsbuilder-build:<fingerprint>`, skips steps whose image exists, flattens images over 100 layers. Mounts: the repository read-only at /var/db/repos/gentoo, volumes `lfsbuilder-distfiles` and `lfsbuilder-binpkgs-<cpu>-<hash of flags>`. `MAKEOPTS` from `jobs` is passed in the environment (not fingerprinted).
- `lfsbuilder/image.py`: `export()` (docker export → `rootfs.tar`, Docker's files replaced from the recipe, fstab added, kernel/initramfs copied out), `disk()` (`mke2fs -d rootfs.tar`), `boot_test()` (QEMU, waits for `login:` on the serial console).
- `lfsbuilder/__main__.py`: `python3 -m lfsbuilder prepare|plan|build|boot RECIPE` (`build --until <step name>`, `boot --console` to log in on the serial console, Ctrl-A X quits), `python3 -m lfsbuilder password`. Work files in `work/` (ignored by git): `gentoo/{keys,stage3,repos}`, `build-<recipe>/{logs,boot,rootfs.tar,disk.img}`.
- `recipes/example.toml`: the example recipe.

## How to work here

- **Ask the owner before every commit.** Never add Claude attribution or co-author lines to commits or PRs. Commits use the owner's GitHub noreply address (already the git config); the owner's real email address is private.
- Installing packages on the host (`sudo apt install …`) is the owner's to run; suggest the command.
- Builds can take long (hours without binary packages, a desktop more): say so before starting one, and run it in the background.
- The owner prefers plain-text questions and direct action.
