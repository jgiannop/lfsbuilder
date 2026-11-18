"""The distribution's own repository ("argo"), written from a recipe: what
carries the recipe to systems already installed.

A built system gets Gentoo's updates from the tree (synced from our server)
and the binary packages; but the recipe itself (its USE flags, its packages,
its look, its version) lived only in the build. This repository carries it,
in Gentoo's own ways:

  profiles/argo/<recipe>        a profile on top of the recipe's Gentoo
                                profile: its USE flags (make.defaults) and
                                USE flags per package (package.use); the
                                system's profile, so a change reaches it
                                with the next sync
  argo-base/argo-release        os-release, the logo, the wallpapers, the
                                login screens (what was the branding step):
                                one package for every recipe, the desktop
                                chosen by its USE flags (gdm, sddm,
                                lightdm, plasma, xfce), which the profile
                                sets; recipes share one set of binary
                                packages, and Portage tells their builds
                                apart by USE
  argo-base/argo-<recipe>       depends on the recipe's packages: the one
                                package in the system's world file, so a
                                package added to the recipe comes with
                                `emerge -uDN @world` and one taken out goes
                                with `emerge --depclean`; named after the
                                recipe, since its contents differ

Both carry the recipe's version: a release that changes them must raise it,
or installed systems won't see the change.

During builds it is mounted read-only at /var/db/repos/argo (like Gentoo's
tree); systems sync it from <server>argo.git, a branch per recipe, its
commits signed with the package signing key (binhost.publish_overlay).
`files()` is pure: the build's fingerprints take digests of it
(profile_digest, packages_digest).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .recipe import Recipe

REPO = "argo"
CATEGORY = "argo-base"
LOCATION = "/var/db/repos/argo"
BRANDING = Path(__file__).resolve().parent.parent / "branding"  # the PNGs made by branding/render.sh
ARGO = "/usr/share/argo"
PICTURES = ("argo-background-1920x1080.png", "argo-background-plain-1920x1080.png", "argo-wordmark-440.png",
            "argo-wordmark-light-440.png", "argo-grub-1024x768.png", "argo-64.png", "argo-128.png", "argo-256.png",
            "argo-512.png")
VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")  # a Portage version, kept simple

PLASMA_LNF = "org.argolinux.desktop"


class OverlayError(ValueError):
    pass


def profile_name(r: Recipe) -> str:
    return f"{REPO}:argo/{r.stem}"


def meta_name(r: Recipe) -> str:
    return f"{CATEGORY}/argo-{r.stem}"


DESKTOP_FLAGS = ("gdm", "sddm", "lightdm", "plasma", "xfce")


def release_flags(r: Recipe) -> list[str]:
    """argo-release's USE flags for the recipe: its login screen and desktop."""
    flags = [s for s in ("gdm", "sddm", "lightdm") if s in r.services]
    if "kde-plasma/plasma-meta" in r.packages:
        flags.append("plasma")
    if "xfce-base/xfce4-meta" in r.packages:
        flags.append("xfce")
    return flags


def packages(r: Recipe) -> list[str]:
    """The recipe's packages as the build installs them (steps.all_steps): its
    own, NetworkManager when chosen, sudo for the wheel group."""
    atoms = list(r.packages)
    if r.network["manager"] == "networkmanager" and "net-misc/networkmanager" not in atoms:
        atoms.append("net-misc/networkmanager")
    if r.sudo and "app-admin/sudo" not in atoms:
        atoms.append("app-admin/sudo")
    return atoms


def _package_use(r: Recipe) -> str:
    lines = ["# Written by lfsbuilder from the recipe.",
             # The kernel's initramfs is made by dracut through installkernel (Handbook: "Installing a distribution kernel").
             "sys-kernel/installkernel dracut"]
    # Sorted: the order they were written in (by hand or by the form) mustn't change the build.
    lines += [f"{atom} {' '.join(flags)}" for atom, flags in sorted(r.package_use.items())]
    flags = release_flags(r)
    if flags:  # (off by default)
        lines.append(f"{CATEGORY}/argo-release {' '.join(flags)}")
    return "\n".join(lines) + "\n"


