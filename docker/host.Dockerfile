# The tools lfsbuilder runs outside the system it builds: checking Gentoo's
# signatures (gpg), fetching the pinned repository commit (git), making disk
# images without loop devices or root (mkfs.ext4 -d), live ISOs (sqfstar,
# grub-mkrescue for BIOS and UEFI), password hashes (openssl passwd), and the
# distribution's pictures (rsvg-convert, the Noto fonts, fontTools to measure
# the label). The same image on every machine, so these steps don't depend on
# the machine's own tools.
FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl e2fsprogs git gnupg libarchive13t64 openssl xz-utils \
        grub-common grub-pc-bin grub-efi-amd64-bin mtools squashfs-tools xorriso \
        librsvg2-bin fonts-noto-core fontconfig python3 python3-fonttools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
