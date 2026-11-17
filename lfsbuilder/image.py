"""The built system out of Docker: a root file system, a disk image, a boot.

The last checkpoint is exported as a tar of the whole system, with the files
Docker keeps for itself (/etc/hostname, /etc/hosts, /etc/resolv.conf)
written from the recipe instead, and an /etc/fstab for the disk. The kernel
and initramfs are taken out of /boot on the way, for QEMU to start directly.

The disk image is made from the tar by mke2fs (-d), in the tools image,
without loop devices or root; the owners come from the tar. The boot test
starts it in QEMU and waits for the login prompt on the serial console.
"""

from __future__ import annotations

import io
import os
import re
import selectors
import subprocess
import tarfile
import time
from pathlib import Path

from .recipe import Recipe

DOCKER_FILES = {"etc/hostname", "etc/hosts", "etc/resolv.conf", "etc/fstab", ".dockerenv"}
KERNEL = re.compile(r"^boot/((?:vmlinuz|kernel)-[^/]+)$")  # installkernel names it kernel-<version> now
INITRAMFS = re.compile(r"^boot/(initramfs-[^/]+\.img)$")


def system_files(r: Recipe) -> dict[str, tuple[str, str]]:
    """What the exported system gets in place of Docker's files: path ->
    (kind, content); kind "file" or "link"."""
    fqdn = f"{r.hostname}.{r.domain}"
    files = {
        "etc/hostname": ("file", f"{r.hostname}\n"),
        "etc/hosts": ("file", f"127.0.0.1  localhost\n::1        localhost\n127.0.1.1  {fqdn} {r.hostname}\n"),
        "etc/fstab": ("file", f"# Written by lfsbuilder\n/dev/vda  /  {r.output.get('filesystem', 'ext4')}  defaults  0 1\n"),
    }
    if r.init == "systemd":
        files["etc/resolv.conf"] = ("link", "../run/systemd/resolve/stub-resolv.conf")
    else:
        files["etc/resolv.conf"] = ("file", "".join(f"nameserver {d}\n" for d in r.network.get("dns", [])))
    return files


def export(image: str, r: Recipe, rootfs: Path, boot: Path) -> tuple[Path, Path]:
    """The system of `image` as `rootfs` (a tar), its kernel and initramfs
    copied to `boot`. Returns their paths."""
    boot.mkdir(parents=True, exist_ok=True)
    kernel = initramfs = None
    replaced = system_files(r)
    container = subprocess.run(["docker", "create", image, "/bin/true"], check=True, text=True, capture_output=True).stdout.strip()
    part = rootfs.with_name(rootfs.name + ".part")
    try:
        source = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=source.stdout, mode="r|") as tin, tarfile.open(part, "w", format=tarfile.PAX_FORMAT) as tout:
            for member in tin:
                name = member.name.removeprefix("./")
                if name in DOCKER_FILES:
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


KVM_FAILED = b"KVM: entry failed"


def boot_test(kernel: Path, initramfs: Path, image: Path, log: Path, timeout: int = 300) -> bool:
    """Starts the disk in QEMU and waits for the login prompt on the serial
    console. True if it comes; the console's output is in `log`.

    KVM is used when /dev/kvm is there. If the virtual machine can't even
    start under it (another hypervisor, such as a running VirtualBox VM,
    holding the processor's VT-x), the test runs again without KVM: slower,
    but the answer is the same."""
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        result = _boot(kernel, initramfs, image, log, timeout, kvm=True)
        if result is not None:
            return result
        print("KVM can't start the virtual machine (is VirtualBox or another hypervisor running?);"
              " trying again without KVM, which is slower", flush=True)
    return bool(_boot(kernel, initramfs, image, log, timeout * 5, kvm=False))


def qemu_command(kernel: Path, initramfs: Path, image: Path, kvm: bool) -> list[str]:
    return [
        "qemu-system-x86_64", "-m", "2048", "-smp", "2", "-nographic", "-no-reboot",
        *(["-enable-kvm", "-cpu", "host"] if kvm else []),
        "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", "root=/dev/vda rw console=ttyS0 panic=-1",
        "-drive", f"file={image},format=raw,if=virtio",
        "-nic", "user,model=virtio-net-pci",
    ]


def console(kernel: Path, initramfs: Path, image: Path) -> None:
    """The system in QEMU on this terminal (the serial console); Ctrl-A X quits."""
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    os.execvp("qemu-system-x86_64", qemu_command(kernel, initramfs, image, kvm))


def _boot(kernel: Path, initramfs: Path, image: Path, log: Path, timeout: int, kvm: bool) -> bool | None:
    """One boot; None if KVM failed to run the machine at all."""
    command = qemu_command(kernel, initramfs, image, kvm)
    seen = b""
    started = time.monotonic()
    with log.open("wb") as out:
        qemu = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector = selectors.DefaultSelector()
        selector.register(qemu.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() - started < timeout:
                if not selector.select(timeout=5):
                    continue
                chunk = os.read(qemu.stdout.fileno(), 65536)
                if not chunk:
                    break  # QEMU stopped (a panic, with -no-reboot)
                out.write(chunk)
                seen = (seen + chunk)[-4096:]
                if b"login:" in seen:
                    return True
                if kvm and KVM_FAILED in seen:
                    return None
            return False
        finally:
            qemu.kill()
            qemu.wait()
