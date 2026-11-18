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
  branding    the distribution's logo (icons, /usr/share/argo) and its login
              screen: GDM's logo, SDDM's Breeze theme, LightDM's background
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

import base64
import dataclasses
import shlex
from pathlib import Path

from . import fixes as fixes_
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
        f'USE="{" ".join(r.use)}"',
        f'FEATURES="{" ".join(features)}"',
        'ACCEPT_LICENSE="-* @FREE @BINARY-REDISTRIBUTABLE"',
        f'EMERGE_DEFAULT_OPTS="{EMERGE_OPTS}"',
        'LC_MESSAGES=C.UTF-8',
    ]
    return "\n".join(lines) + "\n"


def package_use(r: Recipe) -> str:
    lines = ["# Written by lfsbuilder from the recipe.",
             # The kernel's initramfs is made by dracut through installkernel (Handbook: "Installing a distribution kernel").
             "sys-kernel/installkernel dracut"]
    # Sorted: the order they were written in (by hand or by the form) mustn't change the build.
    lines += [f"{atom} {' '.join(flags)}" for atom, flags in sorted(r.package_use.items())]
    return "\n".join(lines) + "\n"


def portage(r: Recipe) -> Step:
    script = "set -e\n"
    script += _file("/etc/portage/make.conf", make_conf(r))
    script += "rm -rf /etc/portage/package.use\n" + _file("/etc/portage/package.use/lfsbuilder", package_use(r))
    if r.binary_packages:
        script += _file("/etc/portage/binrepos.conf/gentoobinhost.conf",
                        f"[gentoobinhost]\npriority = 1\nsync-uri = {BINHOST.format(cpu=r.cpu)}\nverify-signature = true\n")
    else:
        script += "rm -f /etc/portage/binrepos.conf/gentoobinhost.conf\n"
    # Dracut runs in a container: it must not look at the machine it runs on.
    script += _file("/etc/dracut.conf.d/lfsbuilder.conf", 'hostonly="no"\ncompress="zstd"\n')
    script += f"eselect profile set {shlex.quote(r.profile)}\neselect profile show\n"
    return Step("portage", "Portage set up from the recipe", script)


def world() -> Step:
    return Step("world", "The system brought up to the repository", "set -e\nemerge --update --deep --newuse --verbose @world\n")


def package(atom: str) -> Step:
    return Step(atom.replace("/", "_"), atom, f"set -e\nemerge --noreplace --verbose {shlex.quote(atom)}\n", package=atom)


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


BRANDING = Path(__file__).resolve().parent.parent / "branding"  # the PNGs made by branding/render.sh
ARGO = "/usr/share/argo"


def _binary(path: str, data: bytes) -> str:
    """A shell command writing `data` to `path`, as base64 in the script (so it is part of the step's fingerprint)."""
    encoded = base64.encodebytes(data).decode()
    return f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])}\nbase64 -d > {shlex.quote(path)} << 'LFSBUILDER_EOF'\n{encoded}LFSBUILDER_EOF\n"


GDM_PROFILE = "user-db:user\nsystem-db:gdm\nfile-db:/usr/share/gdm/greeter-dconf-defaults\n"
USER_PROFILE = "user-db:user\nsystem-db:local\n"


def branding(r: Recipe) -> Step:
    """The logo and the login screens. The pictures go to /usr/share/argo and
    the logo is the icon "argo-linux" (os-release's LOGO, GNOME's About
    page); each login screen the recipe starts gets them: GDM its logo and
    GNOME the background as wallpaper (dconf), SDDM the Breeze theme (it
    showed its plain default) with the background, LightDM's GTK greeter
    the background. Files no package owns, so updates keep them."""
    script = "set -e\n"
    for name in ("argo-background-1920x1080.png", "argo-background-plain-1920x1080.png", "argo-wordmark-440.png",
                 "argo-wordmark-light-440.png", "argo-256.png"):
        script += _binary(f"{ARGO}/{name}", (BRANDING / name).read_bytes())
    for size in (64, 128, 256, 512):
        script += _binary(f"/usr/share/icons/hicolor/{size}x{size}/apps/argo-linux.png", (BRANDING / f"argo-{size}.png").read_bytes())
    script += f"mkdir -p /usr/share/pixmaps && cp {ARGO}/argo-256.png /usr/share/pixmaps/argo-linux.png\n"
    script += "! command -v gtk-update-icon-cache >/dev/null || gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor\n"
    background = f"{ARGO}/argo-background-1920x1080.png"
    if "gdm" in r.services:
        script += _file("/etc/dconf/profile/gdm", GDM_PROFILE)
        script += _file("/etc/dconf/db/gdm.d/01-argo", f"[org/gnome/login-screen]\nlogo='{ARGO}/argo-wordmark-light-440.png'\n")
        script += _file("/etc/dconf/profile/user", USER_PROFILE)
        script += _file("/etc/dconf/db/local.d/01-argo",
                        f"[org/gnome/desktop/background]\npicture-uri='file://{background}'\npicture-uri-dark='file://{background}'\n"
                        f"[org/gnome/desktop/screensaver]\npicture-uri='file://{background}'\n")
        script += "dconf update\n"
    if "sddm" in r.services:
        script += _file("/etc/sddm.conf.d/50-argo.conf", "[Theme]\nCurrent=breeze\n")
        # Breeze puts its clock where the background's logo is: the plain sea, and Breeze's own logo (at the bottom).
        script += _file("/usr/share/sddm/themes/breeze/theme.conf.user",
                        f"[General]\ntype=image\nbackground={ARGO}/argo-background-plain-1920x1080.png\n"
                        f"showlogo=shown\nlogo={ARGO}/argo-wordmark-light-440.png\n")
    if "lightdm" in r.services:
        # The greeter reads /etc/lightdm/lightdm-gtk-greeter.conf last, and Gentoo's sets background= (its own picture).
        script += f"sed -i 's|^#\\?background=.*|background={background}|' /etc/lightdm/lightdm-gtk-greeter.conf\n"
        script += f"grep -qx 'background={background}' /etc/lightdm/lightdm-gtk-greeter.conf\n"

    return Step("branding", f"{r.name}: logo and login screen", script)


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
    return Step("updates", f"Updates from {r.binhost}", script)


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
    packages = list(r.packages)
    if r.network["manager"] == "networkmanager" and "net-misc/networkmanager" not in packages:
        packages.append("net-misc/networkmanager")
    if r.sudo and "app-admin/sudo" not in packages:
        packages.append("app-admin/sudo")
    enabled = services(r)
    # What changes least comes first, so a change rebuilds as little as it can:
    # adding a package doesn't redo the kernel.
    steps = [portage(r), world(), kernel(r), system(r), *(package(a) for a in packages),
             *([enabled] if enabled else []), users(r), branding(r), installer(r), *([updates(r)] if r.binhost else [])]
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
