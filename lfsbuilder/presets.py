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
    group: str = "System tools"


# The groups the form shows the software in, in this order.
GROUPS = ("Internet", "Office", "Multimedia", "Graphics", "Editors and development", "System tools",
          "Terminal and utilities", "KDE apps", "Security tools")

# Every package here has a stable amd64 version in the pinned tree (checked 2026-11-19 against
# e19f4a84) and a licence Gentoo accepts by default. The order is the recipe's package order (the
# first nine came first): new ones go at the end, or saved recipes would reorder their steps.
SOFTWARE = (
    Software("www-client/firefox", "Firefox (web browser)", package_use=(("media-libs/libvpx", ("postproc",)),),
             group="Internet"),
    Software("app-office/libreoffice-bin", "LibreOffice (office suite)", group="Office"),
    Software("media-video/vlc", "VLC (video player)", group="Multimedia"),
    Software("app-editors/nano", "nano (text editor)", group="Editors and development"),
    Software("app-editors/vim", "Vim (text editor)", group="Editors and development"),
    Software("app-misc/tmux", "tmux (terminal multiplexer)", group="Terminal and utilities"),
    Software("sys-process/htop", "htop (process viewer)"),
    Software("dev-vcs/git", "Git (version control)", group="Editors and development"),
    Software("net-misc/openssh", "OpenSSH server", service="sshd"),
    # Internet
    Software("www-client/firefox-bin", "Firefox, Mozilla's own build (nothing to compile)", group="Internet"),
    Software("mail-client/thunderbird-bin", "Thunderbird (email)", group="Internet"),
    Software("net-p2p/transmission", "Transmission (torrents)", package_use=(("net-p2p/transmission", ("gtk",)),),
             group="Internet"),
    Software("net-p2p/qbittorrent", "qBittorrent (torrents)", group="Internet"),
    Software("net-misc/remmina", "Remmina (remote desktop)", group="Internet"),
    Software("net-ftp/filezilla", "FileZilla (FTP)", group="Internet"),
    Software("net-im/pidgin", "Pidgin (chat)", group="Internet"),
    Software("net-im/signal-desktop-bin", "Signal (messenger)", group="Internet"),
    # Office
    Software("app-office/onlyoffice-bin", "ONLYOFFICE (office suite)", group="Office"),
    Software("app-office/gnumeric", "Gnumeric (spreadsheet)", group="Office"),
    Software("app-office/abiword", "AbiWord (word processor)", group="Office"),
    Software("app-text/evince", "Evince (PDF viewer)", group="Office"),
    Software("app-text/zathura", "Zathura (light PDF viewer)", group="Office"),
    Software("kde-apps/okular", "Okular (PDF viewer, KDE)", group="Office"),
    Software("app-text/calibre", "Calibre (e-books)", group="Office"),
    # Multimedia
    Software("media-video/mpv", "mpv (video player)", group="Multimedia"),
    Software("media-video/celluloid", "Celluloid (video player)", group="Multimedia"),
    Software("media-sound/audacious", "Audacious (music player)", group="Multimedia"),
    Software("media-sound/rhythmbox", "Rhythmbox (music library)", group="Multimedia"),
    Software("media-sound/strawberry", "Strawberry (music library)", group="Multimedia"),
    Software("media-sound/audacity", "Audacity (audio editor)", group="Multimedia"),
    Software("media-video/handbrake", "HandBrake (video converter)", group="Multimedia"),
    Software("media-sound/pavucontrol", "PulseAudio volume control", group="Multimedia"),
    # Graphics
    Software("media-gfx/gimp", "GIMP (image editor)", group="Graphics"),
    Software("media-gfx/inkscape", "Inkscape (vector drawing)", group="Graphics"),
    Software("media-gfx/blender", "Blender (3D)", group="Graphics"),
    Software("media-gfx/darktable", "darktable (photo editing)", group="Graphics"),
    Software("media-gfx/shotwell", "Shotwell (photo library)", group="Graphics"),
    Software("media-gfx/gthumb", "gThumb (image viewer)", group="Graphics"),
    Software("media-gfx/ristretto", "Ristretto (image viewer, Xfce)", group="Graphics"),
    Software("media-gfx/flameshot", "Flameshot (screenshots)", group="Graphics"),
    # Editors and development
    Software("app-editors/vscodium", "VSCodium (code editor)", group="Editors and development"),
    Software("app-editors/neovim", "Neovim (text editor)", group="Editors and development"),
    Software("app-editors/emacs", "Emacs (text editor)", group="Editors and development"),
    Software("app-editors/gedit", "gedit (text editor, GNOME)", group="Editors and development"),
    Software("app-editors/mousepad", "Mousepad (text editor, Xfce)", group="Editors and development"),
    Software("dev-util/meld", "Meld (compare files)", group="Editors and development"),
    Software("dev-lang/go", "Go (language)", group="Editors and development"),
    Software("dev-lang/rust-bin", "Rust (language)", group="Editors and development"),
    Software("dev-lang/php", "PHP (language)", group="Editors and development"),
    Software("app-containers/podman", "Podman (containers)", group="Editors and development"),
    Software("app-containers/docker", "Docker (containers)", service="docker", group="Editors and development"),
    Software("app-emulation/qemu", "QEMU (virtual machines)", group="Editors and development"),
    # System tools
    Software("sys-process/btop", "btop (system monitor)"),
    Software("sys-block/gparted", "GParted (partition editor)"),
    Software("sys-apps/gnome-disk-utility", "GNOME Disks"),
    Software("app-backup/deja-dup", "Déjà Dup (backups)"),
    Software("sys-apps/smartmontools", "smartmontools (disk health)"),
    Software("sys-fs/ntfs3g", "NTFS (Windows disks)"),
    Software("sys-apps/flatpak", "Flatpak (more apps from Flathub)"),
    Software("app-admin/keepassxc", "KeePassXC (passwords)"),
    Software("net-print/cups", "CUPS (printing)", service="cups"),
    Software("app-misc/mc", "Midnight Commander (files)"),
    # Terminal and utilities
    Software("x11-terms/alacritty", "Alacritty (terminal)", group="Terminal and utilities"),
    Software("x11-terms/kitty", "kitty (terminal)", group="Terminal and utilities"),
    Software("app-misc/screen", "GNU screen", group="Terminal and utilities"),
    Software("app-misc/fastfetch", "fastfetch (system info)", group="Terminal and utilities"),
    Software("app-misc/ranger", "ranger (file manager)", group="Terminal and utilities"),
    Software("net-misc/wget", "wget (downloads)", group="Terminal and utilities"),
    Software("net-misc/curl", "curl (downloads)", group="Terminal and utilities"),
    Software("net-misc/rsync", "rsync (copy files)", group="Terminal and utilities"),
    Software("app-text/tree", "tree (folders as a tree)", group="Terminal and utilities"),
    Software("sys-apps/ripgrep", "ripgrep (search text)", group="Terminal and utilities"),
    Software("sys-apps/fd", "fd (find files)", group="Terminal and utilities"),
    Software("app-misc/jq", "jq (JSON)", group="Terminal and utilities"),
    Software("app-arch/7zip", "7-Zip (archives)", group="Terminal and utilities"),
    Software("app-arch/zip", "zip", group="Terminal and utilities"),
    Software("app-arch/unzip", "unzip", group="Terminal and utilities"),
    Software("net-analyzer/nmap", "Nmap (network scanner)", group="Terminal and utilities"),
    Software("net-analyzer/wireshark", "Wireshark (network analyser)", group="Terminal and utilities"),
    Software("net-vpn/wireguard-tools", "WireGuard (VPN)", group="Terminal and utilities"),
    Software("net-vpn/openvpn", "OpenVPN (VPN)", group="Terminal and utilities"),
    # KDE apps (checked 2026-11-19 against e19f4a84: KDE Gear 26.08, stable); best with Plasma, they run elsewhere too
    Software("kde-apps/kcalc", "KCalc (calculator)", group="KDE apps"),
    Software("kde-apps/kate", "Kate (text and code editor)", group="KDE apps"),
    Software("kde-apps/kwrite", "KWrite (simple text editor)", group="KDE apps"),
    Software("kde-apps/gwenview", "Gwenview (image viewer)", group="KDE apps"),
    Software("kde-plasma/spectacle", "Spectacle (screenshots)", group="KDE apps"),
    Software("kde-apps/ark", "Ark (archives: zip, tar…)", group="KDE apps"),
    Software("kde-apps/kolourpaint", "KolourPaint (simple painting)", group="KDE apps"),
    Software("media-sound/elisa", "Elisa (music player)", group="KDE apps"),
    Software("media-video/haruna", "Haruna (video player)", group="KDE apps"),
    Software("kde-apps/kdenlive", "Kdenlive (video editor)", group="KDE apps"),
    Software("sys-block/partitionmanager", "KDE Partition Manager (disks)", group="KDE apps"),
    Software("kde-apps/filelight", "Filelight (what fills the disk)", group="KDE apps"),
    Software("kde-apps/kfind", "KFind (find files)", group="KDE apps"),
    Software("kde-apps/kcharselect", "KCharSelect (special characters)", group="KDE apps"),
    Software("kde-apps/sweeper", "Sweeper (clean traces)", group="KDE apps"),
    Software("kde-misc/kdeconnect", "KDE Connect (your phone and the PC)", group="KDE apps"),
    Software("kde-apps/kmail", "KMail (email)", group="KDE apps"),
    Software("kde-apps/korganizer", "KOrganizer (calendar)", group="KDE apps"),
    Software("net-irc/konversation", "Konversation (IRC chat)", group="KDE apps"),
    Software("kde-apps/krdc", "KRDC (remote desktop)", group="KDE apps"),
    Software("net-p2p/ktorrent", "KTorrent (torrents)", group="KDE apps"),
    Software("kde-apps/kompare", "Kompare (compare files)", group="KDE apps"),
    Software("kde-apps/yakuake", "Yakuake (drop-down terminal)", group="KDE apps"),
    Software("kde-misc/kclock", "KClock (clocks and alarms)", group="KDE apps"),
    Software("kde-misc/kweather", "KWeather (weather)", group="KDE apps"),
    Software("kde-apps/kpat", "KPatience (card games)", group="KDE apps"),
    Software("kde-apps/kmines", "KMines (minesweeper)", group="KDE apps"),
    Software("kde-apps/kmahjongg", "KMahjongg (mahjong)", group="KDE apps"),
    # Security tools (Kali's best known that the pinned tree has, stable; checked 2026-11-19 against e19f4a84).
    # For your own systems, CTFs, learning and authorized testing only.
    # Left out, testing only (~amd64) in e19f4a84 by Portage's own answer (portageq best_visible): masscan, reaver,
    # johntheripper-jumbo, hashcat, crunch, nikto, radare2, volatility3, lynis; kismet: needs an older Python;
    # mitmproxy: needs a Rust with eBPF sysroots (rust_sysroots_bpf) the tree's rust-bin lacks.
    Software("net-analyzer/tcpdump", "tcpdump (capture network traffic)", group="Security tools"),
    Software("net-analyzer/ettercap", "Ettercap (network interception)", group="Security tools"),
    Software("net-analyzer/openbsd-netcat", "netcat (network Swiss army knife)", group="Security tools"),
    Software("net-misc/socat", "socat (connect anything to anything)", group="Security tools"),
    Software("net-wireless/aircrack-ng", "Aircrack-ng (Wi-Fi security)", group="Security tools"),
    Software("net-analyzer/hydra", "Hydra (login testing)", group="Security tools"),
    Software("dev-db/sqlmap", "sqlmap (SQL injection testing)", group="Security tools"),
    Software("dev-util/rizin", "rizin (reverse engineering)", group="Security tools"),
    Software("app-misc/binwalk", "binwalk (firmware analysis)", group="Security tools"),
    Software("app-forensics/sleuthkit", "The Sleuth Kit (disk forensics)", group="Security tools"),
    Software("app-forensics/yara", "YARA (malware patterns)", group="Security tools"),
    Software("app-forensics/foremost", "foremost (recover files)", group="Security tools"),
    Software("app-admin/testdisk", "TestDisk and PhotoRec (recover partitions and files)", group="Security tools"),
    Software("app-forensics/chkrootkit", "chkrootkit (look for rootkits)", group="Security tools"),
    Software("app-forensics/rkhunter", "rkhunter (look for rootkits)", group="Security tools"),
    Software("net-vpn/tor", "Tor (anonymity network)", group="Security tools"),
    Software("net-proxy/torsocks", "torsocks (run a program through Tor)", group="Security tools"),
)

LOCALES = ("en_US.UTF-8", "en_GB.UTF-8", "el_GR.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "it_IT.UTF-8")
TIMEZONES = ("UTC", "Europe/Athens", "Europe/London", "Europe/Berlin", "Europe/Paris", "America/New_York", "America/Los_Angeles", "Asia/Tokyo")
KEYMAPS = ("us", "gr", "uk", "de", "fr", "es", "it")
