"""A distribution's logo drawn by Claude from a description.

`python3 -m lfsbuilder draw-logo NAME "DESCRIPTION"` (and the form's "Draw a
logo with Claude…") asks Claude, through Claude Code as the repair does
(`claude -p`, no tools, a JSON schema for the answer, run in an empty
directory so this project's instructions aren't loaded), for the two drawings
of branding/<NAME>/: logo.svg (for light backgrounds) and logo-light.svg (for
dark ones). The answer is checked before anything is written: a well-formed
SVG, square, small, made only of shapes (an allowlist of elements and
attributes; no text, no images, no scripts, no links outside the file, no
DOCTYPE), since the drawings are drawn by rsvg on this machine and go into
every system built with them. Then the pictures are drawn with the label
(branding.py), for the form to show.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .recipe import BRANDING_ROOT

TIMEOUT = 300
MAX_BYTES = 48_000
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

SYSTEM = """You draw logos for Linux distributions made with lfsbuilder.

You get a description of the logo the owner wants. Answer with two SVG drawings of the same logo:
- logo_svg: for LIGHT backgrounds (application icons, an installer's welcome page): dark or saturated colours.
- logo_light_svg: the same drawing for DARK backgrounds (a night-blue login screen, #0d1b2e to #1d4a80): light colours where logo_svg has dark ones, so it stands out.

Rules (a drawing that breaks one is refused):
- <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">, a transparent background (no full-size background rectangle).
- Only these elements: svg g defs path circle ellipse rect line polyline polygon linearGradient radialGradient stop clipPath mask title desc.
- No text (the distribution's name is written beside the logo by lfsbuilder), no <image>, <style>, <script>, <use>, <foreignObject>, no links, no DOCTYPE. Colours as attributes (fill, stroke, ...); references only to ids in the same file: url(#id).
- Under 40 KB each. Simple, flat, bold shapes that read at 64 x 64 pixels, centred, filling most of the square; a few colours.
- summary: one sentence saying what you drew."""

SCHEMA = {
    "type": "object",
    "properties": {
        "logo_svg": {"type": "string"},
        "logo_light_svg": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["logo_svg", "logo_light_svg", "summary"],
}

SVG_NS = "http://www.w3.org/2000/svg"
ELEMENTS = {"svg", "g", "defs", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon",
            "linearGradient", "radialGradient", "stop", "clipPath", "mask", "title", "desc"}
ATTRIBUTES = {
    "id", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy",
    "d", "points", "transform", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "opacity",
    "clip-path", "clip-rule", "mask", "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform",
    "spreadMethod", "clipPathUnits", "maskUnits", "maskContentUnits", "preserveAspectRatio", "version",
}
URL = re.compile(r"url\(\s*([^)]*)\)")


class LogoError(ValueError):
    pass


def problems(svg: str) -> list[str]:
    """What is wrong with a drawing, if anything (empty: it may be used)."""
    found: list[str] = []
    if len(svg.encode()) > MAX_BYTES:
        found.append(f"it is {len(svg.encode())} bytes (at most {MAX_BYTES})")
    if re.search(r"<!DOCTYPE|<!ENTITY|<\?xml-stylesheet", svg, re.I):
        found.append("a DOCTYPE, entity or stylesheet")
        return found
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as error:
        return [*found, f"not well-formed XML ({error})"]
    if root.tag != f"{{{SVG_NS}}}svg":
        found.append("the root isn't an SVG element")
    box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(box) != 4 or box[0] != "0" or box[1] != "0" or box[2] != box[3]:
        found.append(f"the viewBox {root.get('viewBox')!r} isn't a square starting at 0 0")
    for element in root.iter():
        namespace, _, tag = element.tag.rpartition("}")
        if namespace.lstrip("{") != SVG_NS or tag not in ELEMENTS:
            found.append(f"<{tag}> isn't allowed")
            continue
        if tag not in ("title", "desc") and (element.text or "").strip():
            found.append(f"<{tag}> holds text")
        for name, value in element.attrib.items():
            if name not in ATTRIBUTES:
                found.append(f"the attribute {name!r} on <{tag}> isn't allowed")
            for target in URL.findall(value):
                if not target.strip().strip("'\"").startswith("#"):
                    found.append(f"{name}={value!r} points outside the file")
    return sorted(set(found))


def ask_claude(description: str, previous: str = "") -> tuple[str, str, str, float]:
    """Claude's two drawings for the description: (logo, logo-light, summary, cost in USD)."""
    prompt = f"The logo the owner wants:\n\n{description.strip()}\n"
    if previous:
        prompt += f"\nYour last drawing was refused, because: {previous}\nDraw it again, keeping to the rules.\n"
    command = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM]
    with tempfile.TemporaryDirectory() as empty:  # outside the project: its CLAUDE.md isn't loaded
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=empty, timeout=TIMEOUT)
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise LogoError(f"Claude Code didn't answer: {(result.stderr or result.stdout)[-500:]}") from None
    answer = reply.get("structured_output") or {}
    if not answer.get("logo_svg") or not answer.get("logo_light_svg"):
        raise LogoError(f"Claude Code gave no drawings: {str(reply.get('result', ''))[-500:]}")
    return (answer["logo_svg"], answer["logo_light_svg"], answer.get("summary", ""),
            float(reply.get("total_cost_usd", 0) or 0))


def draw(name: str, description: str, attempts: int = 2) -> tuple[str, str, str, float]:
    """Checked drawings for the description: a refused one is asked for again once, with the reasons."""
    previous, cost = "", 0.0
    for _ in range(attempts):
        logo, light, summary, spent = ask_claude(description, previous)
        cost += spent
        bad = [f"logo.svg: {p}" for p in problems(logo)] + [f"logo-light.svg: {p}" for p in problems(light)]
        if not bad:
            return logo, light, summary, cost
        previous = "; ".join(bad)
    raise LogoError(f"the drawings were refused twice: {previous}")


def write(name: str, logo: str, light: str, replace: bool = False) -> Path:
    """branding/<name>/ with the two drawings (an existing folder only with `replace`)."""
    if not NAME.match(name):
        raise LogoError(f"{name!r} isn't a folder name (lower-case letters, digits, _ and -)")
    folder = BRANDING_ROOT / name
    if folder.exists() and not replace:
        raise LogoError(f"branding/{name}/ is there already (choose another name, or replace it)")
    for svg in (logo, light):
        if found := problems(svg):
            raise LogoError("; ".join(found))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "logo.svg").write_text(logo if logo.endswith("\n") else logo + "\n")
    (folder / "logo-light.svg").write_text(light if light.endswith("\n") else light + "\n")
    return folder
