"""真机联调：连运行中的 pen VM（QMP），取一帧、按各显示模式离屏渲染成 PNG，并可注入点击。

不依赖 QEMU 用什么 display 后端（QMP 是独立通道），所以 VM 以 -display gtk 或 none
跑着都能测。用法（需要 Python 3.13 + PySide6）：

    python vmgui/tests/smoke_live.py                 # 只渲染
    python vmgui/tests/smoke_live.py --tap 280 85    # 渲染后再在 guest 坐标点击一下
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtWidgets import QApplication                     # noqa: E402

from vmgui.config import Config                               # noqa: E402
from vmgui.core import events as ev                            # noqa: E402
from vmgui.core import logging as vlog                         # noqa: E402
from vmgui.core.session import Session                         # noqa: E402
from vmgui.view.canvas import Canvas                           # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vm")


def wait_frame(app, frames, timeout=20.0):
    deadline = time.time() + timeout
    while not frames and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    return frames[-1] if frames else None


def diff_ratio(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    n = sum(1 for i in range(0, len(a), 997) if a[i] != b[i])
    return n / ((len(a) + 996) // 997)


def render_variants(cfg, frame) -> None:
    cases = [
        ("fit-2x", (1120, 340), "fit", 0, None),
        ("fit-letterbox", (400, 300), "fit", 0, None),
        ("stretch", (900, 200), "stretch", 0, None),
        ("one-to-one", (560, 170), "1:1", 0, None),
        ("fit-rot90", (300, 900), "fit", 90, None),
        ("free-zoom3", (700, 400), "free", 0, None),
        ("crop-region", (300, 120), "fit", 0,
         {"enabled": True, "x": 100, "y": 20, "w": 200, "h": 80}),
    ]
    os.makedirs(OUT, exist_ok=True)
    for name, size, mode, rot, crop in cases:
        cfg.set("display.mode", mode)
        cfg.set("display.rotation", rot)
        cfg.set("display.scale_algo", "nearest")
        cfg.set("display.zoom", 3.0 if mode == "free" else cfg.get("display.zoom"))
        cfg.set("display.crop", crop or {"enabled": False, "x": 0, "y": 0, "w": 0, "h": 0})
        canvas = Canvas(cfg)
        canvas.set_frame(frame)
        canvas.resize(*size)
        img = canvas.grab()
        path = os.path.join(OUT, f"vmgui-{name}.png")
        img.save(path)
        t = canvas.transform()
        print(f"  {name:16s} widget={size[0]}x{size[1]} -> {img.width()}x{img.height()}"
              f"  sx={t.sx:.3f} disp={t.disp_w:.1f}x{t.disp_h:.1f}"
              f" off=({t.ox:.1f},{t.oy:.1f})  {os.path.basename(path)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--uri", default="tcp:127.0.0.1:4446")
    args = ap.parse_args()

    vlog.setup("info")
    cfg = Config.load(
        cli_overrides={"connection": {"uri": args.uri}},
        profiles=["pear-a6pro"], user_path=os.devnull)
    app = QApplication(sys.argv[:1])

    frames: list = []
    errors: list = []
    states: list = []
    session = Session(cfg)
    session.bus.on(ev.FRAME, frames.append)
    session.bus.on(ev.ERROR, errors.append)
    session.bus.on(ev.STATE, lambda s, d: states.append((s, d)))
    session.start()

    frame = wait_frame(app, frames)
    if frame is None:
        print("FAIL: 15s 内没取到帧；states=%r errors=%r" % (states, errors))
        session.stop()
        return 1
    print(f"OK 取帧: {frame.width}x{frame.height} {len(frame.pixels)}B fmt={frame.fmt}"
          f"（capabilities={session.capabilities()}）")
    print("渲染各显示模式:")
    render_variants(cfg, frame)

    if args.tap:
        cfg.set("display.crop", {"enabled": False, "x": 0, "y": 0, "w": 0, "h": 0})
        x, y = args.tap
        before = frame.pixels
        session.pointer(x, y, 1, True)          # 按下（含 BTN_TOUCH）
        time.sleep(0.15)
        session.pointer(x, y, 0, True)          # 抬起
        frames.clear()
        after_frame = wait_frame(app, frames, timeout=8.0)
        if after_frame is None:
            print("FAIL: 点击后没取到帧")
        else:
            ratio = diff_ratio(before, after_frame.pixels)
            print(f"点击 ({x},{y}) 后画面变化比例 ≈ {ratio * 100:.2f}%"
                  f"（0% 表示 UI 无可见反应；>0% 说明事件已抵达 guest 并被处理）")

    session.stop()
    if errors:
        print(f"后端报错 {len(errors)} 条: {errors[:5]}")
        return 1
    print("PASS: 无后端错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())