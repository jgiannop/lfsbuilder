"""The build's steps: what a recipe becomes inside the stage3's container.

Every step is a shell script written here from the recipe, run as root in a
container of the step before it. They are the Gentoo Handbook's
installation, less what Docker does (unpacking the stage3 and chrooting into
it, mounting /dev, /proc, /sys) and less what the boot step does (the
disk and the boot loader):

  portage     make.conf, package.use, the binary package host, the profile
  world       the system brought up to the pinned repository (@world)
  kernel      the kernel and its initramfs
  system      time zone, locale, keymap, network, services
  <package>   each package of the recipe, one step each, in its order
  services    the services to start at boot (the recipe's, NetworkManager)
  users       root's password, the accounts, sudo for "wheel"
  argo        the distribution's own packages (overlay.py): argo-meta, the
              recipe's packages; argo-release, os-release and the look
  installer   GRUB (BIOS and UEFI), the disk tools, and lfsbuilder-install,
              which puts the live system (iso) on a disk; last, so adding it
              changed no other step's checkpoint
  updates     only with [updates]: the distribution's own binary package
              server and its signing key, which the system trusts for
              packages from it (lfsbuilder-trust); last for the same reason

/etc/hostname and /etc/hosts are not written here: Docker mounts its own
over them in every container, so they are written when the system is
exported, from the recipe.
"""

from __future__ import annotations

import dataclasses
import shlex
from pathlib import Path

from . import binhost, fixes as fixes_, overlay
from .recipe import Recipe

EMERGE_OPTS = "--usepkg --binpkg-respect-use=y --with-bdeps=y"  # the build adds --jobs (runner.Shared)

BINHOST = "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/{cpu}/"

# Portage's own sandboxes that need new namespaces (unshare), which an
# unprivileged container doesn't allow; the container is the sandbox.
# The LD_PRELOAD sandbox ("sandbox") still runs.
NO_NAMESPACES = "-ipc-sandbox -mount-sandbox -network-sandbox -pid-sandbox"


@dataclasses.dataclass
class Step:
    name: str
    title: str
    script: str
    package: str = ""  # the package a package step installs


def _file(path: str, text: str) -> str:
    """A shell command writing `text` to `path` (quoted, so nothing in it is expanded)."""
    return f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])}\ncat > {shlex.quote(path)} << 'LFSBUILDER_EOF'\n{text.rstrip()}\nLFSBUILDER_EOF\n"


def make_conf(r: Recipe) -> str:
    features = [NO_NAMESPACES, "buildpkg"]
    if r.binary_packages:
        features += ["getbinpkg"]  # signatures are required of Gentoo's packages in binrepos.conf, not of ours
    lines = [
        "# Written by lfsbuilder from the recipe; changes here are lost at the next build.",
        f'COMMON_FLAGS="-march={r.cpu} {r.cflags}"',
        'CFLAGS="${COMMON_FLAGS}"',
        'CXXFLAGS="${COMMON_FLAGS}"',
        'FCFLAGS="${COMMON_FLAGS}"',
        'FFLAGS="${COMMON_FLAGS}"',
        f'FEATURES="{" ".join(features)}"',
        'ACCEPT_LICENSE="-* @FREE @BINARY-REDISTRIBUTABLE"',
        f'EMERGE_DEFAULT_OPTS="{EMERGE_OPTS}"',
        'LC_MESSAGES=C.UTF-8',
    ]
    return "\n".join(lines) + "\n"


def portage(r: Recipe) -> Step:
    script = "set -e\n"
    script += _file("/etc/portage/make.conf", make_conf(r))
    # USE flags (the recipe's, and per package) are the argo profile's (overlay.py), whose digest
    # is here: a change to it rebuilds from this step.
    script += f"# the argo profile: {overlay.profile_digest(r)}\n"
    script += "rm -rf /etc/portage/package.use\n"
    if r.binary_packages:
        script += _file("/etc/portage/binrepos.conf/gentoobinhost.conf",
                        f"[gentoobinhost]\npriority = 1\nsync-uri = {BINHOST.format(cpu=r.cpu)}\nverify-signature = true\n")
    else:
        script += "rm -f /etc/portage/binrepos.conf/gentoobinhost.conf\n"
    # Dracut runs in a container: it must not look at the machine it runs on.
    script += _file("/etc/dracut.conf.d/lfsbuilder.conf", 'hostonly="no"\ncompress="zstd"\n')
    # The distribution's repository, mounted read-only for the build like Gentoo's (runner.Shared).
    script += _file("/etc/portage/repos.conf/argo.conf", f"[{overlay.REPO}]\nlocation = {overlay.LOCATION}\n")
    script += f"eselect profile set {shlex.quote(overlay.profile_name(r))}\neselect profile show\n"
    return Step("portage", "Portage set up from the recipe", script)


