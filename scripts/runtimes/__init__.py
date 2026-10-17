#!/usr/bin/env python3
"""框架内置的多种"运行框架"（boot backend）。

虚拟机本身不是"某一台机器"，而是一个**能跑多类固件**的框架：一种固件形态对应一个
runtime。包通过 `pack.toml` 的 `[platform].runtime` 选一个；新增平台 = 加一个模块
+ 一个包，不需要改 `dpctl.py` 的核心逻辑。

`Runtime` 是接口（protocol 意义上的），框架只依赖这些方法：

    validate(pack)          -> [问题描述]      缺什么、为什么这个包跑不了
    kernel(pack) / initrd(pack)                 该平台这次启动用哪个内核 / 基础 initramfs
    initramfs_extra(pack)   -> [(name, mode, data, nlink)]   额外要塞进 initrd 的东西
    platform_args(pack, ctx) -> [QEMU 参数]     该平台的机器/驱动/显示参数

与机型无关的部分（网络、串口、监视器、QMP、显示前端、输入设备）不在这里，
由 dpctl 统一组装 —— runtime 只负责"这个平台的固件怎么跑起来"。
"""
from __future__ import annotations

import dataclasses
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class UnsupportedPlatform(Exception):
    """包声明的平台在框架里没有对应的 runtime。"""


@dataclasses.dataclass
class BootContext:
    """一次启动的运行时上下文（由 dpctl 填好，交给 runtime 组装 QEMU 参数）。"""
    kernel: str = ""
    initrd: str = ""
    append: str = ""
    state_img: str = ""


class Runtime:
    """boot backend 基类。子类至少要实现 `platform_args`。"""

    name = ""
    note = ""

    # ---- 平台能力声明 -------------------------------------------------- #
    def validate(self, pack):
        return []

    def kernel(self, pack):
        return pack.kernel

    def initrd(self, pack):
        return pack.initrd

    def initramfs_extra(self, pack):
        return []

    def platform_args(self, pack, ctx):
        raise NotImplementedError

    # ---- 通用小工具 ---------------------------------------------------- #
    @staticmethod
    def display_args(pack):
        """面板尺寸只由包声明；框架不猜。"""
        if not pack.xres or not pack.yres:
            return []
        return ["-device", f"bochs-display,xres={pack.xres},yres={pack.yres}"]

    @staticmethod
    def drive_args(pack, state_img):
        return [
            "-drive", f"file={pack.rootfs},if=virtio,format=raw,readonly=on",
            "-drive", f"file={state_img},if=virtio,format=raw,cache=writeback",
        ]


# --------------------------------------------------------------------------- #
# 框架自带资产（runtime 留空时用）
# --------------------------------------------------------------------------- #
def framework_kernel_default():
    """框架自带的通用内核（Debian arm64，含 DRM/virtio/ext4 模块）。"""
    return os.path.join(ROOT, "tools", "kernel-root-gui", "boot",
                        "vmlinuz-6.1.0-50-arm64")


def framework_initrd_default():
    """框架的通用 initramfs（只构建一次，永不含机型内容）。"""
    return os.path.join(ROOT, "images", "initramfs.cpio.gz")


# --------------------------------------------------------------------------- #
# 注册表（放在最后：子模块要 import 上面的基类与默认路径）
# --------------------------------------------------------------------------- #
from . import rv1106_linux                                    # noqa: E402
from . import cv18xx_linux                                    # noqa: E402
from . import rockchip_ext4                                   # noqa: E402

REGISTRY = {c.name: c for c in (rv1106_linux.Rv1106LinuxRuntime,
                                cv18xx_linux.Cv18xxLinuxRuntime,
                                cv18xx_linux.Cv18xxLinuxErofsRuntime,
                                rockchip_ext4.RockchipExt4Runtime)}


def names():
    return sorted(REGISTRY)


def available():
    """[(name, note)]，供 `dpctl packs` / 报错提示列出。"""
    return [(n, REGISTRY[n].note) for n in names()]


def get(name):
    try:
        return REGISTRY[name]()
    except KeyError:
        raise UnsupportedPlatform(
            f"该平台 runtime 未实现：{name!r}；框架现有：{', '.join(names()) or '（无）'}")