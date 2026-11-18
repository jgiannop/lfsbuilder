"""The built system out of Docker: a root file system, a disk image, a boot.

The last checkpoint is exported as a tar of the whole system, with the files
Docker keeps for itself (/etc/hostname, /etc/hosts, /etc/resolv.conf)
written from the recipe instead, and an /etc/fstab for the disk. The kernel
and initramfs are taken out of /boot on the way, for QEMU to start directly.

The disk image is made from the tar by mke2fs (-d), in the tools image,
without loop devices or root; the owners come from the tar. The boot test
starts it in QEMU, waits for the login prompt on the serial console, and
saves a picture of the screen (a desktop's login screen, if it has one).
"""

from __future__ import annotations

import io
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import tarfile
import time
import zlib
from pathlib import Path

from .recipe import Recipe

DOCKER_FILES = {"etc/hostname", "etc/hosts", "etc/resolv.conf", "etc/fstab", ".dockerenv"}
# Left out of every exported system: getuto's keyring holds a private key
# (its local trust anchor) that each installed system makes for itself, at
# its first binary package download, rather than all sharing the build's.
LEFT_OUT = ("etc/portage/gnupg",)
KERNEL = re.compile(r"^boot/((?:vmlinuz|kernel)-[^/]+)$")  # installkernel names it kernel-<version> now
INITRAMFS = re.compile(r"^boot/(initramfs-[^/]+\.img)$")


def system_files(r: Recipe, live: bool = False) -> dict[str, tuple[str, str]]:
    """What the exported system gets in place of Docker's files: path ->
    (kind, content); kind "file" or "link". A live system's root is mounted
    by its initramfs, so its fstab has nothing to mount."""
    fqdn = f"{r.hostname}.{r.domain}"
    root = "" if live else f"/dev/vda  /  {r.output.get('filesystem', 'ext4')}  defaults  0 1\n"
    files = {
        "etc/hostname": ("file", f"{r.hostname}\n"),
        "etc/hosts": ("file", f"127.0.0.1  localhost\n::1        localhost\n127.0.1.1  {fqdn} {r.hostname}\n"),
        "etc/fstab": ("file", f"# Written by lfsbuilder\n{root}"),
    }
    # Who the system is: the distribution's name, not Gentoo's (Gentoo's name and logo are Gentoo's to use).
    # A file in place of baselayout's link to /usr/lib/os-release; /etc is protected, so updates keep it.
    files["etc/os-release"] = ("file", "".join(f"{k}={json.dumps(v)}\n" for k, v in (
        ("NAME", r.name), ("ID", r.os_id), ("ID_LIKE", "gentoo"), ("VERSION", r.version), ("VERSION_ID", r.version),
        ("PRETTY_NAME", f"{r.name} {r.version}"), ("ANSI_COLOR", "1;34"), ("LOGO", "argo-linux"))))  # the branding step's icon
    files["etc/issue"] = ("file", f"\n{r.name} {r.version} (\\s \\m \\r) \\t\n\n")  # agetty's \\s etc.
    if r.init == "systemd":
        files["etc/resolv.conf"] = ("link", "../run/systemd/resolve/stub-resolv.conf")
    else:
        files["etc/resolv.conf"] = ("file", "".join(f"nameserver {d}\n" for d in r.network.get("dns", [])))
    return files


