"""The graphical installer: Calamares on the live desktop, set up from the
recipe ([installer] graphical = true; desktops only, systemd).

Calamares asks for the language, the keyboard, the disk and the user, then
copies the live system to the disk from where dmsquash-live has the ISO's
SquashFS mounted, /run/rootfsbase (as the text installer, installer.sh,
does; with rsync: Gentoo's unsquashfs is built without zstd, which our
SquashFS is compressed with), and sets it up with its standard modules.
What Argo adds, in shell scripts it runs (`shellprocess`):

  - before the new user is made: the recipe's accounts are removed from the
    copy (they are the live system's: `demo`), and the live-only files
    (the launcher, its sudo rule, the automatic-install service);
  - before GRUB is installed: GRUB's menu name and the Argo background, with
    Gentoo's theme off (as installer.sh does).

On the live system, "Install Argo Linux" (menu entry) runs `argo-install`,
which starts Calamares as root through sudo without a password: a rule for
the live system only (polkit's pkexec would need an agent, which Xfce has
none of, and drops the display's environment).

`files()` is pure (tested); steps.installer() writes them.
"""

from __future__ import annotations

import json

from .overlay import ARGO, BRANDING
from .recipe import Recipe

CONFIG = "/etc/calamares"
LAUNCHER = "/usr/share/applications/argo-install.desktop"
# Calamares' own menu entry ("Install System", through pkexec: Xfce has no polkit agent).
OWN_LAUNCHER = "/usr/share/applications/calamares.desktop"
# "zz-": sudo reads sudoers.d in name order and the last matching rule wins; Gentoo's `wheel` (a password)
# would come after "argo-...".
SUDO_RULE = "/etc/sudoers.d/zz-argo-live-installer"
WRAPPER = "/usr/bin/argo-install"
LIVE_ONLY = (LAUNCHER, SUDO_RULE, WRAPPER, "/etc/systemd/system/multi-user.target.wants/lfsbuilder-autoinstall.service")
LIVE_ROOT = "/run/rootfsbase/"  # the ISO's SquashFS, mounted by dmsquash-live (the trailing /: its contents)
# The USE changes Calamares needs (found with emerge --pretend, 2026-11-18): written in the installer
# step's own package.use file, so no other step's fingerprint changes.
PACKAGE_USE = ("dev-libs/boost python", "dev-libs/libpwquality python", "sys-boot/grub mount")
PACKAGES = ("app-admin/calamares", "sys-boot/efibootmgr")
# Pictures of /usr/share/argo (argo-release) the branding names, linked into its directory.
PICTURES = ("argo-64.png", "argo-wordmark-440.png", "argo-background-plain-1920x1080.png")
# The side bar's logo: the ship in light colours (the dark one doesn't show on the side bar); written
# into the branding directory by the installer step, so the argo packages stay as they are.
LIGHT_LOGO = BRANDING / "argo-light-256.png"
DISPLAY_MANAGERS = ("gdm", "sddm", "lightdm")

SIDEBAR, SIDEBAR_CURRENT, SAIL, TEXT = "#16325a", "#1d4a80", "#f2b544", "#ffffff"  # argo-background.svg's


def _yaml(value) -> str:
    """A YAML scalar: JSON's quoting is valid YAML."""
    return json.dumps(value)


def display_manager(r: Recipe) -> str:
    return next(s for s in r.services if s in DISPLAY_MANAGERS)


def settings() -> str:
    return f"""# Written by lfsbuilder (calamares.py).
---
modules-search: [ local ]

instances:
- id: live
  module: shellprocess
  config: shellprocess-live.conf
- id: argo
  module: shellprocess
  config: shellprocess-argo.conf

sequence:
- show:
  - welcome
  - locale
  - keyboard
  - partition
  - users
  - summary
- exec:
  - partition
  - mount
  - unpackfs
  - machineid
  - fstab
  - locale
  - keyboard
  - localecfg
  - shellprocess@live
  - users
  - displaymanager
  - networkcfg
  - hwclock
  - shellprocess@argo
  - bootloader
  - umount
- show:
  - finished

branding: argo
prompt-install: true
dont-chroot: false
oem-setup: false
disable-cancel: false
disable-cancel-during-exec: false
hide-back-and-next-during-exec: true
quit-at-end: false
"""


