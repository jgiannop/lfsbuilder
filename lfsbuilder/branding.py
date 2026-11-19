"""The distribution's pictures, made from its two drawings and its label.

A distribution's look is a folder, `branding/<name>/` (the recipe's
`[distribution] branding`, "argo" when it names none), with two drawings:

  logo.svg        the logo for light backgrounds (icons, the installer)
  logo-light.svg  the same for dark backgrounds (login screens, the boot menu)

and the label, the words beside the logo (`[distribution] label`, else the
name): its first word bold, the rest regular, in Noto Sans. From these, every
picture the system uses is composed here: the logo at four sizes, the
wordmarks (logo + label, dark and light), the login screens' night sea with
the wordmark and without it, and GRUB's background. The label is measured
(fontTools, with the fonts that draw it), so any label fits.

They are drawn by rsvg-convert in the tools image and kept in git beside the
drawings, `branding/<name>/pictures/<label>/`, with a stamp of what they were
made from: the build's fingerprints come from these files, and a picture
drawn again on another machine (other fonts, another rsvg) could differ by a
pixel, so they are drawn once and committed. `ensure()` draws them when the
stamp doesn't match (a new label, a changed drawing).
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from pathlib import Path

from .recipe import BRANDING_FILES, BRANDING_ROOT, Recipe

# Bump when the composition below changes: every distribution's pictures are drawn again.
COMPOSITION = "1"

# The pictures, by the names the system knows them by (/usr/share/argo/<name>; the names stay for every
# distribution, they are the system's internal paths).
LOGO_SIZES = (64, 128, 256, 512)
LIGHT_LOGO = "argo-light-256.png"
WORDMARK = "argo-wordmark-440.png"              # dark words, for light backgrounds (the installer's welcome)
WORDMARK_LIGHT = "argo-wordmark-light-440.png"  # light words, for dark backgrounds (GDM, SDDM)
BACKGROUND = "argo-background-1920x1080.png"   # the night sea with the wordmark (LightDM, wallpapers)
BACKGROUND_PLAIN = "argo-background-plain-1920x1080.png"  # the sea alone (SDDM draws its own logo; the installer)
GRUB = "argo-grub-1024x768.png"                # the boot menu's background, the wordmark small in a corner
NAMES = (*(f"argo-{s}.png" for s in LOGO_SIZES), LIGHT_LOGO, WORDMARK, WORDMARK_LIGHT, BACKGROUND, BACKGROUND_PLAIN, GRUB)
STAMP = "inputs.sha256"

FONTS = "/usr/share/fonts/truetype/noto"
BOLD, REGULAR = f"{FONTS}/NotoSans-Bold.ttf", f"{FONTS}/NotoSans-Regular.ttf"

# The wordmark: the 256-unit logo, then the label, at 112 units unless it must shrink to fit.
LOGO, GAP, SIZE, SPACE, MARGIN, MAX_WIDTH = 256, 6, 112, 26, 24, 1400
DARK_TEXT, LIGHT_TEXT = "#1d3f6e", "#ffffff"
SKY = ("#0d1b2e", "#16325a", "#1d4a80")
SWELL = "#5dade2"


class BrandingError(ValueError):
    pass


def folder(r: Recipe) -> Path:
    return BRANDING_ROOT / r.branding


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def pictures_dir(r: Recipe) -> Path:
    return folder(r) / "pictures" / slug(r.label_text)


def stamp(r: Recipe) -> str:
    """What the pictures are made from: the drawings, the label, the composition."""
    h = hashlib.sha256(f"composition {COMPOSITION}\nlabel {r.label_text}\n".encode())
    for name in BRANDING_FILES:
        h.update(name.encode() + b"\0" + (folder(r) / name).read_bytes() + b"\0")
    return h.hexdigest()


def current(r: Recipe) -> bool:
    d = pictures_dir(r)
    return (d / STAMP).is_file() and (d / STAMP).read_text().strip() == stamp(r) and all((d / n).is_file() for n in NAMES)


def pictures(r: Recipe) -> dict[str, bytes]:
    """The recipe's pictures, by name. They must have been drawn (ensure())."""
    if not current(r):
        raise BrandingError(f"the pictures of {r.branding!r} with the label {r.label_text!r} aren't drawn yet, or are "
                            f"out of date: python3 -m lfsbuilder branding {r.path}")
    d = pictures_dir(r)
    return {name: (d / name).read_bytes() for name in NAMES}


def label_parts(label: str) -> tuple[str, str]:
    """"Odyssey Linux" -> ("Odyssey", "Linux"): the first word bold, the rest regular."""
    first, _, rest = label.strip().partition(" ")
    return first, rest.strip()


def _data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def wordmark(logo_svg: str, label: str, em: tuple[float, float], colour: str) -> tuple[str, int]:
    """The logo and the label side by side, as SVG, and its width (its height is 256).
    `em`: the bold and the regular part's widths in ems (measured with the fonts)."""
    bold, light = label_parts(label)
    size = float(SIZE)

    def text_width(s: float) -> float:
        return em[0] * s + ((SPACE / SIZE) * s + em[1] * s if light else 0)

    start = LOGO + GAP
    if start + text_width(size) + MARGIN > MAX_WIDTH:
        size = (MAX_WIDTH - start - MARGIN) / text_width(1.0)  # (the width grows with the size, linearly)
    width = int(start + text_width(size) + MARGIN + 0.999)
    width += width % 2
    baseline = 128 + 0.357 * size  # the capitals centred on the logo's middle (Noto's capital height: 0.714 em)
    light_part = (f'<tspan font-weight="400" dx="{SPACE / SIZE * size:.2f}">{light}</tspan>' if light else "")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 256" width="{width}" height="256">'
           f'<image x="0" y="0" width="{LOGO}" height="{LOGO}" href="{_data_uri(logo_svg)}"/>'
           f'<text x="{start}" y="{baseline:.2f}" font-family="Noto Sans" font-size="{size:.2f}" fill="{colour}">'
           f'<tspan font-weight="700">{bold}</tspan>{light_part}</text></svg>')
    return svg, width


