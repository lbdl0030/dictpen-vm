#!/usr/bin/env python3
"""框架内置的多种"运行框架"（boot backend）。

虚拟机本身不是"某一台机器"，而是一个**能跑多类固件**的框架：一种固件形态对应一个
runtime。包通过 `pack.toml` 的 `[platform].runtime` 选一个；**新增平台 = 往这个目录
丢一个 `.py` + 一个包**，连本文件都不用改（下面的注册表会自动发现它）。

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
import shutil

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
    # 机器参数：留空 = 用 runtime 自己的 `default_mem` / `default_smp`。
    # 值从 `vm/machine.toml`（控制台「配置」页写的）来，**下次起机**才看得到 ——
    # QEMU 的 `-m` / `-smp` 是命令行参数，跑着的机器改不了。
    mem: str = ""
    smp: int = 0
    # 声音：`-audiodev dsound` + intel-hda（宿主侧出声）。默认关 —— 不是每台宿主都有声卡，
    # 打开后 guest 里还要有 snd 模块（见 build-initramfs 的 MODULES）。
    audio: bool = False


class Runtime:
    """boot backend 基类。子类至少要实现 `platform_args`。"""

    name = ""
    note = ""

    # 这台虚拟机默认给多少内存 / 几个 vCPU（`vm/machine.toml` 能覆盖，见 dpctl.machine_plan）
    default_mem = "2G"
    default_smp = 4

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
    def _extra_copy(pack, path):
        """额外镜像按"每台虚拟机一份可写副本"挂。

        这些分区（如 x3s 的 uresource）在固件首启时会被应用改写：先用
        `mount -o remount,rw` 把离线资源搬进去，再切回 ro。直接挂包里的原图
        既写不进去，也会改脏发布资产，所以第一次启动时复制到 `vm/`。
        """
        dst = os.path.join(ROOT, "vm", f"extra-{pack.name}-{os.path.basename(path)}")
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(path, dst)
        return dst

    @classmethod
    def drive_args(cls, pack, state_img):
        state_fmt = os.environ.get("DICTPEN_STATE_FMT", "raw")
        args = [
            "-drive", f"file={pack.rootfs},if=virtio,format=raw,readonly=on",
            "-drive", f"file={state_img},if=virtio,format={state_fmt},cache=writeback",
        ]
        # 包声明的额外镜像：/dev/vdc、vdd…（顺序即声明顺序）
        # 存快照 / 从快照恢复时把它们挂成只读：QEMU 不允许"可写但不支持快照"的
        # 块设备参与 savevm（x3s342 的 uresource 实测报 `Device 'virtio2' is
        # writable but does not support snapshots`）。这台固件本来就 `-o ro` 挂它。
        extra_ro = bool(os.environ.get("DICTPEN_SNAPSHOT")
                        or os.environ.get("DICTPEN_LOADVM"))
        for path in getattr(pack, "extra_images", []) or []:
            ro = ",readonly=on" if extra_ro else ""
            args += ["-drive",
                     f"file={cls._extra_copy(pack, path)},if=virtio,format=raw"
                     f",cache=writeback{ro}"]
        return args


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
# 注册表：**自动发现** `runtimes/*.py` 里的 Runtime 子类
#
# 加一个平台 = 往这个目录丢一个 .py（类名随意，只要 `name` 唯一），不必改本文件。
# 三条契约（写插件时请遵守）：
#   * runtime 模块的顶层**只做定义、不做副作用** —— 发现过程会 import 它；
#   * 试验/半成品用 `_` 开头命名，发现过程会跳过它们；
#   * 单个模块 import 失败**不会**拖垮框架，问题收集在 `load_errors()` 里，
#     由 `dpctl packs` 打出来。
# --------------------------------------------------------------------------- #
import importlib                                              # noqa: E402
import pkgutil                                                # noqa: E402

from . import rv1106_linux                                    # noqa: E402
from . import cv18xx_linux                                    # noqa: E402
from . import rockchip_ext4                                   # noqa: E402

# 显式种子：即使某个模块被改名成 `_xxx.py`（扫描会跳过）也保证在册
SEED = (rv1106_linux.Rv1106LinuxRuntime,
        cv18xx_linux.Cv18xxLinuxRuntime,
        cv18xx_linux.Cv18xxLinuxErofsRuntime,
        rockchip_ext4.RockchipExt4Runtime)

_LOAD_ERRORS = []


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def _discover():
    """import `runtimes/*.py`（跳过 `_` 开头的），返回其中的全部 Runtime 子类。"""
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as e:                 # noqa: BLE001 插件目录里可能有半成品
            _LOAD_ERRORS.append(f"{mod.name}.py 导入失败：{type(e).__name__}: {e}")
    return list(_all_subclasses(Runtime))


def _registry():
    classes = list(SEED) + [c for c in _discover() if c not in SEED]
    reg = {}
    for cls in classes:
        if not getattr(cls, "name", ""):
            _LOAD_ERRORS.append(f"{cls.__module__}.{cls.__name__} 没有 name，已跳过")
            continue
        if cls.name in reg:
            _LOAD_ERRORS.append(f"runtime 名字重复：{cls.name}"
                                f"（{reg[cls.name].__module__} 与 {cls.__module__}）")
            continue
        reg[cls.name] = cls
    return reg


REGISTRY = _registry()


def names():
    return sorted(REGISTRY)


def load_errors():
    """发现过程中的问题（半成品插件、重名、缺 name…）；空 = 全部正常。"""
    return list(_LOAD_ERRORS)


def available():
    """[(name, note)]，供 `dpctl packs` / 报错提示列出。"""
    return [(n, REGISTRY[n].note) for n in names()]


def get(name):
    try:
        return REGISTRY[name]()
    except KeyError:
        raise UnsupportedPlatform(
            f"该平台 runtime 未实现：{name!r}；框架现有：{', '.join(names()) or '（无）'}")