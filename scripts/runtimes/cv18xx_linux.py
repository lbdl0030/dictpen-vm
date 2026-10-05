#!/usr/bin/env python3
"""runtime: cv18xx-linux-* —— Cvitek CV18xx（CV180x/CV181x/CV182x）Linux 固件。

覆盖 CV18xx 同族、rootfs 为只读镜像的机器：X5pro / S6pro（ext4）、S7pro（erofs）。
厂商的 boot 镜像是 FIT（gzip 内核 + 多个板级 DTB），但那颗内核是给真机 CV182x 外设
用的，QEMU 没有 CV182x 机型，所以和 rv1106 runtime 一样：**厂商 rootfs + 框架通用
Debian arm64 内核 + virt 机型**。boot 镜像只作参数参考。

两种镜像形态只差一个内核参数，所以做成同一个类的两个子类（`rootfstype` 不同）：

  * `cv18xx-linux-ext4`  —— rootfs 为只读 **ext4**（X5pro / S6pro / Cherry V1 …）；
  * `cv18xx-linux-erofs` —— rootfs 为只读 **erofs**（S7pro / Y08 等）。

其余共性（都不含机型信息）：

  * 通过 `DICTPEN_ROOTFSTYPE=<fs>` 告诉 initramfs 的通用 init 用什么 fs 挂只读根
    （框架内核的 ext4/erofs 都是模块）；
  * 厂商用户态可能是 **32 位 armhf**（Cvitek SDK 惯例），框架内核是 arm64 ——
    依赖它的 CONFIG_COMPAT / CONFIG_COMPAT_BINFMT_ELF（Debian 默认开）直接跑
    32 位进程，因此这里必须用 `-cpu max` 而不是指定无 aarch32 支持的 CPU 模型。
"""
from __future__ import annotations

from . import BootContext, Runtime, framework_initrd_default, framework_kernel_default


class Cv18xxLinuxRuntime(Runtime):
    name = "cv18xx-linux-ext4"
    rootfstype = "ext4"
    note = "CV18xx 族 Linux 固件：只读 ext4 rootfs（armhf 用户态跑在 arm64 兼容内核上，virt 机型）"

    def kernel(self, pack):
        return pack.kernel or framework_kernel_default()

    def initrd(self, pack):
        return pack.initrd or framework_initrd_default()

    def validate(self, pack):
        problems = []
        if not pack.rootfs:
            problems.append("pack.toml 的 [image].rootfs 没写")
        if not pack.xres or not pack.yres:
            problems.append("pack.toml 的 [display] 缺 xres/yres（虚拟屏尺寸来自包）")
        return problems

    def platform_args(self, pack, ctx: BootContext):
        # 只读根的类型由包选的 runtime 决定：initramfs 的通用 init 按它挂 /dev/vda
        ctx.append += f" DICTPEN_ROOTFSTYPE={self.rootfstype}"
        return ([
            "-M", "virt", "-cpu", "max", "-smp", "4", "-m", "2G",
            "-accel", "tcg,thread=multi",
            "-kernel", ctx.kernel, "-initrd", ctx.initrd, "-append", ctx.append,
        ]
            + self.drive_args(pack, ctx.state_img)
            # bochs-display：guest 写显存即上屏（理由见 rv1106 runtime 的注释）
            + self.display_args(pack))


class Cv18xxLinuxErofsRuntime(Cv18xxLinuxRuntime):
    """同族里 rootfs 为只读 erofs 的机器（S7pro / Y08）：只多一个 rootfstype。"""

    name = "cv18xx-linux-erofs"
    rootfstype = "erofs"
    note = "CV18xx 族 Linux 固件：只读 erofs rootfs（armhf 用户态跑在 arm64 兼容内核上，virt 机型）"