def world() -> Step:
    return Step("world", "The system brought up to the repository", "set -e\nemerge --update --deep --newuse --verbose @world\n")


def package(atom: str) -> Step:
    # --oneshot: not in the world file; argo-meta (the argo step) holds the recipe's packages there,
    # so one taken out of the recipe goes from installed systems with --depclean.
    return Step(atom.replace("/", "_"), atom, f"set -e\nemerge --noreplace --oneshot --verbose {shlex.quote(atom)}\n", package=atom)


def kernel(r: Recipe) -> Step:
    script = (
        "set -e\n"
        f"emerge --noreplace --verbose sys-kernel/{r.kernel}\n"
        "ls -l /boot\n"
    )
    return Step("kernel", f"The kernel ({r.kernel})", script)


def system(r: Recipe) -> Step:
    language = r.locale.split(".")[0]
    charmap = r.locale.split(".", 1)[1] if "." in r.locale else "UTF-8"
    script = "set -e\n"
    script += f"ln -sf {shlex.quote('../usr/share/zoneinfo/' + r.timezone)} /etc/localtime\n"
    script += _file("/etc/locale.gen", f"{language}.{charmap} {charmap}\nC.UTF-8 UTF-8\n") + "locale-gen\n"
    if r.init == "systemd":
        script += _file("/etc/locale.conf", f"LANG={r.locale}\n")
        script += _file("/etc/vconsole.conf", f"KEYMAP={r.keymap}\n")
        if r.network["manager"] == "networkd":
            script += _file("/etc/systemd/network/50-lfsbuilder.network", network_unit(r))
            script += "systemctl enable systemd-networkd.service\n"
        script += "systemctl enable systemd-resolved.service\n"  # /etc/resolv.conf points at its stub
        script += "systemctl preset-all\n"
        script += "rm -f /etc/machine-id\n"  # each installed system makes its own at first boot
    else:
        script += f"eselect locale set {shlex.quote(r.locale)}\n"
        # A login on the serial console (commented out in Gentoo's inittab): for servers and the boot test.
        script += "sed -i -E 's|^#?s0:.*|s0:12345:respawn:/sbin/agetty -L 115200 ttyS0 vt100|' /etc/inittab\ngrep '^s0:' /etc/inittab\n"
        script += f"sed -i 's/^keymap=.*/keymap=\"{r.keymap}\"/' /etc/conf.d/keymaps\n"
        if r.network["manager"] == "netifrc":
            script += _file("/etc/conf.d/net", openrc_network(r))
            script += "ln -sf net.lo /etc/init.d/net.eth0\nrc-update add net.eth0 default\n"
    return Step("system", "Time zone, locale, keymap, network", script)


def network_unit(r: Recipe) -> str:
    lines = ["[Match]", "Name=en* eth*", "", "[Network]"]
    if r.network.get("mode") == "static":
        lines.append(f"Address={r.network['address']}")
        if r.network.get("gateway"):
            lines.append(f"Gateway={r.network['gateway']}")
        lines += [f"DNS={d}" for d in r.network.get("dns", [])]
    else:
        lines.append("DHCP=yes")
    return "\n".join(lines) + "\n"


def openrc_network(r: Recipe) -> str:
    if r.network.get("mode") == "static":
        out = f'config_eth0="{r.network["address"]}"\n'
        if r.network.get("gateway"):
            out += f'routes_eth0="default via {r.network["gateway"]}"\n'
        if r.network.get("dns"):
            out += f'dns_servers_eth0="{" ".join(r.network["dns"])}"\n'
        return out
    return 'config_eth0="dhcp"\n'


def users(r: Recipe) -> Step:
    """The accounts. The hashes are set with tracing off, so they stay out of the log."""
    script = "set -e\n"
    for a in r.accounts:
        groups = f" -G {shlex.quote(','.join(a.groups))}" if a.groups else ""
        script += (f"id {a.name} >/dev/null 2>&1 || useradd -m -s /bin/bash -c {shlex.quote(a.full_name)}{groups} {a.name}\n")
    script += "set +x\n"
    script += f"usermod -p {shlex.quote(r.root_password)} root\n" if r.root_password else "passwd -l root >/dev/null\n"
    for a in r.accounts:
        script += f"usermod -p {shlex.quote(a.password)} {a.name}\n" if a.password else f"passwd -l {a.name} >/dev/null\n"
    script += "set -x\n"
    if r.sudo:
        script += _file("/etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n") + "chmod 0440 /etc/sudoers.d/wheel\nvisudo -c\n"
    return Step("users", "Root's password, the accounts, sudo", script)