def export(image: str, r: Recipe, rootfs: Path, boot: Path, live: bool = False) -> tuple[Path, Path]:
    """The system of `image` as `rootfs` (a tar), its kernel and initramfs
    copied to `boot`. Returns their paths."""
    boot.mkdir(parents=True, exist_ok=True)
    kernel = initramfs = None
    replaced = system_files(r, live)
    container = subprocess.run(["docker", "create", image, "/bin/true"], check=True, text=True, capture_output=True).stdout.strip()
    part = rootfs.with_name(rootfs.name + ".part")
    try:
        source = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=source.stdout, mode="r|") as tin, tarfile.open(part, "w", format=tarfile.PAX_FORMAT) as tout:
            for member in tin:
                name = member.name.removeprefix("./")
                # Docker's files and those written from the recipe below (one copy only: tools that read
                # the tar keep the first of two, which would be the image's own os-release, say).
                if name in DOCKER_FILES or name in replaced or any(name == d or name.startswith(d + "/") for d in LEFT_OUT):
                    continue
                data = tin.extractfile(member) if member.isreg() else None
                if member.isreg() and (KERNEL.match(name) or INITRAMFS.match(name)):
                    content = data.read()
                    (boot / Path(name).name).write_bytes(content)
                    if KERNEL.match(name):
                        kernel = boot / Path(name).name
                    else:
                        initramfs = boot / Path(name).name
                    data = io.BytesIO(content)
                tout.addfile(member, data)
            for name, (kind, content) in replaced.items():
                info = tarfile.TarInfo(name)
                info.mtime = int(time.time())
                if kind == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, content
                    tout.addfile(info)
                else:
                    data = content.encode()
                    info.size, info.mode = len(data), 0o644
                    tout.addfile(info, io.BytesIO(data))
        if source.wait():
            raise RuntimeError(f"docker export of {image} failed")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    if kernel is None or initramfs is None:
        raise RuntimeError(f"{image} has no kernel and initramfs in /boot (is the kernel step done?)")
    part.rename(rootfs)
    return kernel, initramfs


def disk(tools, rootfs: Path, out: Path, size_gb: int) -> None:
    """An ext4 disk image of the rootfs tar, made by mke2fs in the tools image."""
    out.unlink(missing_ok=True)
    tools.run("sh", "-c", f'truncate -s {int(size_gb)}G "$1" && mke2fs -q -t ext4 -L root -d "$2" "$1"',
              "sh", tools.inside(out), tools.inside(rootfs))


def volume_id(r: Recipe) -> str:
    """The ISO's label, by which the live initramfs finds it (CDLABEL=)."""
    return (re.sub(r"[^A-Z0-9]+", "_", f"{r.name} {r.version}".upper()).strip("_") or "LIVE")[:32]


def live_initramfs(image: str, shared_args: list[str], kernel_version: str, out: Path) -> None:
    """An initramfs that boots the live system: dracut with dmsquash-live
    (which finds the ISO by its label, mounts the SquashFS and puts a
    writable overlay in memory over it). Made in a throwaway container of
    the last checkpoint, where device-mapper's tools (which dmsquash-live
    needs) are installed only for this; the system itself doesn't get them."""
    out.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "set -e\n"
        "rm -f /etc/portage/binrepos.conf/lfsbuilder.conf\n"  # the distribution's server needn't be up for this
        "emerge --oneshot --quiet sys-fs/lvm2\n"
        f"dracut --no-hostonly --add dmsquash-live --force /out/{out.name} {kernel_version}\n"
        f"chown {os.getuid()}:{os.getgid()} /out/{out.name}\n"
    )
    result = subprocess.run(["docker", "run", "--rm", *shared_args, "-v", f"{out.parent}:/out", image, "bash", "-lc", script],
                            text=True, capture_output=True)
    if result.returncode or not out.exists():
        raise RuntimeError("making the live initramfs failed:\n" + "\n".join((result.stdout + result.stderr).splitlines()[-30:]))


GRUB_CFG = """# Written by lfsbuilder
set timeout=5
set default=0
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
insmod all_video

menuentry "{title} (live)" {{
    linux /boot/vmlinuz root=live:CDLABEL={volume} rd.live.image rd.overlay console=tty0 console=ttyS0,115200
    initrd /boot/initramfs.img
}}
"""


def iso(tools, rootfs: Path, kernel: Path, initramfs: Path, work: Path, out: Path, r: Recipe) -> None:
    """A live ISO that boots on BIOS and UEFI PCs: the system as a SquashFS
    (sqfstar reads the tar, so the owners come from it, without root), the
    kernel and live initramfs, and GRUB, put together by grub-mkrescue."""
    tree = work / "iso"
    subprocess.run(["rm", "-rf", str(tree)], check=True)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "LiveOS").mkdir()
    (tree / "boot" / "vmlinuz").write_bytes(kernel.read_bytes())
    (tree / "boot" / "initramfs.img").write_bytes(initramfs.read_bytes())
    volume = volume_id(r)
    (tree / "boot" / "grub" / "grub.cfg").write_text(GRUB_CFG.format(title=f"{r.name} {r.version}", volume=volume))
    tools.run("sh", "-c", 'sqfstar -quiet -no-progress -comp zstd "$1" < "$2"', "sh",
              tools.inside(tree / "LiveOS" / "squashfs.img"), tools.inside(rootfs))
    out.unlink(missing_ok=True)
    tools.run("grub-mkrescue", "--quiet", "-o", tools.inside(out), tools.inside(tree), "--", "-volid", volume, capture=True)
    subprocess.run(["rm", "-rf", str(tree)], check=True)