def _profile(r: Recipe) -> dict[str, bytes]:
    base = f"profiles/argo/{r.stem}"
    return {
        "profiles/repo_name": f"{REPO}\n".encode(),
        "profiles/categories": f"{CATEGORY}\n".encode(),
        "profiles/profiles.desc": f"amd64 argo/{r.stem} stable\n".encode(),
        "metadata/layout.conf": ("masters = gentoo\nthin-manifests = true\nsign-manifests = false\n"
                                 "profile-formats = portage-2\ncache-formats =\n").encode(),
        f"{base}/parent": f"gentoo:{r.profile}\n".encode(),
        f"{base}/make.defaults": f'# Written by lfsbuilder from the recipe.\nUSE="{" ".join(r.use)}"\n'.encode(),
        f"{base}/package.use": _package_use(r).encode(),
    }


def _os_release(r: Recipe) -> str:
    return "".join(f"{k}={json.dumps(v)}\n" for k, v in (
        ("NAME", r.name), ("ID", r.os_id), ("ID_LIKE", "gentoo"), ("VERSION", r.version), ("VERSION_ID", r.version),
        ("PRETTY_NAME", f"{r.name} {r.version}"), ("ANSI_COLOR", "1;34"), ("LOGO", "argo-linux")))


GDM_PROFILE = "user-db:user\nsystem-db:gdm\nfile-db:/usr/share/gdm/greeter-dconf-defaults\n"
USER_PROFILE = "user-db:user\nsystem-db:local\n"
PLASMA_WALLPAPER = {"KPlugin": {"Id": "Argo", "Name": "Argo Linux", "License": "CC-BY-SA-4.0"}}
PLASMA_LNF_METADATA = {"KPackageStructure": "Plasma/LookAndFeel",
                       "KPlugin": {"Id": PLASMA_LNF, "Name": "Argo Linux", "Description": "Breeze, with Argo Linux's wallpaper",
                                   "License": "GPL-2.0-or-later"}}
# Breeze's look-and-feel defaults (plasma-workspace), with our wallpaper; what isn't here falls back to Breeze.
PLASMA_LNF_DEFAULTS = """[kdeglobals][KDE]
widgetStyle=Breeze

[kdeglobals][General]
ColorScheme=BreezeLight

[kdeglobals][Icons]
Theme=breeze

[plasmarc][Theme]
name=default

[Wallpaper]
Image=Argo

[kcminputrc][Mouse]
cursorTheme=breeze_cursors

[kwinrc][org.kde.kdecoration2]
library=org.kde.breeze
theme=Breeze

[ksplashrc][KSplash]
Theme=org.kde.breeze.desktop
"""
XFDESKTOP_HOOK = """# Written by lfsbuilder (argo-base/argo-release): Argo Linux's wallpaper as
# xfdesktop's default, the file built into it, at every install of xfdesktop.
argo_wallpaper() {
    local dir="${ED%/}/usr/share/backgrounds/xfce"
    mkdir -p "$dir"
    rm -f "$dir/xfce-x.svg"
    # The picture inside the SVG (a data: URI): GdkPixbuf, which xfdesktop loads it with, doesn't
    # know the file's directory, so a picture beside it isn't found (a black desktop).
    {
        printf '%s' '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><image width="1920" height="1080" href="data:image/png;base64,'
        base64 -w0 /usr/share/argo/argo-background-1920x1080.png
        printf '%s\\n' '"/></svg>'
    } > "$dir/xfce-x.svg"
}
post_src_install() { argo_wallpaper; }
"""