INSTALLER = Path(__file__).with_name("installer.sh")

AUTOINSTALL_UNIT = """[Unit]
Description=lfsbuilder automatic install (lfsbuilder.autoinstall=DISK on the kernel command line)
ConditionKernelCommandLine=lfsbuilder.autoinstall
After=local-fs.target systemd-udev-settle.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/lfsbuilder-install --auto
ExecStartPost=/usr/bin/systemctl poweroff
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
"""


def installer(r: Recipe) -> Step:
    """GRUB for both kinds of PC, the disk tools, the installer; and on systemd
    a service that installs by itself when the kernel command line says
    lfsbuilder.autoinstall=DISK (for automatic installs and the install test)."""
    script = "set -e\n"
    script += "grep -q '^GRUB_PLATFORMS=' /etc/portage/make.conf || echo 'GRUB_PLATFORMS=\"efi-64 pc\"' >> /etc/portage/make.conf\n"
    script += "emerge --noreplace --verbose sys-boot/grub sys-apps/gptfdisk sys-fs/dosfstools\n"
    script += _file("/usr/sbin/lfsbuilder-install", INSTALLER.read_text()) + "chmod 0755 /usr/sbin/lfsbuilder-install\n"
    if r.init == "systemd":
        script += _file("/etc/systemd/system/lfsbuilder-autoinstall.service", AUTOINSTALL_UNIT)
        script += "systemctl enable lfsbuilder-autoinstall.service\n"
    return Step("installer", "GRUB, disk tools and the installer", script)


def argo(r: Recipe) -> Step:
    """The distribution's packages (overlay.py): argo-meta, which holds the
    recipe's packages in the world file, and argo-release, which brings
    os-release, the logo, the wallpapers and the login screens. Built from
    the argo repository like any other package, so they are published
    and updated like the rest."""
    script = "set -e\n"
    script += f"# the argo packages: {overlay.packages_digest(r)}\n"
    # Built from the ebuilds every time (cheap): a binary of an earlier build with the same version
    # and USE flags may hold older pictures or files, and the newest build is what gets published.
    script += f"emerge --verbose --usepkg=n --getbinpkg=n {overlay.CATEGORY}/argo-release {overlay.meta_name(r)}\n"
    script += f"emerge --verbose --noreplace {overlay.meta_name(r)}\n"  # (the world file: only it)
    return Step("argo", f"{r.name} {r.version}: argo-release, {overlay.meta_name(r)}", script)


TRUST = Path(__file__).with_name("trust.sh")
BINHOST_KEY = "/usr/share/openpgp-keys/lfsbuilder-binhost.asc"


def updates(r: Recipe) -> Step:
    """The distribution's binary package server (python3 -m lfsbuilder
    publish/serve) as a second source of binary packages beside Gentoo's,
    for this recipe's set of them. Its packages must be signed with the
    recipe's key: lfsbuilder-trust, Portage's trust helper here, adds that
    key to the keyring getuto keeps, trusted the way getuto trusts Gentoo's."""
    script = "set -e\n"
    script += _file(BINHOST_KEY, r.binhost_key_text())
    script += _file("/usr/sbin/lfsbuilder-trust", TRUST.read_text()) + "chmod 0755 /usr/sbin/lfsbuilder-trust\n"
    script += "grep -q '^PORTAGE_TRUST_HELPER=' /etc/portage/make.conf || echo 'PORTAGE_TRUST_HELPER=\"/usr/sbin/lfsbuilder-trust\"' >> /etc/portage/make.conf\n"
    script += _file("/etc/portage/binrepos.conf/lfsbuilder.conf",
                    f"[lfsbuilder]\npriority = 2\nsync-uri = {r.binhost}{r.binary_set}/\nverify-signature = true\n")
    # The repository: the commit the recipe pins, from the same server (publish puts it there), not
    # Gentoo's latest, which would run ahead of the packages; its head must be signed by Gentoo.
    script += "emerge --noreplace --verbose dev-vcs/git\n"
    script += _file("/etc/portage/repos.conf/gentoo.conf", REPOS_CONF.format(server=r.binhost, branch=r.stem))
    script += _file("/etc/portage/repos.conf/argo.conf", ARGO_REPOS_CONF.format(
        repo=overlay.REPO, location=overlay.LOCATION, server=r.binhost, branch=r.stem, key=BINHOST_KEY,
        max_age=binhost.MAX_AGE_DAYS))
    script += _file("/etc/portage/postsync.d/argo-gentoo-pin", GENTOO_PIN_HOOK) + "chmod 0755 /etc/portage/postsync.d/argo-gentoo-pin\n"
    return Step("updates", f"Updates from {r.binhost}", script)


