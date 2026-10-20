"""适配层接口：与协议无关的帧 / 能力 / 源与汇。

新增一种显示协议 = 实现一个 Backend；新增一种机型 = 加一个配置 profile，
核心逻辑（缩放 / 坐标变换 / 输入映射）不动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Frame:
    """与协议无关的一帧。pixels 为该 fmt 下的紧凑像素数据。"""
    pixels: bytes
    width: int
    height: int
    stride: int                  # 一行的字节数（>= width * 每像素字节数）
    fmt: str = "rgb888"          # "rgb888" | "xrgb8888" | "rgb565" | ...

    @property
    def bytes_per_pixel(self) -> int:
        return {"rgb888": 3, "bgr888": 3, "xrgb8888": 4, "rgb565": 2}.get(self.fmt, 0)


class Capabilities(NamedTuple):
    absolute_pointer: bool = False
    send_keys: bool = False
    resolution_events: bool = False    # VNC=True（有 resize 事件），QMP=False（轮询）
    clipboard: bool = False
    audio: bool = False
    hotplug: bool = False


@runtime_checkable
class FrameSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> Optional[Frame]: ...
    def resolution(self) -> Optional[tuple[int, int]]: ...
    def capabilities(self) -> Capabilities: ...


@runtime_checkable
class InputSink(Protocol):
    def pointer(self, x: int, y: int, buttons: int, absolute: bool) -> None: ...
    def key(self, code: int, down: bool, mods: int) -> None: ...
    def capabilities(self) -> Capabilities: ...


class Backend(Protocol):
    """组合；允许只读（source 有效）或只发（sink 有效）。"""
    source: Optional[FrameSource]
    sink: Optional[InputSink]


# 指针按键位（与 Qt::MouseButton 的常见取值解耦，用自有位定义）
BUTTON_LEFT = 1
BUTTON_MIDDLE = 2
BUTTON_RIGHT = 4


class PointerMode:
    AUTO = "auto"
    ABSOLUTE = "absolute"
    RELATIVE = "relative"

    @staticmethod
    def resolve(configured: str, caps: Capabilities) -> str:
        """按配置 + 后端能力确定真正生效的模式（不假设设备名）。"""
        if configured == PointerMode.RELATIVE:
            return PointerMode.RELATIVE
        if configured == PointerMode.ABSOLUTE and caps.absolute_pointer:
            return PointerMode.ABSOLUTE
        if configured == PointerMode.ABSOLUTE and not caps.absolute_pointer:
            return PointerMode.RELATIVE
        # auto
        return PointerMode.ABSOLUTE if caps.absolute_pointer else PointerMode.RELATIVE


def content_rect(crop, fw: int, fh: int) -> tuple[int, int, int, int]:
    """裁剪声明 -> 内容矩形 ``(cw, ch, x0, y0)``。**唯一的实现**（决策 #35 / `33-…` §0.2）。

    视图（`view/canvas.py` 的尺寸、`input/mapper.py` 的坐标变换）与后端
    （`backends/qmp.py` 的绝对轴换算）都调这一个函数 —— 两处各写一遍公式就会在
    ``crop.x/y > 0`` 时分叉（一边 ``max(1,…)``、一边 ``cw<=0`` 回落整帧），
    而分叉的表现正是"点了没反应 / 右半屏失灵"。

    原点约定：**恒为 0**。裁剪永远取左上角那块面板，所以负/越界一律钳回 0，
    ``x/y`` 非 0 时按 0 处理（调用方若真需要偏移，得先在这里显式加语义）。
    """
    fw, fh = max(1, int(fw)), max(1, int(fh))
    if crop and crop.get("enabled"):
        cw, ch = int(crop.get("w") or 0), int(crop.get("h") or 0)
        if cw > 0 and ch > 0:
            return max(1, min(cw, fw)), max(1, min(ch, fh)), 0, 0
    return fw, fh, 0, 0