OVMF = Path("/usr/share/ovmf/OVMF.fd")  # UEFI firmware for QEMU (Debian/Ubuntu's ovmf package)
KVM_FAILED = b"KVM: entry failed"
MEMORY_MB = 4096  # a desktop needs more than 2 GB


def boot_test(kernel: Path | None, initramfs: Path | None, image: Path, log: Path, screenshot: Path,
              timeout: int = 300, settle: int = 45, cdrom: bool = False, uefi: bool = False) -> bool:
    """Starts the disk in QEMU and waits for the login prompt on the serial
    console. True if it comes; the console's output is in `log`. Then, after
    `settle` seconds (a display manager starts after the serial login), a
    picture of the screen is saved as `screenshot` (PNG).

    KVM is used when /dev/kvm is there. If the virtual machine can't even
    start under it (another hypervisor, such as a running VirtualBox VM,
    holding the processor's VT-x), the test runs again without KVM: slower,
    but the answer is the same."""
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        result = _boot(kernel, initramfs, image, log, screenshot, timeout, settle, kvm=True, cdrom=cdrom, uefi=uefi)
        if result is not None:
            return result
        print("KVM can't start the virtual machine (is VirtualBox or another hypervisor running?);"
              " trying again without KVM, which is slower", flush=True)
    return bool(_boot(kernel, initramfs, image, log, screenshot, timeout * 5, settle * 5, kvm=False, cdrom=cdrom, uefi=uefi))


def qemu_command(kernel: Path | None, initramfs: Path | None, image: Path, kvm: bool, monitor: Path | None = None,
                 cdrom: bool = False, uefi: bool = False) -> list[str]:
    """With `monitor`, a virtual graphics card (no window) and QEMU's monitor
    on that socket, for screenshots; without, the serial console only. With
    `cdrom`, `image` is an ISO started as a PC would (its own boot loader);
    without a kernel, `image` is a disk started the same way (an installed
    system, with its GRUB)."""
    screen = (["-display", "none", "-vga", "virtio", "-serial", "stdio", "-monitor", f"unix:{monitor},server,nowait"]
              if monitor else ["-nographic"])
    if cdrom:
        boot = ["-cdrom", str(image), "-boot", "d"]
    elif kernel is None:
        boot = ["-drive", f"file={image},format=raw,if=virtio"]
    else:
        boot = ["-kernel", str(kernel), "-initrd", str(initramfs),
                # The last console= is /dev/console (systemd's messages); the screen gets the kernel's too.
                # net.ifnames=0: the network card is eth0, as a recipe's static settings name it (the
                # installer will have to deal with names on real hardware).
                "-append", "root=/dev/vda rw console=tty0 console=ttyS0 net.ifnames=0 panic=-1",
                "-drive", f"file={image},format=raw,if=virtio"]
    return [
        "qemu-system-x86_64", "-m", str(MEMORY_MB), "-smp", "2", "-no-reboot", *screen,
        *(["-enable-kvm", "-cpu", "host"] if kvm else []),
        *(["-bios", str(OVMF)] if uefi else []),
        *boot,
        "-nic", "user,model=virtio-net-pci",
    ]