ARGO_REPOS_CONF = """# Written by lfsbuilder: the distribution's own repository, from its server, signed with its key.
[{repo}]
location = {location}
sync-type = git
sync-uri = {server}argo.git
sync-git-clone-extra-opts = --branch {branch} --single-branch
sync-git-verify-commit-signature = yes
sync-openpgp-key-path = {key}
# The key file is the trust anchor: ours is on no keyserver, and refreshing it from one fails the check.
sync-openpgp-key-refresh = no
# Refused when published longer ago: an old release can't be passed off as the latest.
sync-git-verify-max-age-days = {max_age}
auto-sync = yes
"""


GENTOO_PIN_HOOK = """#!/bin/sh
# Written by lfsbuilder: after every sync, Gentoo's tree must be the commit this release of the
# distribution goes with (/var/db/repos/argo/metadata/gentoo-commit; argo is signed and dated, so
# this keeps an old Gentoo tree from being sent with a current release). If it isn't, every
# package is masked until it is: an update on the wrong tree could do more harm than none.
mask=/etc/portage/package.mask/argo-gentoo-pin
pin=$(cat /var/db/repos/argo/metadata/gentoo-commit 2>/dev/null) || exit 0
head=$(git -C /var/db/repos/gentoo rev-parse HEAD 2>/dev/null || echo none)
if [ "$head" = "$pin" ]; then
    rm -f "$mask"
    exit 0
fi
mkdir -p /etc/portage/package.mask
printf '# Written by /etc/portage/postsync.d/argo-gentoo-pin: Gentoo tree %s, the release pins %s.\\n*/*\\n' "$head" "$pin" > "$mask"
echo "!!! Gentoo's tree is at $head, but this release pins $pin: every package is masked until a sync brings the right one."
exit 1
"""


REPOS_CONF = """# Written by lfsbuilder: the repository comes from the distribution's server.
[DEFAULT]
main-repo = gentoo

[gentoo]
location = /var/db/repos/gentoo
sync-type = git
sync-uri = {server}gentoo.git
sync-git-clone-extra-opts = --branch {branch} --single-branch
sync-git-verify-commit-signature = yes
sync-openpgp-key-path = /usr/share/openpgp-keys/gentoo-release.asc
auto-sync = yes
"""


def services(r: Recipe) -> Step | None:
    """The services to start at boot: the recipe's, and what its choices
    bring (NetworkManager). After the packages, since a service exists only
    once its package is installed."""
    names = list(r.services)
    if r.network["manager"] == "networkmanager" and "NetworkManager" not in names:
        names.insert(0, "NetworkManager")
    if not names:
        return None
    if r.init == "systemd":
        script = "set -e\n" + "".join(f"systemctl enable {shlex.quote(n)}\n" for n in names)
    else:
        script = "set -e\n" + "".join(f"rc-update add {shlex.quote(n)} default\n" for n in names)
    return Step("services", "Services started at boot: " + ", ".join(names), script)


def all_steps(r: Recipe) -> list[Step]:
    packages = overlay.packages(r)
    enabled = services(r)
    # What changes least comes first, so a change rebuilds as little as it can:
    # adding a package doesn't redo the kernel.
    steps = [portage(r), world(), kernel(r), system(r), *(package(a) for a in packages),
             *([enabled] if enabled else []), users(r), argo(r), installer(r), *([updates(r)] if r.binhost else [])]
    # Each fix goes in at the start of the step it was found in (fixes.py).
    names = {s.name for s in steps}
    for fix in r.fixes:
        if fix.step not in names:
            raise fixes_.FixError(f"a fix is for step {fix.step!r}, which this recipe doesn't have ({', '.join(sorted(names))})")
    for step in steps:
        mine = [f for f in r.fixes if f.step == step.name]
        if mine:
            step.script = "set -e\n" + fixes_.script(mine) + step.script.removeprefix("set -e\n")
    return steps
