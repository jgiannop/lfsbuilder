"""The form's choices, and what each one writes into a recipe.

A desktop is a profile, packages, services, a network manager, and the USE
changes `check` found it needs with the pinned repository. A desktop is
offered only once a recipe with it has built and booted (`tested`); the
others are shown but can't be chosen yet.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Desktop:
    key: str
    label: str
    tested: bool
    profile: str  # for systemd; OpenRC gets the profile without "/systemd"
    packages: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    manager: str = "networkd"
    package_use: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def profile_for(self, init: str) -> str:
        return self.profile if init == "systemd" else self.profile.removesuffix("/systemd")


DESKTOPS = (
    Desktop("none", "None (a server or a base to build on)", True, "default/linux/amd64/23.0/systemd"),
    Desktop(
        "gnome", "GNOME", True, "default/linux/amd64/23.0/desktop/gnome/systemd",
        packages=("gnome-base/gnome-light", "gnome-base/gdm"),
        services=("gdm",),
        manager="networkmanager",
        package_use=(("net-libs/ngtcp2", ("gnutls",)),),  # Gentoo's binary samba was built against it
    ),
    Desktop("plasma", "KDE Plasma", True, "default/linux/amd64/23.0/desktop/plasma/systemd",
            packages=("kde-plasma/plasma-meta", "kde-apps/konsole", "kde-apps/dolphin"),
            services=("sddm",), manager="networkmanager"),
    Desktop("xfce", "Xfce", True, "default/linux/amd64/23.0/desktop/systemd",
            packages=("x11-base/xorg-server", "xfce-base/xfce4-meta", "x11-misc/lightdm", "x11-misc/lightdm-gtk-greeter"),
            services=("lightdm",), manager="networkmanager",
            package_use=(("dev-libs/libdbusmenu", ("gtk3",)),)),  # Xfce's panel needs it (found by check)
)


def desktop_of(profile: str) -> Desktop:
    """The desktop a recipe's profile belongs to (the plain profile: none)."""
    for d in DESKTOPS[1:]:
        if profile in (d.profile, d.profile_for("openrc")):
            return d
    return DESKTOPS[0]


@dataclasses.dataclass(frozen=True)
class Software:
    package: str
    label: str
    package_use: tuple[tuple[str, tuple[str, ...]], ...] = ()
    service: str = ""


SOFTWARE = (
    Software("www-client/firefox", "Firefox (web browser)", package_use=(("media-libs/libvpx", ("postproc",)),)),
    Software("app-office/libreoffice-bin", "LibreOffice (office suite)"),
    Software("media-video/vlc", "VLC (video player)"),
    Software("app-editors/nano", "nano (text editor)"),
    Software("app-editors/vim", "Vim (text editor)"),
    Software("app-misc/tmux", "tmux (terminal multiplexer)"),
    Software("sys-process/htop", "htop (process viewer)"),
    Software("dev-vcs/git", "Git"),
    Software("net-misc/openssh", "OpenSSH server", service="sshd"),
)

LOCALES = ("en_US.UTF-8", "en_GB.UTF-8", "el_GR.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "it_IT.UTF-8")
TIMEZONES = ("UTC", "Europe/Athens", "Europe/London", "Europe/Berlin", "Europe/Paris", "America/New_York", "America/Los_Angeles", "Asia/Tokyo")
KEYMAPS = ("us", "gr", "uk", "de", "fr", "es", "it")