def branding(r: Recipe) -> str:
    strings = {
        "productName": r.name, "shortProductName": r.short_name, "version": r.version, "shortVersion": r.version,
        "versionedName": f"{r.name} {r.version}", "shortVersionedName": f"{r.short_name} {r.version}",
        "bootloaderEntryName": r.short_name, "productUrl": "", "supportUrl": "", "knownIssuesUrl": "",
        "releaseNotesUrl": "", "donateUrl": "",
    }
    lines = "\n".join(f"    {key}: {_yaml(value)}" for key, value in strings.items())
    return f"""# Written by lfsbuilder (calamares.py).
---
componentName: argo
welcomeStyleCalamares: false
welcomeExpandingLogo: true
windowExpanding: normal
windowSize: 960px,640px
windowPlacement: center
sidebar: widget
navigation: widget

strings:
{lines}

images:
    productLogo: "argo-light-256.png"
    productIcon: "argo-64.png"
    productWelcome: "argo-wordmark-440.png"

slideshow: "show.qml"
slideshowAPI: 2

style:
    SidebarBackground: "{SIDEBAR}"
    SidebarText: "{TEXT}"
    SidebarTextCurrent: "{SAIL}"
    SidebarBackgroundCurrent: "{SIDEBAR_CURRENT}"
"""


def slideshow(r: Recipe) -> str:
    slides = [
        f"{r.name} {r.version} is being installed.",
        f"{r.name} is built on Gentoo: its packages, its updates, its USE flags.",
        "Updates come from the distribution's own server, every package signed.",
    ]
    body = "\n".join(f"""    Slide {{
        Image {{ anchors.fill: parent; source: "argo-background-plain-1920x1080.png"; fillMode: Image.PreserveAspectCrop }}
        Text {{
            anchors.centerIn: parent; width: parent.width * 0.8
            text: {_yaml(text)}
            color: "{TEXT}"; font.pixelSize: 24; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
        }}
    }}""" for text in slides)
    return f"""// Written by lfsbuilder (calamares.py).
import QtQuick
import calamares.slideshow 1.0

Presentation {{
    id: presentation
    Timer {{
        interval: 8000; repeat: true; running: presentation.activatedInCalamares
        onTriggered: presentation.goToNextSlide()
    }}
{body}
    function onActivate() {{ }}
    function onLeave() {{ }}
}}
"""


def modules(r: Recipe) -> dict[str, str]:
    region, _, zone = r.timezone.partition("/")
    head = "# Written by lfsbuilder (calamares.py).\n---\n"
    accounts = " ".join(a.name for a in r.accounts)
    return {
        "welcome.conf": head + """showSupportUrl: false
showKnownIssuesUrl: false
showReleaseNotesUrl: false
showDonateUrl: false
requirements:
    requiredStorage: 20
    requiredRam: 2.0
    check: [ storage, ram, root, screen ]
    required: [ storage, ram, root ]
""",
        "locale.conf": head + f"""region: {_yaml(region or "Europe")}
zone: {_yaml(zone or "London")}
localeGenPath: "/etc/locale.gen"
""",
        "keyboard.conf": head + """xOrgConfFileName: "/etc/X11/xorg.conf.d/00-keyboard.conf"
convertedKeymapPath: "/lib/kbd/keymaps/xkb"
writeEtcDefaultKeyboard: false
""",
        # The partition table: Calamares' default, GPT on UEFI PCs and MBR on BIOS ones.
        "partition.conf": head + """efi:
    mountPoint: "/boot/efi"
    recommendedSize: 512MiB
    minimumSize: 300MiB
userSwapChoices: [ none, file ]
initialSwapChoice: none
defaultFileSystemType: "ext4"
availableFileSystemTypes: [ "ext4" ]
# Tested: ext4, unencrypted (the installed initramfs isn't made to unlock a disk).
enableLuksAutomatedPartitioning: false
alwaysShowPartitionLabels: true
""",
        "unpackfs.conf": head + f"""unpack:
    - source: "{LIVE_ROOT}"
      sourcefs: "file"
      destination: ""
""",
        "machineid.conf": head + """systemd: true
dbus: true
dbus-symlink: true
""",
        "users.conf": head + """defaultGroups:
    - name: users
      must_exist: true
      system: true
    - wheel
    - audio
    - video
    - input
autoLoginGroup: autologin
doAutologin: false
sudoersGroup: wheel
setRootPassword: true
doReusePassword: true
passwordRequirements:
    minLength: 1
    maxLength: -1
allowWeakPasswords: true
allowWeakPasswordsDefault: true
user:
    shell: /bin/bash
    forbidden_names: [ root ]
hostname:
    location: EtcFile
    writeHostsFile: true
""",
        "displaymanager.conf": head + f"""displaymanagers:
    - {display_manager(r)}
basicSetup: false
sysconfigSetup: false
""",
        "bootloader.conf": head + f"""efiBootLoader: "grub"
grubInstall: "grub-install"
grubMkconfig: "grub-mkconfig"
grubCfg: "/boot/grub/grub.cfg"
grubProbe: "grub-probe"
efiBootMgr: "efibootmgr"
efiBootloaderId: {_yaml(r.os_id)}
installEFIFallback: true
""",
        "shellprocess-live.conf": head + f"""dontChroot: false
timeout: 120
script:
    - "/usr/libexec/argo-calamares live {accounts}"
i18n:
    name: "Removing the live system's accounts"
""",
        "shellprocess-argo.conf": head + """dontChroot: false
timeout: 120
script:
    - "/usr/libexec/argo-calamares grub"
i18n:
    name: "Setting up the boot menu"
""",
        "finished.conf": head + """restartNowEnabled: true
restartNowChecked: true
restartNowCommand: "systemctl -i reboot"
""",
    }