def install_test(kernel: Path, initramfs: Path, iso: Path, volume: str, disk: Path, log: Path, timeout: int = 1800) -> bool:
    """The live system started from `iso` with an empty `disk` and
    lfsbuilder.autoinstall on its command line: it installs itself on the
    disk and powers off. True if the installer said it was done."""
    disk.unlink(missing_ok=True)
    with disk.open("wb") as f:
        f.truncate(20 * 2**30)  # sparse
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    command = [
        "qemu-system-x86_64", "-m", str(MEMORY_MB), "-smp", "2", "-no-reboot", "-nographic",
        *(["-enable-kvm", "-cpu", "host"] if kvm else []),
        "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", f"root=live:CDLABEL={volume} rd.live.image rd.overlay console=tty0 console=ttyS0,115200 "
                   "lfsbuilder.autoinstall=/dev/vda",
        "-cdrom", str(iso), "-drive", f"file={disk},format=raw,if=virtio",
        "-nic", "user,model=virtio-net-pci",
    ]
    limit = timeout if kvm else timeout * 5
    started, seen = time.monotonic(), b""
    with log.open("wb") as out:
        qemu = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector = selectors.DefaultSelector()
        selector.register(qemu.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() - started < limit:
                if not selector.select(timeout=2):
                    continue
                chunk = os.read(qemu.stdout.fileno(), 65536)
                if not chunk:
                    break  # powered off, after the install (or a panic)
                out.write(chunk)
                seen = (seen + chunk)[-8192:]
                if any(sign in seen for sign in INSTALL_FAILED):
                    return False  # no need to wait out the timeout
        finally:
            qemu.kill()
            qemu.wait()
    return b"==> Done:" in log.read_bytes()


# What the console shows when the automatic install fails: the installer's
# own message, or systemd's for its service.
INSTALL_FAILED = (b"lfsbuilder-install: ", b"Failed to start lfsbuilder automatic install",
                  b"lfsbuilder-autoinstall.service: Failed", b"Kernel panic")


def console(kernel: Path, initramfs: Path, image: Path) -> None:
    """The system in QEMU on this terminal (the serial console); Ctrl-A X quits."""
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    os.execvp("qemu-system-x86_64", qemu_command(kernel, initramfs, image, kvm))


def _boot(kernel: Path | None, initramfs: Path | None, image: Path, log: Path, screenshot: Path,
          timeout: int, settle: int, kvm: bool, cdrom: bool = False, uefi: bool = False) -> bool | None:
    """One boot; None if KVM failed to run the machine at all."""
    monitor = log.with_name("qemu-monitor.sock")
    monitor.unlink(missing_ok=True)
    command = qemu_command(kernel, initramfs, image, kvm, monitor, cdrom, uefi)
    seen = b""
    started = time.monotonic()
    logged_in_at = None
    with log.open("wb") as out:
        qemu = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector = selectors.DefaultSelector()
        selector.register(qemu.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() - started < timeout or logged_in_at:
                if logged_in_at and time.monotonic() - logged_in_at > settle:
                    _screendump(monitor, screenshot)
                    return True
                if not selector.select(timeout=2):
                    continue
                chunk = os.read(qemu.stdout.fileno(), 65536)  # read on, or QEMU's serial blocks
                if not chunk:
                    break  # QEMU stopped (a panic, with -no-reboot)
                out.write(chunk)
                seen = (seen + chunk)[-4096:]
                if logged_in_at is None and b"login:" in seen:
                    logged_in_at = time.monotonic()
                if kvm and KVM_FAILED in seen:
                    return None
            return False
        finally:
            qemu.kill()
            qemu.wait()
            monitor.unlink(missing_ok=True)


def _screendump(monitor: Path, png: Path) -> None:
    """QEMU's screendump (a PPM file) through the monitor, saved as PNG."""
    ppm = png.with_suffix(".ppm")
    ppm.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(monitor))
        s.sendall(f"screendump {ppm}\n".encode())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (ppm.exists() and ppm.stat().st_size > 0):
            time.sleep(0.5)
        time.sleep(1)  # let QEMU finish writing it
    png.write_bytes(ppm_to_png(ppm.read_bytes()))
    ppm.unlink()


def ppm_to_png(data: bytes) -> bytes:
    """A binary PPM (P6, 8 bits) as a PNG, with the standard library only."""
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    magic, width, height, maxval = fields[0], int(fields[1]), int(fields[2]), int(fields[3])
    if magic != b"P6" or maxval != 255:
        raise ValueError("not an 8-bit binary PPM")
    pixels = data[pos + 1:]
    stride = width * 3
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
