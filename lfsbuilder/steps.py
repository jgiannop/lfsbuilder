"""The build's steps: what a recipe becomes inside the stage3's container.

Every step is a shell script written here from the recipe, run as root in a
container of the step before it. They are the Gentoo Handbook's
installation, less what Docker does (unpacking the stage3 and chrooting into
it, mounting /dev, /proc, /sys) and less what the boot step does (the
disk and the boot loader):

  portage     make.conf, package.use, the binary package host, the profile
  world       the system brought up to the pinned repository (@world)
  <package>   each package of the recipe, one step each, in its order
  kernel      the kernel and its initramfs
  system      time zone, locale, keymap, network, services

/etc/hostname and /etc/hosts are not written here: Docker mounts its own
over them in every container, so they are written when the system is
exported, from the recipe.
"""

from __future__ import annotations

import dataclasses
import shlex

from .recipe import Recipe

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
        'EMERGE_DEFAULT_OPTS="--usepkg --binpkg-respect-use=y --with-bdeps=y"',
        'LC_MESSAGES=C.UTF-8',
    ]
    return "\n".join(lines) + "\n"


def package_use(r: Recipe) -> str:
    lines = ["# Written by lfsbuilder from the recipe.",
             # The kernel's initramfs is made by dracut through installkernel (Handbook: "Installing a distribution kernel").
             "sys-kernel/installkernel dracut"]
    lines += [f"{atom} {' '.join(flags)}" for atom, flags in r.package_use.items()]
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
    return Step(atom.replace("/", "_"), atom, f"set -e\nemerge --noreplace --verbose {shlex.quote(atom)}\n")


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
        script += _file("/etc/systemd/network/50-lfsbuilder.network", network_unit(r))
        script += "systemctl enable systemd-networkd.service systemd-resolved.service\n"
        script += "systemctl preset-all\n"
        script += "rm -f /etc/machine-id\n"  # each installed system makes its own at first boot
    else:
        script += f"eselect locale set {shlex.quote(r.locale)}\n"
        script += f"sed -i 's/^keymap=.*/keymap=\"{r.keymap}\"/' /etc/conf.d/keymaps\n"
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


def all_steps(r: Recipe) -> list[Step]:
    return [portage(r), world(), *(package(a) for a in r.packages), kernel(r), system(r)]