def _release(r: Recipe) -> dict[str, bytes]:
    """argo-release: its ebuild and files, the same for every recipe of the
    distribution (only the name and version come from it); each desktop's
    part under its USE flag."""
    d = f"{CATEGORY}/argo-release"
    files: dict[str, bytes] = {f"{d}/files/{name}": (BRANDING / name).read_bytes() for name in PICTURES}
    files[f"{d}/files/os-release"] = _os_release(r).encode()
    files[f"{d}/files/issue"] = f"\n{r.name} {r.version} (\\s \\m \\r) \\t\n\n".encode()  # agetty's \s etc.
    background = f"{ARGO}/argo-background-1920x1080.png"
    files[f"{d}/files/gdm.profile"] = GDM_PROFILE.encode()
    files[f"{d}/files/user.profile"] = USER_PROFILE.encode()
    files[f"{d}/files/gdm-01-argo"] = f"[org/gnome/login-screen]\nlogo='{ARGO}/argo-wordmark-light-440.png'\n".encode()
    files[f"{d}/files/local-01-argo"] = (
        f"[org/gnome/desktop/background]\npicture-uri='file://{background}'\npicture-uri-dark='file://{background}'\n"
        f"[org/gnome/desktop/screensaver]\npicture-uri='file://{background}'\n").encode()
    # Breeze puts its clock where the background's logo is: the plain sea, and Breeze's own logo (at the bottom).
    files[f"{d}/files/sddm-50-argo.conf"] = b"[Theme]\nCurrent=breeze\n"
    files[f"{d}/files/breeze-theme.conf.user"] = (
        f"[General]\ntype=image\nbackground={ARGO}/argo-background-plain-1920x1080.png\n"
        f"showlogo=shown\nlogo={ARGO}/argo-wordmark-light-440.png\n").encode()
    files[f"{d}/files/wallpaper-metadata.json"] = (json.dumps(PLASMA_WALLPAPER, indent=4) + "\n").encode()
    files[f"{d}/files/lnf-metadata.json"] = (json.dumps(PLASMA_LNF_METADATA, indent=4) + "\n").encode()
    files[f"{d}/files/lnf-defaults"] = PLASMA_LNF_DEFAULTS.encode()
    files[f"{d}/files/xfdesktop.env"] = XFDESKTOP_HOOK.encode()
    lnf = f"/usr/share/plasma/look-and-feel/{PLASMA_LNF}"
    pictures = "\n".join(f'\tdoins "${{FILESDIR}}"/{name}' for name in PICTURES)
    ebuild = f"""# Written by lfsbuilder: the release package of {r.name}.
EAPI=8

inherit xdg-utils

DESCRIPTION="{r.name} {r.version}: os-release, the logo, the wallpapers and the login screens"
HOMEPAGE="https://github.com/jgiannop/lfsbuilder"
S="${{WORKDIR}}"

LICENSE="CC-BY-SA-4.0"
SLOT="0"
KEYWORDS="amd64"
IUSE="{' '.join(DESKTOP_FLAGS)}"

src_install() {{
\tinsinto {ARGO}
{pictures}
\tlocal s
\tfor s in 64 128 256 512; do
\t\tinsinto /usr/share/icons/hicolor/${{s}}x${{s}}/apps
\t\tnewins "${{FILESDIR}}"/argo-${{s}}.png argo-linux.png
\tdone
\tinsinto /usr/share/pixmaps
\tnewins "${{FILESDIR}}"/argo-256.png argo-linux.png
\tinsinto /usr/lib/argo
\tdoins "${{FILESDIR}}"/os-release "${{FILESDIR}}"/issue
\tif use gdm; then
\t\tinsinto /etc/dconf/profile
\t\tnewins "${{FILESDIR}}"/gdm.profile gdm
\t\tnewins "${{FILESDIR}}"/user.profile user
\t\tinsinto /etc/dconf/db/gdm.d
\t\tnewins "${{FILESDIR}}"/gdm-01-argo 01-argo
\t\tinsinto /etc/dconf/db/local.d
\t\tnewins "${{FILESDIR}}"/local-01-argo 01-argo
\tfi
\tif use sddm; then
\t\tinsinto /etc/sddm.conf.d
\t\tnewins "${{FILESDIR}}"/sddm-50-argo.conf 50-argo.conf
\t\tinsinto /usr/share/sddm/themes/breeze
\t\tnewins "${{FILESDIR}}"/breeze-theme.conf.user theme.conf.user
\tfi
\tif use plasma; then
\t\t# Plasma's default wallpaper is the look-and-feel package's [Wallpaper] Image: a package of our
\t\t# own (Breeze's defaults with our wallpaper; the rest falls back to Breeze), chosen system-wide.
\t\tinsinto /usr/share/wallpapers/Argo
\t\tnewins "${{FILESDIR}}"/wallpaper-metadata.json metadata.json
\t\tinsinto /usr/share/wallpapers/Argo/contents/images
\t\tnewins "${{FILESDIR}}"/argo-background-1920x1080.png 1920x1080.png
\t\tinsinto {lnf}
\t\tnewins "${{FILESDIR}}"/lnf-metadata.json metadata.json
\t\tinsinto {lnf}/contents
\t\tnewins "${{FILESDIR}}"/lnf-defaults defaults
\tfi
\tif use xfce; then
\t\t# xfdesktop's default wallpaper is a file of its own, built into it: ours in its place, now and
\t\t# (a Portage hook, run as the package is installed) at every update of xfdesktop.
\t\tinsinto /etc/portage/env/xfce-base
\t\tnewins "${{FILESDIR}}"/xfdesktop.env xfdesktop
\tfi
}}

pkg_postinst() {{
\t# A file in place of baselayout's link to /usr/lib/os-release: /etc is protected, so
\t# baselayout's updates leave it (a ._cfg file at most), and this package's update rewrites it.
\trm -f "${{EROOT}}"/etc/os-release
\tcp "${{EROOT}}"/usr/lib/argo/os-release "${{EROOT}}"/etc/os-release
\tcp "${{EROOT}}"/usr/lib/argo/issue "${{EROOT}}"/etc/issue
\tif use gdm; then
\t\tdconf update || ewarn "dconf update failed"
\tfi
\tif use lightdm; then
\t\t# The greeter reads its main file last, and Gentoo's sets background= (its own picture).
\t\tsed -i 's|^#\\?background=.*|background={background}|' "${{EROOT}}"/etc/lightdm/lightdm-gtk-greeter.conf
\tfi
\tif use plasma; then
\t\tlocal kdeglobals="${{EROOT}}"/etc/xdg/kdeglobals
\t\ttouch "${{kdeglobals}}"
\t\tif grep -q "^LookAndFeelPackage=" "${{kdeglobals}}"; then
\t\t\tsed -i "s/^LookAndFeelPackage=.*/LookAndFeelPackage={PLASMA_LNF}/" "${{kdeglobals}}"
\t\telse
\t\t\tprintf '\\n[KDE]\\nLookAndFeelPackage={PLASMA_LNF}\\n' >> "${{kdeglobals}}"
\t\tfi
\tfi
\tif use xfce; then
\t\t( ED="${{EROOT}}/"; source "${{EROOT}}"/etc/portage/env/xfce-base/xfdesktop; argo_wallpaper )
\tfi
\txdg_icon_cache_update
}}

pkg_postrm() {{
\txdg_icon_cache_update
}}
"""
    files[f"{d}/argo-release-{r.version}.ebuild"] = ebuild.encode()
    return files


