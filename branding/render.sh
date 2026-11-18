#!/bin/bash
# Renders the logo's SVGs to the PNGs the systems use (login screens, boot
# menu), with headless Chrome or Chromium on the machine you run it on. The
# PNGs are committed beside the SVGs, so a build never needs a browser: run
# this again after changing an SVG.
#
#   branding/render.sh
set -euo pipefail
cd "$(dirname "$0")"
browser=$(command -v google-chrome || command -v chromium || command -v chromium-browser) || {
    echo "needs Chrome or Chromium" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A broken SVG renders as the browser's error page: check them all first.
python3 -c 'import sys, xml.dom.minidom; [xml.dom.minidom.parse(f) for f in sys.argv[1:]]' *.svg

# render SVG WIDTH HEIGHT OUT: the SVG scaled to WIDTH x HEIGHT on a transparent background.
render() {
    sed "0,/width=\"[0-9]*\" height=\"[0-9]*\"/s//width=\"$2\" height=\"$3\"/" "$1" > "$work/in.svg"
    "$browser" --headless=new --disable-gpu --hide-scrollbars --default-background-color=00000000 \
        --window-size="$2,$3" --screenshot="$work/out.png" "file://$work/in.svg" >/dev/null 2>&1
    mv "$work/out.png" "$4"
    echo "$4"
}

for size in 64 128 256 512; do
    render argo.svg "$size" "$size" "argo-$size.png"
done
render argo-light.svg 256 256 argo-light-256.png
render argo-wordmark.svg 440 128 argo-wordmark-440.png
render argo-wordmark-light.svg 440 128 argo-wordmark-light-440.png
render argo-wordmark-light.svg 880 256 argo-wordmark-light-880.png
render argo-background.svg 1920 1080 argo-background-1920x1080.png
render argo-background-plain.svg 1920 1080 argo-background-plain-1920x1080.png
render argo-grub.svg 1024 768 argo-grub-1024x768.png
