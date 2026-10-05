"""输入链路验证：把 QMP 事件送进 guest，再回读 guest 里的原始 evdev 事件。

链路：vmgui backend -> QMP input-send-event -> virtio-tablet(event1)
      -> touchbridge.pl -> uinput axs_ts_pen(event2) -> guest app

注意：vmgui 持有 QMP 连接期间，别的进程（如 `dpctl tap`）连不上 QMP —— QEMU 的
socket chardev 只服务一个客户端。所以这里直接用 vmgui 自己的会话发事件。

抓取用阻塞式 dd（count=N）而不是后台进程，免得 SSH 断开把后台任务带走。
    python vmgui/tests/verify_input.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "scripts"))

import dpctl                                                       # noqa: E402

from vmgui.config import Config                                   # noqa: E402
from vmgui.core import events as ev                               # noqa: E402
from vmgui.core import logging as vlog                            # noqa: E402
from vmgui.core.session import Session                            # noqa: E402

EV_NAMES = {0: "EV_SYN", 1: "EV_KEY", 2: "EV_REL", 3: "EV_ABS"}
KEY_NAMES = {272: "BTN_LEFT", 330: "BTN_TOUCH"}
ABS_NAMES = {0: "ABS_X", 1: "ABS_Y"}
SYN_NAMES = {0: "SYN_REPORT"}
DEVICES = {0: "virtio-keyboard", 1: "virtio-tablet", 2: "axs_ts_pen(uinput)"}
COUNT = 64


def decode(raw: bytes) -> list[str]:
    """16 字节 input_event（32 位 timeval）：sec,usec,type,code,value(32)。"""
    out = []
    for o in range(0, len(raw) - 15, 16):
        typ = int.from_bytes(raw[o + 8:o + 10], "little")
        code = int.from_bytes(raw[o + 10:o + 12], "little")
        value = int.from_bytes(raw[o + 12:o + 16], "little", signed=True)
        if typ == 3:
            name = ABS_NAMES.get(code, f"ABS_{code}")
        elif typ == 1:
            name = KEY_NAMES.get(code, f"KEY_{code}")
        elif typ == 0:
            name = SYN_NAMES.get(code, f"SYN_{code}")
        else:
            name = f"code{code}"
        out.append(f"{EV_NAMES.get(typ, typ):7s} {name:12s} = {value}")
    return out


def start_capture(device: int) -> threading.Thread:
    def run():
        dpctl.guest_run(
            f"dd if=/dev/input/event{device} of=/tmp/ev{device}.bin "
            f"bs=16 count={COUNT} 2>/dev/null", timeout=12)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main() -> int:
    vlog.setup("info")
    cfg = Config.load(profiles=["pear-a6pro"], user_path=os.devnull)
    session = Session(cfg)
    errors: list = []
    session.bus.on(ev.ERROR, errors.append)
    session.start()
    deadline = time.time() + 15
    while session.resolution() is None and time.time() < deadline:
        time.sleep(0.1)
    print(f"guest 分辨率: {session.resolution()}  能力: {session.capabilities()}")

    threads = [start_capture(i) for i in (1, 2, 0)]
    time.sleep(1.5)                       # 等 dd 把设备打开
    print("发送: 按下(85,85) -> 抬起 -> KEY_A(30) 按下/抬起")
    session.pointer(85, 85, 1, True)
    time.sleep(0.2)
    session.pointer(85, 85, 0, True)
    time.sleep(0.2)
    session.key(30, True)
    time.sleep(0.1)
    session.key(30, False)

    for t in threads:
        t.join(timeout=14)

    for i in (1, 2, 0):
        rc, hextext = dpctl.guest_run(f"od -An -tx1 /tmp/ev{i}.bin 2>/dev/null")
        raw = bytes(int(t, 16) for t in hextext.split())
        lines = decode(raw)
        print(f"\n=== event{i} / {DEVICES[i]}（{len(raw)} 字节 / {len(lines)} 事件）===")
        for line in lines[:26]:
            print("   ", line)
        if not lines:
            print("    （空）")

    session.stop()
    if errors:
        print(f"\n后端报错 {len(errors)}: {errors[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())