def _meta(r: Recipe) -> dict[str, bytes]:
    depends = "\n".join(f"\t{atom}" for atom in [f"{CATEGORY}/argo-release", *packages(r)])
    ebuild = f"""# Written by lfsbuilder from the recipe {r.path.name}.
EAPI=8

DESCRIPTION="{r.name} {r.version}: the packages of the recipe {r.stem}"
HOMEPAGE="https://github.com/jgiannop/lfsbuilder"
S="${{WORKDIR}}"

LICENSE="metapackage"
SLOT="0"
KEYWORDS="amd64"

RDEPEND="
{depends}
"
"""
    name = meta_name(r).split("/")[1]
    return {f"{CATEGORY}/{name}/{name}-{r.version}.ebuild": ebuild.encode()}


PN = re.compile(r"^[a-z0-9][a-z0-9_]*$")  # the recipe's name, in argo-<recipe>: no "-" (Portage would read a version)


def files(r: Recipe) -> dict[str, bytes]:
    """The whole repository for the recipe: path -> content."""
    if not PN.match(r.stem):
        raise OverlayError(f"the recipe's file name {r.stem!r} names its package argo-{r.stem}: lower-case letters, digits and _")
    if not VERSION.match(r.version):
        raise OverlayError(f"distribution.version {r.version!r} is the version of Argo's packages too: "
                           "numbers with dots, like 0.1 or 1.2.3")
    return {**_profile(r), **_release(r), **_meta(r)}


def _digest(items: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(items):
        h.update(path.encode() + b"\0" + hashlib.sha256(items[path]).hexdigest().encode() + b"\n")
    return h.hexdigest()


def profile_digest(r: Recipe) -> str:
    """Of what the profile holds: in the portage step's script, so a change rebuilds from there."""
    return _digest({p: c for p, c in files(r).items() if p.startswith(("profiles/", "metadata/"))})


def packages_digest(r: Recipe) -> str:
    """Of argo-release and argo-meta: in the argo step's script."""
    return _digest({p: c for p, c in files(r).items() if p.startswith(f"{CATEGORY}/")})


def write(r: Recipe, into: Path) -> Path:
    """Writes the repository to `into` (replacing what was there, at once)."""
    new = into.with_name(into.name + ".new")
    old = into.with_name(into.name + ".old")
    for leftover in (new, old):
        shutil.rmtree(leftover, ignore_errors=True)
    for path, content in files(r).items():
        target = new / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if into.exists():
        into.rename(old)
    new.rename(into)
    shutil.rmtree(old, ignore_errors=True)
    return into
