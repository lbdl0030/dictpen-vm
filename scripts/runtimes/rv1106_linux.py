#!/usr/bin/env python3
"""runtime: rv1106-linux-erofs —— RV1106（rockchip）Linux 固件。

这是框架里的第一个 boot backend，覆盖 RV1106 同族（A6pro / Hermes / Y09P…）：
只读 **erofs** rootfs 挂给 guest，另外一份 boot 镜像（DTB + 面板/DRM 参数）只作
参数参考，QEMU 本身用 `virt` 机型 + 框架自带的 Debian arm64 通用内核 + 通用 initramfs。

平台假设只在这里出现（数量极少，且都是"这类固件怎么跑"的共性，不是某一台机器的
型号/分辨率/补丁）：

  * 镜像格式        erofs（只读）、boot 单独一份
  * 显示设备        bochs-display（线性显存，guest 写完即上屏，见 README）
  * 机器/CPU        virt / max / tcg,thread=multi（WHPX 不支持 ARM64 客户机）

渲染、输赢设备的细节（EDID 面板尺寸、HAL 补丁、触摸桥…）都由**包**提供，
本 runtime 不引入任何机型知识。
"""
from __future__ import annotations

import os

from . import BootContext, Runtime, framework_initrd_default, framework_kernel_default


class Rv1106LinuxRuntime(Runtime):
    name = "rv1106-linux-erofs"
    note = "RV1106 族 Linux 固件：只读 erofs rootfs + 独立 boot 镜像（virt 机型）"

    # RV1106 是真单核 A7。TCG 下多给的 vCPU 不会闲着：a7p 实测空闲时宿主
    # 4 个 vCPU 37%、2 个 29%、1 个 24%，而 1 个会让页面响应从 ~160 ms 掉到 ~460 ms。
    # 2 个是不损失交互、又少一份空转的折中（机器参数仍可由 vm/machine.toml 覆盖）。
    default_smp = 2

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
        return ([
            "-M", "virt", "-cpu", os.environ.get("DICTPEN_CPU", "max"),
            "-smp", str(ctx.smp or self.default_smp),
            "-m", ctx.mem or self.default_mem,
            "-accel", "tcg," + os.environ.get("DICTPEN_TCG", "thread=multi"),
            "-kernel", ctx.kernel, "-initrd", ctx.initrd, "-append", ctx.append,
        ]
            + self.drive_args(pack, ctx.state_img)
            # 用 bochs-display 而不是 virtio-gpu：bochs-drm 给的是显存里的
            # framebuffer，guest 写内存即上屏。pen 的 falcon HAL 从不发
            # dirty/page-flip（依赖面板一直扫描那块内存），virtio-gpu 需要显式
            # RESOURCE_FLUSH，画面会永远停在启动图。
            + self.display_args(pack))