def _sea(width: int, height: int, swell_y: int) -> str:
    """The night sky over the sea, and three swells low down: the backgrounds' base."""
    swells = "".join(
        f'<path d="M0 {y} C160 {y - d} 320 {y - d} 480 {y} S800 {y + d} 960 {y} S1280 {y - d} 1440 {y} S1760 {y + d} 1920 {y}"/>'
        for y, d in ((swell_y, 30), (swell_y + 60, 25), (swell_y + 120, 20)))
    return (f'<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{SKY[0]}"/>'
            f'<stop offset="0.65" stop-color="{SKY[1]}"/><stop offset="1" stop-color="{SKY[2]}"/></linearGradient></defs>'
            f'<rect width="{width}" height="{height}" fill="url(#sky)"/>'
            f'<g opacity="0.18" fill="none" stroke="{SWELL}" stroke-width="3" stroke-linecap="round">{swells}</g>')


def background(mark: tuple[str, int] | None) -> str:
    """1920x1080: the sea, and the light wordmark high and centred (the login box stays clear)."""
    body = _sea(1920, 1080, 900)
    if mark:
        svg, width = mark
        body += f'<image x="{960 - width / 2:.1f}" y="120" width="{width}" height="256" href="{_data_uri(svg)}"/>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">{body}</svg>'


def grub(mark: tuple[str, int]) -> str:
    """GRUB's 4:3 background: the sea, the wordmark small in the bottom-right corner, below GRUB's help text."""
    svg, width = mark
    scale = 0.55
    body = _sea(1920, 1440, 1260)
    body += (f'<image x="{1874 - width * scale:.1f}" y="1300" width="{width * scale:.1f}" height="{256 * scale:.1f}" '
             f'href="{_data_uri(svg)}"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1440" width="1024" height="768">{body}</svg>'


MEASURE = """
import sys
from fontTools.ttLib import TTFont
def em(path, text):
    font = TTFont(path)
    cmap, hmtx = font.getBestCmap(), font["hmtx"]
    return sum(hmtx[cmap.get(ord(c), cmap[ord("?")])][0] for c in text) / font["head"].unitsPerEm
print(em(sys.argv[1], sys.argv[3]), em(sys.argv[2], sys.argv[4]))
"""


def ensure(tools, r: Recipe, work: Path) -> bool:
    """Draws the recipe's pictures if they aren't current. True if it drew them."""
    if current(r):
        return False
    render(tools, r, work)
    return True


def render(tools, r: Recipe, work: Path) -> Path:
    """Composes the pictures and draws them with rsvg-convert in the tools image (into `work`, a directory
    under the tools' work mount), then puts them in branding/<name>/pictures/<label>/ with their stamp."""
    logo = (folder(r) / "logo.svg").read_text()
    logo_light = (folder(r) / "logo-light.svg").read_text()
    bold, light = label_parts(r.label_text)
    measured = tools.run("python3", "-c", MEASURE, BOLD, REGULAR, bold, light or "x", capture=True).stdout.split()
    em = (float(measured[0]), float(measured[1]) if light else 0.0)
    dark_mark = wordmark(logo, r.label_text, em, DARK_TEXT)
    light_mark = wordmark(logo_light, r.label_text, em, LIGHT_TEXT)
    sources = {"logo.svg": logo, "logo-light.svg": logo_light, "wordmark.svg": dark_mark[0],
               "wordmark-light.svg": light_mark[0], "background.svg": background(light_mark),
               "background-plain.svg": background(None), "grub.svg": grub(light_mark)}
    jobs = [*((f"argo-{s}.png", "logo.svg", ["-w", str(s), "-h", str(s)]) for s in LOGO_SIZES),
            (LIGHT_LOGO, "logo-light.svg", ["-w", "256", "-h", "256"]),
            (WORDMARK, "wordmark.svg", ["-h", "128"]), (WORDMARK_LIGHT, "wordmark-light.svg", ["-h", "128"]),
            (BACKGROUND, "background.svg", ["-w", "1920", "-h", "1080"]),
            (BACKGROUND_PLAIN, "background-plain.svg", ["-w", "1920", "-h", "1080"]),
            (GRUB, "grub.svg", ["-w", "1024", "-h", "768"])]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for name, text in sources.items():
        (work / name).write_text(text)
    script = "set -e\n" + "".join(
        f'rsvg-convert {" ".join(size)} -o "$1/{out}" "$1/{src}"\n' for out, src, size in jobs)
    tools.run("sh", "-c", script, "sh", tools.inside(work))
    out = pictures_dir(r)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for name in NAMES:
        shutil.copyfile(work / name, out / name)
    (out / STAMP).write_text(stamp(r) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    return out