def helper(r: Recipe) -> str:
    """The script the shellprocess instances run in the new system (chroot)."""
    return f"""#!/bin/bash
# argo-calamares: what Calamares' own modules don't do for {r.name} (calamares.py).
#   argo-calamares live USER...   remove the live system's accounts and its live-only files
#   argo-calamares grub           the boot menu's name and picture
set -euo pipefail
case "${{1:-}}" in
    live)
        shift
        for user in "$@"; do
            if id "$user" >/dev/null 2>&1; then userdel -r "$user" 2>/dev/null || userdel "$user"; fi
        done
        rm -f {" ".join(LIVE_ONLY)}
        ;;
    grub)
        sed -i 's|^#\\?GRUB_DISTRIBUTOR=.*|GRUB_DISTRIBUTOR="{r.short_name}"|' /etc/default/grub
        if [ -f {ARGO}/argo-grub-1024x768.png ]; then
            mkdir -p /boot/grub
            cp {ARGO}/argo-grub-1024x768.png /boot/grub/argo.png
            sed -i 's|^#\\?GRUB_BACKGROUND=.*|GRUB_BACKGROUND="/boot/grub/argo.png"|' /etc/default/grub
            sed -i 's|^GRUB_THEME=|#GRUB_THEME=|' /etc/default/grub
        fi
        ;;
    *) echo "usage: argo-calamares live USER... | grub" >&2; exit 2 ;;
esac
"""


def wrapper() -> str:
    return """#!/bin/sh
# argo-install: Calamares as root on the live user's display (calamares.py). Live system only.
exec sudo -n --preserve-env=DISPLAY,XAUTHORITY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,XDG_SESSION_TYPE,XDG_CURRENT_DESKTOP \\
    /usr/bin/calamares "$@"
"""


def launcher(r: Recipe) -> str:
    return f"""[Desktop Entry]
Type=Application
Name=Install {r.name}
Comment=Put {r.name} on this computer's disk
Exec={WRAPPER}
Icon=argo-linux
Terminal=false
Categories=System;
Keywords=installer;calamares;install;
"""


def files(r: Recipe) -> dict[str, str]:
    """Every file the graphical installer adds to the system: path -> text."""
    out = {f"{CONFIG}/settings.conf": settings(),
           f"{CONFIG}/branding/argo/branding.desc": branding(r),
           f"{CONFIG}/branding/argo/show.qml": slideshow(r),
           "/usr/libexec/argo-calamares": helper(r),
           WRAPPER: wrapper(),
           LAUNCHER: launcher(r),
           SUDO_RULE: "# The live system's installer, without a password (calamares.py); not on installed systems.\n"
                      "%wheel ALL=(root) NOPASSWD:SETENV: /usr/bin/calamares\n"}
    out.update({f"{CONFIG}/modules/{name}": text for name, text in modules(r).items()})
    return out


EXECUTABLE = ("/usr/libexec/argo-calamares", WRAPPER)
