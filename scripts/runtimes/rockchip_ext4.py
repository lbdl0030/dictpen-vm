"""Read-only Rockchip Linux ext4 userspace on QEMU virt."""
from __future__ import annotations

import gzip
from pathlib import Path

from . import BootContext, Runtime, framework_initrd_default, framework_kernel_default


class RockchipExt4Runtime(Runtime):
    name = "rockchip-linux-ext4"
    note = "Rockchip ARM Linux ext4 userspace on virt; board adaptations belong to the pack"

    def kernel(self, pack):
        return pack.kernel or framework_kernel_default()

    def initrd(self, pack):
        return pack.initrd or framework_initrd_default()

    def validate(self, pack):
        problems = []
        if not pack.rootfs or not Path(pack.rootfs).is_file():
            problems.append("Missing ext4 rootfs")
        else:
            with open(pack.rootfs, "rb") as stream:
                stream.seek(1080)
                if stream.read(2) != b"\x53\xef":
                    problems.append("Rootfs must be raw ext4, not sparse or EROFS")
        if not pack.xres or not pack.yres:
            problems.append("Display dimensions are required")
        for label, path in (("kernel", self.kernel(pack)), ("initrd", self.initrd(pack))):
            if not Path(path).is_file():
                problems.append("Missing " + label + ": " + path)
        return problems

    def initramfs_extra(self, pack):
        from packlib import PackError, read_cpio
        entries = read_cpio(gzip.decompress(Path(self.initrd(pack)).read_bytes()))
        matches = [entry for entry in entries if entry[0].lstrip("./") == "init"]
        if len(matches) != 1:
            raise PackError("Expected exactly one init in the base initramfs")
        name, mode, data, nlink = matches[0]
        if b'ROOTFSTYPE="${DICTPEN_ROOTFSTYPE:-erofs}"' in data:
            return []
        old = b'mount -t erofs -o ro "$ROOTDEV" /ro'
        if data.count(old) != 1:
            raise PackError("Base init root-mount contract changed; review ext4 runtime")
        # Support the older base archive without changing framework source.
        data = data.replace(old, b'mount -t ext4 -o ro,noload "$ROOTDEV" /ro')
        data = data.replace(b"### erofs root mounted:", b"### ext4 root mounted:")
        return [(name, mode, data, nlink)]

    def platform_args(self, pack, ctx: BootContext):
        ctx.append += " DICTPEN_ROOTFSTYPE=ext4"
        return (["-M", "virt", "-cpu", "max", "-smp", "4", "-m", "2G",
                 "-accel", "tcg,thread=multi", "-kernel", ctx.kernel,
                 "-initrd", ctx.initrd, "-append", ctx.append]
                + self.drive_args(pack, ctx.state_img) + self.display_args(pack))
