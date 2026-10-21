"""控制台主窗口：设备视图 + 常驻状态栏 + 可收起右抽屉（`30-console-ui.md` §1 B 布局）。

三条纪律照抄不改：

* 设备视图**只画宿主收到的那帧**，尺寸从帧读、**按面板声明裁剪**（决策 #8 / #27）。
* 触摸**单点**，滚轮 → 一次整屏 swipe（决策 #9）。
* 必须有一条**不依赖 VM 通道**的日志出口 —— 抽屉「串口」读宿主 `vm\\serial.log`（决策 #7）。

空态文案与按钮见 `31-console-copy.md` §3；判据来自 `core/rules.py`，采集来自
`core/probe.py`。本文件只负责把它们摆出来，不做判定、不发探针命令。
"""
from __future__ import annotations

import os
import re
import posixpath
import threading
import time
from collections import deque
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget,
                               QSlider,
                               QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSizePolicy, QStackedWidget, QStatusBar,
                               QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from ..core import events as ev
from ..core import logging as vlog
from ..core import probe as P
from ..core import rules as R
from ..input import mapper
from ..input.grab import PointerGrab
from ..input.keymap import KeyMapper
from ..view import scaling
from ..view.canvas import Canvas
from . import harness

log = vlog.get("console")

STATUS_BAR_PX = 26            # #4：一行 token 串 + hairline 的高度
PROBE_INTERVAL_MS = 5000      # #12：探针 5 s 一轮（帧那条另算）
SERIAL_INTERVAL_MS = 1000     # 串口尾巴不依赖 VM 通道，读宿主文件很便宜
# 「取帧」那格的断流阈值 = max(2 s, 请求节奏 ×6)。两个数都是**为了不误伤**：
# ×6 让"慢但活着"的流（客机忙时一次截屏要 900 ms）不闪成断流；2 s 的下限是因为
# 心跳是 1 s 一跳，比这更短只会抖。实测节奏 216~240 ms（#50 / Q60）→ 2 s ≈ 少 8 帧。
RATE_STALE_FLOOR_S = 2.0
RATE_STALE_FACTOR = 6.0
RATE_TICK_MS = 1000           # 只为重估「取帧」一格：断流后没有别的定时器会去碰它
# 实际尺寸与"我们请求的那对"差多少才算用户拖过窗口。Qt 会因为布局最小尺寸 / 屏幕钳制
# 把一个请求改掉几像素，而且那类调整可能**延后投递** —— 精确比对会把程序自己的 resize
# 误判成用户接管，从此窗口不再跟着 guest 分辨率走。
RESIZE_TOLERANCE_PX = 16

TOOLTIPS = {"启动": "dpctl up --pack <名> --display none（控制台自己占 QMP）；"
                    "起的是工具条「设备」那颗上选着的这台",
            "关机": "dpctl down（先请 guest poweroff，超时才收进程）",
            "重启": "关机后立刻按同包再起一次（不抹状态盘）",
            "截图": "通过 QMP 获取 PNG 截图（比 PPM 小 8.6 倍）",
            "设备": "选择控制台要管理的设备；选择后不会自动启动，请按「启动」。\n"
                    "缺少镜像的设备灰显且不可选；虚拟机运行期间不可切换（各设备使用独立状态盘与端口），"
                    "确认框提供「关闭并切换」。",
            "左上角返回": "按面板比例点击左上角返回位置；用于没有返回控件的拦截页"
                          "（键盘 Esc/Backspace 无效）",
            "暂停画面": "停下来看一帧；再按一次继续（继续时会立刻补一帧）",
            "抽屉": "展开/收起右侧（Tab）"}

# `39` §2 #2 / §5 #8：面板里那行引导**不许**出现 `dpctl`（人不知道它是什么）。
SHELL_PLACEHOLDER = "在这里输入 guest 命令，回车执行 · 例：uptime"
SHELL_INPUT_PLACEHOLDER = "输入 guest 命令，回车执行"

# 「文件」页的默认落点：文档里那条 canonical 例子就是 `/userdata`（`dpctl put a.txt /userdata/a.txt`）。
# ⚠ 它**就是状态盘**，所以这一页必须一直显示"还剩多少"（`42` §2.2）。
FILE_DEFAULT_DIR = "/userdata"

# 「装 miniapp」那一行（人点名要的功能）：只吃 `.amr`、落点固定 `/userdata/<原名>`、
# **落地成功了才发安装命令**。对话框过滤器就写这一个类型 —— 这一行不是通用文件行，
# 别的文件要用上面那行「放进去」（误选一个 `.so` 装下去是要改设备的）。
AMR_FILTER = "miniapp 包 (*.amr)"
AMR_SUFFIX = ".amr"

# Q49 那条警告的出口 `看这个目录` 填的是**列目录的命令**，不是「文件」页"取回来"那格的路径。
# 原因（千问读码，`31` §9 原来那条设计是错的）：`dpctl get` 先 sftp `get` 再回落 `ssh cat`，
# 两头都只吃**单个文件**（`dpctl.py:1128-1148`）—— 把目录填进"取回来"只会换来一次
# "不是常规文件"的失败回执，比不给出口更糟。命令里只用 `ls -l` + `head`（探针本来就在用
# `ls | wc -l`、`tail -c`，busybox 这两样实测有）；**不用 `-S`**，那个在最弱的 rv1106 一族没验过。
COREFILE_DIR = "/userdisk/corefile"
COREFILE_LS_CMD = f"ls -l {COREFILE_DIR} | head -40"

# 空态正文里单行最多显示多少个字符。串口里出现过 **4111 字符**的 `echo <base64>` 回显
# （Q48，千问实测）：`QLabel` 的 wordWrap 只在词边界折行，那种没有空格的超长行会原样量宽
# → 窗口被撑到 **5950 px**、右侧全在屏外。所以这里**硬折**（按字符切），不指望 wordWrap。
TAIL_LINE_MAX = 100


def _wrap_hard(text: str, width: int = TAIL_LINE_MAX) -> str:
    """把过长的单行按字符硬折成多行（`QLabel` 的 wordWrap 对无空格长行无效）。"""
    out = []
    for line in text.splitlines() or [""]:
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    return "\n".join(out)


def fresh_warning_text(pack: str, path: str, size: int) -> str:
    """`--fresh` 二次确认弹窗的正文（决策 #15 / #71）。

    三条硬要求：① **写出盘名与大小**（让人认得出自己丢的是什么）；② 写清**作用域**只这一台；
    ③ **不许**写"清除数据"这类抽象词，也不许承诺"可以恢复"。
    """
    name = os.path.basename(path) if path else f"state-{pack}.img"
    where = f"vm\\{name}（{harness.format_size(size)}）"
    return (f"这会把这一台（{pack}）的状态盘 {where} 删掉重建 —— 只影响这一台，\n"
            f"别的包的 state-*.img 不受影响。\n\n"
            f"盘上的东西（/userdata 的配置与已下载内容、/userdisk 的应用与档案）会全部丢失，\n"
            f"不可恢复。重建前会自动复制一份 {name}.bak 留档。")


def disk_full_note(pct, core) -> tuple[str, str]:
    """把 `disk-full` 这条警告翻成人话，并给出它属于哪一档（档名拿去当去重键）。

    为什么要分档（Q49）：`rules.py:163-165` 是**或** —— a6pro 实测「盘只用 20% + 目录 626 项」
    也命中，旧文案照实印成 **「状态盘快满：20%」**，一句话里自己打自己。分档之后：只有目录超
    就不说"快满"（那是**稳定性**不是空间），只有盘超就不提目录数（`core=None` 会被印成字面
    `None`，#30）。措辞与四分支表见 `31-console-copy.md` §9。
    """
    over_pct = pct is not None and pct >= R.DEGRADE_STATE_PCT
    over_core = core is not None and core > R.DEGRADE_CORE
    if over_pct and over_core:
        return (f"状态盘快满：{pct}%　／　/userdisk/corefile 堆了 {core} 项", "pct+core")
    if over_pct:
        return (f"状态盘快满：{pct}%　→　要腾空间先清 /userdisk/corefile", "pct")
    tail = (f"盘只用了 {pct}%，不是故障" if pct is not None
            else "盘用了多少这一轮没采到")
    return (f"/userdisk/corefile 堆了 {core} 项 —— 这里面多半不是 core 转储"
            f"（a6pro 实测 750 项、真 core 0 个，全是 Capframe 日志）；{tail}", "core")


def identity_note(values: dict) -> str:
    """抽屉「guest 应用」页首行：**虚拟热点 + 设备身份并列**（决策 #75 / #78）。

    为什么并列、不给单一结论：拦路页（"开始激活"）实测**至少三种成因同形**（没 AP / 没身份 /
    jsapi 链断）—— 按优先级猜一个，就会把人支去改一个没坏的东西。
    这里只摆**我们真的能观测到**的两把钥匙，第三把（jsapi）明确写"只能看日志"。
    """
    if not values:
        return ""
    ap = P.ap_state(values)
    ap_txt = {"up": "已开", "cfg": "配了但 hostapd 没起来",
              "off": "未开（这个包没开虚拟热点）"}.get(ap, "未知（这轮没采到）")

    def one(tail) -> str:
        if tail is None:
            return "未知"
        return "空" if tail == "" else "…" + tail

    sn, uuid = P.identity_tail(values)
    return (f"虚拟热点：{ap_txt}　·　设备身份：sn {one(sn)} / uuid {one(uuid)}"
            f"　→　进应用若被要求激活，这两件都可能是原因；第三把钥匙（jsapi 加载链）"
            f"只能看串口/应用日志，界面不给结论。")


def app_adapt_note(values: dict) -> str:
    """「guest 应用」页第二行：**应用注册表逐条对盘**的体检结果（Q70）。

    四态分开说，绝不共用一句话（#30 那条老规矩）：整轮没值 ／ 这台没有注册表 ／ 都在 ／ 有缺。
    只报 **appid**（ASCII）不报应用名：注册表里是中文，而串口通道是 cp936（Q52/Q68 实测会把
    中文解成豆腐块），宁可让人拿号去查，也不在界面上演一遍乱码。
    """
    if not values:
        return ""
    if "apps" not in values:
        return "应用体检：这一轮探针没带回 `apps` 这个键（不判）"
    adapt = P.app_adapt(values)
    if adapt is None:
        return ("应用体检：这台没有应用注册表（`/userdata/miniapp`、`/userdisk/secondary/miniapp`、"
                "`/userdisk/miniapp` 三棵都找过，没有 `pkg/packages.json`）—— "
                "没有可体检的对象，不判")
    total, bad, ids, _at = adapt
    if bad == 0:
        return (f"应用体检：注册表 {total} 条，`app.js.bin` 与注册表声明的图标"
                f"逐条都在盘上")
    show = " ".join(ids[:6]) + (" …" if len(ids) > 6 else "")
    return (f"应用体检：注册表 {total} 条，其中 {bad} 条盘上没有文件（{show}）"
            f"　→　这些在列表里没图标、点开黑屏。补回来走「文件」页装对应的 .amr")


def app_missing_ls_cmd(at) -> str:
    """警告出口的**填充内容**：一条只读的 `ls -ld`，回车由人按（不自动执行）。

    只收绝对路径：探针里 `installPath` 缺失会发 `?`，那种 token 进命令就是让客机去 glob
    单字符，宁可少列。
    """
    paths = [p for p in (at or []) if p.startswith("/")]
    return "ls -ld " + " ".join(paths[:12]) if paths else ""


# 「点左上角」应急键的落点（决策 #74）：按**面板比例**，不写绝对像素（换族就废）。
# 千问实测两颗样本**都在 s7pro 1020×240**：`规范扫描 ×`≈(44,42) = 4.3%/17.5%、
# `配置网络 <`≈(43,37) = 4.2%/15.4% → 取 **4.2% / 16%**。
# ⚠ 另外两族（a6pro/a7pro）的实测落点还没到 —— 所以下面留了**按包覆盖**的表，
#   拿到数就填一行，不必改算法（绝对像素换族就废这件事是千问自己纠正的）。
ESCAPE_TAP_RATIO = {"default": (0.042, 0.16)}

# 按住多久（ms）：与 `dpctl tap` 同口径 —— 太短会被 mousedev 当成抖动，不是一次点击。
TAP_HOLD_MS = 220


def escape_tap_point(cw: int, ch: int, pack: str = "") -> tuple[int, int]:
    """面板内容尺寸 → 「点左上角」那颗键的 guest 坐标。"""
    rx, ry = ESCAPE_TAP_RATIO.get(pack, ESCAPE_TAP_RATIO["default"])
    return int(round(int(cw) * rx)), int(round(int(ch) * ry))


def _size_text(n: int) -> str:
    """字节 → 人读得懂（**空闲空间**用）。

    与 `harness.format_size` 分开：那个的 0 是"还没有状态盘"（重建弹窗用），
    这里 0 就是 0 —— 两种场合的 0 含义不同，共用一个函数迟早说错话。
    """
    for unit, div in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def space_note(path: str, nbytes) -> str:
    """落点旁那行剩余空间（`42` §2.2）。

    `/userdata` **就是状态盘**：一边报"状态盘快满"、一边开一条往同一个盘里堆的通道，
    等于自己拆自己的报警 —— 所以这一行不是装饰。
    **采不到就写"未知"**（#30 同一条：不许把"没采到"显示成 0）。
    """
    if nbytes is None or nbytes < 0:
        return f"落点 {path} 剩余未知（这一轮没采到）"
    return f"落点 {path} 还剩 {_size_text(int(nbytes))}"


def amr_target(local: str) -> str:
    """.amr 在笔里的落点：`/userdata/<原名>`，**原名照原样带过去**（中文/空格都不改）。

    `put` 那条通道本来就是"名字照原样"的前提（sftp，`42` §2.8），所以这里只做一件事：
    把宿主路径折成笔里那一个 POSIX 路径。宿主分隔符两种都吃 —— 粘 `D:\\x\\a.amr`
    和 `D:/x/a.amr` 得到同一个落点。
    """
    name = os.path.basename(local.replace("\\", "/").rstrip("/")) or "miniapp.amr"
    return f"{FILE_DEFAULT_DIR}/{name}"


def _zoom_text(z: float) -> str:
    """#31：一位小数、去尾零 —— `1×` / `1.5×` / `2×`。`z<1` 用**正常形状**（不是降级）。"""
    s = f"{z:.1f}"
    return (s[:-2] if s.endswith(".0") else s) + "×"


def _age_text(age: float) -> str:
    """把"多久没收到帧"写成一个**粗**读数：`12 s` / `3 分` / `2 小时`。

    粗是有意的 —— 这一格挂在工具条上、每秒被心跳刷一次，给它秒级精度只会让它变成一个
    跳动倒计时，而"停了 4 分"和"停了 4 分 12 秒"对要不要动手是同一句话。单位混用
    （`s` 配 `分`）也是跟着同一行既有的写法走：`取帧 … ms` 用拉丁、`运行 35 分` 用汉字。
    """
    if age < 60:
        return f"{int(age)} s"
    if age < 3600:
        return f"{int(age // 60)} 分"
    return f"{int(age // 3600)} 小时"



# ⚠ 别用 `bluetoothctl devices` 取清单：蓝牙没上电/没有 bluetoothd 时它会**挂住**
# （实测 `dpctl sh` 直接超时 60 s）。这里只读两个不吃守护进程的地方：
# `hciconfig`（控制器状态）＋ `/var/lib/bluetooth/*/*/info`（配对库）。
BT_REPORT_CMD = ("echo @@status; btmgmt info 2>&1 | head -12; "
                 "echo @@devices; for f in /var/lib/bluetooth/*/*/info; do "
                 "[ -f \"$f\" ] || continue; "
                 "a=$(basename $(dirname $f)); "
                 "n=$(sed -n 's/^Name=//p' \"$f\" | head -1); "
                 "echo \"Device $a ${n:-?}\"; done")
# 扫描不经过 bluetoothd：客机固件里 bluetoothd 由笔的蓝牙入口拉起，控制台没开那个入口时
# `bluetoothctl --timeout 8 scan on` 会卡在 D-Bus（实测直接挂住 60 s）。btmgmt 直接和
# 内核 HCI 说话，`--timeout` 到了就退。
BT_UP_CMD = "btmgmt power on 2>&1; btmgmt le on 2>&1"
BT_SCAN_CMD = "btmgmt --timeout 8 find 2>&1 | tail -20"


# 声音页：只读两处 —— 声卡清单与那颗 mixer 控件；控件名是 QEMU USB Audio 报的原文。
SND_CTL = "Audio Output Volume Control"
SND_REPORT_CMD = ("echo @@cards; cat /proc/asound/cards 2>&1 | head -8; "
                  "echo @@mix; amixer -c 0 sget '" + SND_CTL + "' 2>&1 | head -8")
SND_TONE = "/tmp/vm-tone.wav"


def parse_snd_report(text: str) -> dict:
    """解析 `@@cards / @@mix` 报告：有没有声卡、卡名、音量百分比、是否静音。"""
    absent, card, desc, vol, muted, ctl = False, "", "", None, False, ""
    sect = ""
    for raw in (text or "").splitlines():
        t = raw.strip()
        if t == "@@cards":
            sect = "c"
            continue
        if t == "@@mix":
            sect = "m"
            continue
        low = t.lower()
        if "no soundcards" in low or "no such device" in low:
            absent = True
        if sect == "c":
            m = re.match(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.+)$", t)
            if m:
                card, desc = m.group(2).strip(), m.group(3).strip()
            elif card and not desc and t and not t.startswith("---"):
                desc = t
        elif sect == "m":
            if t.startswith("Simple mixer control"):
                m = re.search(r"'([^']+)'", t)
                if m:
                    ctl = m.group(1)
            if t.startswith("Front Left:"):
                m = re.search(r"\[(\d+)%\]", t)
                if m:
                    vol = int(m.group(1))
                muted = "[off]" in t
    return {"absent": absent and not card, "card": card, "desc": desc,
            "vol": vol, "muted": muted, "ctl": ctl}


def parse_bt_report(text: str) -> dict:
    """解析客机那段 `@@status / @@devices` 报告（`vm/_qwen_bt_probe.sh` 同形状）。

    两种"没得玩"要分开，别都写成"出错"：
    · `hciconfig` 印 `No such device` → **这台固件没有蓝牙**（不是所有型号都有，人点名过）；
    · `bluetoothctl` 印 `No default controller available` → 有控制器但**没开**（按「开」就行）。
    """
    state, addr, name, devs = "unknown", "", "", []
    absent = powered_off = False
    sect = ""
    for raw in (text or "").splitlines():
        t = raw.strip()
        if t == "@@status":
            sect = "s"
            continue
        if t == "@@devices":
            sect = "d"
            continue
        low = t.lower()
        if ("index list with 0 items" in low or "no such device" in low
                or "can't open hci" in low):
            absent = True
        if "no default controller" in low:
            powered_off = True
        if sect == "s":
            # `btmgmt info`：`addr 01:00:..` / `current settings: powered ...` / `name dictpen-vm`；
            # 只带 `hciconfig` 的包也认（`BD Address:` / `UP RUNNING`）。
            m = (re.search(r"\baddr\s+([0-9A-Fa-f:]{17})", t)
                 or re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", t))
            if m:
                addr = m.group(1)
            m = re.search(r"^name\s+(\S.*)$", t)
            if m:
                name = m.group(1).strip()
            if t.startswith("current settings:"):
                on = "powered" in t.split(":", 1)[1].split()
                state, powered_off = ("up" if on else "down"), not on
            if "UP RUNNING" in t:
                state = "up"
            elif "DOWN" in t:
                state = "down"
        elif sect == "d":
            m = re.search(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.+)$", t)
            if m:
                devs.append((m.group(1), m.group(2).strip()))
    return {"state": state, "addr": addr, "name": name, "devices": devs,
            "absent": absent and not addr, "powered_off": powered_off}


class UiBridge(QObject):
    """后端/工作线程 → UI 线程的转接。emit 在别的线程，Qt 自动排队到此对象所在线程。"""
    frame = Signal(object)
    resolution = Signal(int, int)
    state = Signal(str, str)
    shot = Signal(str, bool, str)
    decision = Signal(object)
    dpctl = Signal(str, int, str)
    shell = Signal(str, int, str)
    file_op = Signal(str, int, str)
    space = Signal(int, str)
    machine = Signal(object)             # 「配置」页的客机侧读数（内存 / CPU / 文件系统）
    machine_note = Signal(str, int, str)  # 「配置」页动作的回执（改参数 / 扩容 / resize2fs）
    bt = Signal(str, int, str)           # 「蓝牙」页的回执（刷新 / 开关 / 扫描 / 连断）
    snd = Signal(int, str, int, str)     # 「声音」页的回执（代次 / 刷新 / 测一下 / 音量）


class ConsoleWindow(QMainWindow):
    def __init__(self, cfg, session, pack: Optional[str] = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.session = session
        self.pack = pack or harness.current_pack()
        self.keymap = KeyMapper(cfg.get("input.keymap"))
        self._pointer_mode = "absolute"
        self._decision: Optional[R.Decision] = None
        self._probe: Optional[P.Probe] = None
        self._packs: list = []
        self._pending_pack = ""                         # 「关机并切到 X」里那个 X（回执来了才生效）
        self._prev_bridge_pids: Optional[tuple] = None   # F4：记集合，不记一个 int
        self._prev_holder_pid: Optional[int] = None
        self._probe_busy = False                          # 上一轮探针没回来就不起新的
        self._shell_busy = False                          # 窗口内 shell 同时只跑一条
        self._file_busy_flag = False                      # 「文件」页同时只跑一个取/放/装
        self._bt_busy = False                             # 「蓝牙」页同时只跑一条命令
        self._snd_job = None                              # 「声音」页当前命令（可中断）
        self._snd_gen = 0                                 # 声音页代次：旧命令的回执不认
        self._last_flags_key = ""
        # 后端报上来的连接状态（`""` = 还没收到过任何一次 STATE 事件，**不等于**"没连上"）。
        # `_on_state` 以前只拿它改指针模式、把 state/detail 整个丢掉，于是"从没连上 QMP"和
        # "连上了但没画面"在界面里是同一句话 —— 见 `_note_flags` 的 `no-frame` 分支。
        self._sess_state = ""
        self._busy = False
        self._readonly = False
        # 画面是"字盒居中"的（x7 的 960×266 塞进 960×541 的画布，上下各一大条黑边）。
        # 画布上任何一点都会被 `screen_to_guest` **钳到画面边缘**再发出去 —— 于是点黑边等于
        # 在画面最上/最下一行上点了一下，人看到的就是"点在画面外，客机却动了，还动错地方"。
        # 所以要在"画面外"这一层挡住，而不是让钳位替人决定（`mapper.inside()` 本来就有）。
        self._touch_active = False    # 这一按是从画面内开始的（后续滑到画面外也要把抬起发出去）
        self._self_size = (0, 0)      # 我们最后一次"打算"的尺寸
        self._expect_resize = False   # 刚请求过 resize：下一个事件是我们的，不是用户的
        self._user_sized = False      # 用户拖过窗口，之后只改画布不改窗口
        self._refit_next_frame = False  # 换过设备：下一帧按新面板重新套窗口（见 `_on_frame`）
        self._started_at = time.time()
        self._paused = False                              # 「暂停画面」开关
        self._zoom = None                                 # None = 适合窗口（`39` §4.2 的缩放档位）
        self._frame_gaps = deque(maxlen=24)               # 取帧节奏（我们自己量的 ms）
        self._last_frame_t = 0.0
        self._gap_discard = False     # True = 下一帧的间隔**不当节奏样本**（跨暂停边界用）
        self._rate_text = ""          # 最后一次写进工具条的那串字（1 s 心跳靠它省重排）

        self._set_title()
        self._build_chrome()
        self._build_toolbar()
        self._build_status()
        self._build_drawer()

        self.bridge = UiBridge()
        session.bus.on(ev.FRAME, self.bridge.frame.emit)
        session.bus.on(ev.RESOLUTION, self.bridge.resolution.emit)
        session.bus.on(ev.STATE, self.bridge.state.emit)
        session.bus.on(ev.SHOT, self.bridge.shot.emit)
        self.bridge.frame.connect(self._on_frame)
        self.bridge.resolution.connect(self._on_resolution)
        self.bridge.state.connect(self._on_state)
        self.bridge.shot.connect(self._on_shot)
        self.bridge.decision.connect(self._on_decision)
        self.bridge.dpctl.connect(self._on_dpctl)
        self.bridge.shell.connect(self._on_shell)
        self.bridge.file_op.connect(self._on_file_op)
        self.bridge.space.connect(self._on_space)
        self.bridge.machine.connect(self._on_machine)
        self.bridge.machine_note.connect(self._on_machine_note)
        self.bridge.bt.connect(self._on_bt)
        self.bridge.snd.connect(self._on_snd)

        self.canvas.mouse_moved.connect(self._on_mouse_move)
        self.canvas.mouse_pressed.connect(self._on_mouse_press)
        self.canvas.mouse_released.connect(self._on_mouse_release)
        self.grab = PointerGrab(self.canvas)

        self._probe_timer = QTimer(self)
        self._probe_timer.timeout.connect(self._kick_probe)
        self._probe_timer.start(PROBE_INTERVAL_MS)
        self._serial_timer = QTimer(self)
        self._serial_timer.timeout.connect(self._refresh_serial)
        self._serial_timer.start(SERIAL_INTERVAL_MS)
        self._refresh_serial()
        # 「取帧」一格**必须**有东西在断流之后来敲它一次。旧写法只在帧到达和按暂停时刷，
        # 于是流一断，那一格就停在最后量到的数上、永远用现在时报一个已经结束的节奏
        # （Q64 第一条）。这一跳不读文件、不探活、不开进程，只做一次减法 + 条件 setText。
        self._rate_timer = QTimer(self)
        self._rate_timer.timeout.connect(self._refresh_rate_label)
        self._rate_timer.start(RATE_TICK_MS)
        QTimer.singleShot(0, self._kick_probe)

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_chrome(self) -> None:
        self.canvas = Canvas(self.cfg)
        self.empty = _EmptyPane()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.empty)

        self.drawer = QWidget()
        self.drawer.setObjectName("drawer")       # QSS 只在这里给抽屉上底色 + 左侧 hairline
        self.drawer.setMinimumWidth(300)
        self.drawer.setMaximumWidth(420)
        self._drawer_open = True

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.drawer, 0)
        self.setCentralWidget(central)

    def _set_title(self) -> None:
        """`39` §2 #4：标题里不出现内部词（"设备视图"是 `view` 的名字），只写包名。"""
        self.setWindowTitle(f"词典笔控制台 · {self.pack}" if self.pack else "词典笔控制台")

    def _refresh_rate_label(self) -> None:
        """把取帧节奏写成一行读数（`39` §2 #3 / #69）。四档，**每档都只说量到的事**：

        * `取帧 —`：本次会话一帧都没收到过 —— 没量过就没有数，不许编（#30）。
        * `取帧 233 ms（199~264）`：**当前还在流**时才有这一档。断流之后旧读数必须让位，
          否则这一格会用现在时报一个已经结束的节奏（Q64 第一条：同一行状态栏写
          `帧 …（上次）`，工具条却写"正在每 233 ms 出一帧"—— 两个读数两套真相）。
        * `取帧 停了 4 分`：收到过，然后断了，而**不是你按的暂停**。它说的只是
          "控制台这一侧没收到帧"，不等于"客机卡住了" —— 出口在 tooltip 里。
        * `取帧 暂停`：用户自己的动作，优先级最高（否则"暂停"会被"停了"覆盖成故障味）。
        """
        if self._paused:
            self._set_rate("取帧 暂停")
            return
        if not self._last_frame_t:
            self._set_rate("取帧 —")
            return
        age = time.monotonic() - self._last_frame_t
        poll = max(0.005, int(self.cfg.get("display.frame_poll_ms", 30)) / 1000.0)
        if age > max(RATE_STALE_FLOOR_S, RATE_STALE_FACTOR * poll):
            self._set_rate("取帧 停了 " + _age_text(age))
            return
        if not self._frame_gaps:
            self._set_rate("取帧 —")
            return
        gaps = sorted(self._frame_gaps)
        self._set_rate(f"取帧 {sum(gaps) / len(gaps):.0f} ms"
                       f"（{gaps[0]:.0f}~{gaps[-1]:.0f}）")

    def _set_rate(self, text: str) -> None:
        """只在字**真的变了**时才写标签。心跳是 1 s 一跳，`停了 4 分` 会连着 60 次一模一样，
        而 `QLabel.setText` 不看内容是否相同、每次都重排版 —— 省掉它，别的都不省。"""
        if text == self._rate_text:
            return
        self._rate_text = text
        self.lbl_rate.setText(text)

    def _build_toolbar(self) -> None:
        tb = QToolBar("控制台", self)
        tb.setMovable(False)
        self.addToolBar(tb)
        self.toolbar = tb                       # 算可用区要它的高度
        self.act_start = self._tb_action(tb, "启动", self._start_clicked)
        self.act_fast = self._tb_action(tb, "快速启动", self._fast_start_clicked)
        self.act_fast.setToolTip("从快照恢复上次首页状态（约 6 秒；快照不包含音频）")
        self.act_fast_save = self._tb_action(tb, "保存快照", self._fast_save_clicked)
        self.act_fast_save.setToolTip("关机后保存当前状态为快速启动快照（约 1.5 分钟；不包含音频）")
        self.act_stop = self._tb_action(tb, "关机", self._stop_clicked)
        # 「重启」= 看得见两步的 down + up（决策 #71）；「立即断电」在抽屉里（破坏性动词
        # 不该占主区，两颗行为相近的破坏性按钮比一颗更糟 —— 千问否掉了"优雅/强制"并列）。
        self.act_restart = self._tb_action(tb, "重启", self._restart_clicked)
        self.act_shot = self._tb_action(tb, "截图", self._shot_clicked)
        # 「点左上角」应急键（#74）：触摸链断掉时**唯一不依赖 guest UI 的出口** ——
        # 拦路页（"开始激活"）实测是整页纯黑 + 一颗发光按钮、**无 × 无返回**，
        # 而 `sendkey esc/backspace` 对它零作用（`40` §5.2）。
        self.act_escape = self._tb_action(tb, "左上角返回", self._escape_clicked)
        # 实体键（Q88/Q90/Q92）：语义键名在这里，具体码**按包**从
        # packs/<名>/patches/vm-keys.env 读（客机 S21vm-keys 读同一份起 uinput）。
        # x6pro: Home 102 / 音量 115,114；s6pro/x5pro: Home 139；s7/a6/a7: Home 102；x7 无。
        # 播放键 164 不摆：厂商 key config 里没有 play_key，固件不建播放模块。
        self._key_actions = {}
        for _name, _t, _tip in (("home", "Home", "发送实机 Home 键（返回桌面）"),
                                ("vol_down", "音量－", "发送实机音量减键"),
                                ("vol_up", "音量＋", "发送实机音量加键")):
            _act = self._tb_action(tb, _t, lambda _=False, n=_name: self._send_key(n))
            _act.setToolTip(_tip)
            _act.setProperty("tip_ok", _tip)
            self._key_actions[_name] = _act
        self._sync_key_actions()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        # `39` §2 #3：动词到抽屉键之间那条空档白送 —— 放**我们自己量的**取帧节奏，
        # 让人知道"慢在哪"（`38` §1 的反例：不许按"换了 CSS 就不卡"承诺）。
        self.lbl_rate = QLabel("取帧 —")
        self.lbl_rate.setObjectName("rate")
        self.lbl_rate.setToolTip(
            "控制台自己在帧到达处量的间隔（200 ms 是我们请求的节奏，不是承诺）。\n"
            "取帧 停了 N —— 这段时间**一帧都没收到**，说的是控制台这一侧收没收到，"
            "不等于客机卡住；要看机器，读状态栏和抽屉。\n"
            "取帧 暂停 —— 你按的那个开关，不算故障。")
        tb.addWidget(self.lbl_rate)
        self._build_zoom_menu(tb)
        self._build_pack_menu(tb)
        self.act_drawer = self._tb_action(tb, "抽屉 ▾", self._toggle_drawer, checkable=True)
        self.act_drawer.setShortcut(QKeySequence("Tab"))
        self.act_drawer.setChecked(True)

    def _build_zoom_menu(self, tb) -> None:
        """缩放档位（`39` §4.2）：扁屏在 `fit` 下由**高度**定倍率（1020×240 只能 1×），
        画面在宽屏上偏小 —— 人想要的是"**我能自己拧**"，不是我们替他算好。

        「适合窗口」= 现在那套自动算法（跟着分辨率走、拖过窗口就停）。
        选了具体倍率就**锁住**（分辨率变了也按这个倍率重排），点回「适合窗口」才解锁。
        """
        self.btn_zoom = QToolButton()
        self.btn_zoom.setPopupMode(QToolButton.InstantPopup)
        self.btn_zoom.setToolTip("画面缩放：「适合窗口」跟着分辨率自动算，其余是锁定的固定倍率")
        menu = QMenu(self.btn_zoom)
        self.zoom_actions = {}
        for label, z in (("适合窗口", None), ("1×", 1.0), ("1.5×", 1.5),
                         ("2×", 2.0), ("3×", 3.0)):
            a = QAction(label, self, checkable=True)
            a.triggered.connect(lambda _=False, zz=z: self._set_zoom(zz))
            menu.addAction(a)
            self.zoom_actions[label] = a
        self.btn_zoom.setMenu(menu)
        tb.addWidget(self.btn_zoom)
        self._refresh_zoom_button()

    def _refresh_zoom_button(self) -> None:
        z = self._zoom
        self.btn_zoom.setText("缩放 " + ("适合" if z is None else _zoom_text(z)))
        for label, act in self.zoom_actions.items():
            act.setChecked((z is None and label == "适合窗口")
                           or (z is not None and label == _zoom_text(z)))

    def _set_zoom(self, z) -> None:
        """切档：始终改画布倍率；只有没被用户拖过窗口时才顺手把窗口套上去（#57/#60）。"""
        self._zoom = z
        self._refresh_zoom_button()
        if z is None:
            self._user_sized = False          # 「适合窗口」= 回到"跟着分辨率走"
        self._fit_window()
        if self._decision is not None:
            # 状态栏那个 `· 1×` 是**实际**倍率（画布变换算出来的），切档后必须重画，
            # 否则菜单写着 2×、读数还停在 1×。
            self.lbl_status.setText(self._status_text(self._decision))

    def _build_pack_menu(self, tb) -> None:
        """「设备」那颗下拉（人点名：「控制台没有切换设备选项」）。

        形状抄「缩放」：InstantPopup 的 QToolButton，一台一行、正在管的那台打勾。
        为什么**不做**成每台一颗「起这台」（另一条候选）：那颗键把"选目标"和"动手"合成一步，
        而这一族最贵的错就是对着错的机器动手 —— 七台各有一套状态盘、面板、rootfs。
        选择留在这里，执行还是「启动」那颗（它是灰的时候自己会说为什么）。
        """
        self.btn_pack = QToolButton()
        self.btn_pack.setPopupMode(QToolButton.InstantPopup)
        self.btn_pack.setToolTip(TOOLTIPS["设备"])
        self.pack_menu = QMenu(self.btn_pack)
        self.btn_pack.setMenu(self.pack_menu)
        self._pack_menu_sig: Optional[tuple] = None
        tb.addWidget(self.btn_pack)
        self._refresh_pack_menu()

    def _refresh_pack_menu(self) -> None:
        """重建菜单，**只在清单或选中项真的变了之后**才重建。

        每 5 s 一轮探针都会敲这里；不设闸门就是每轮 new 七个 QAction —— 这条 UI 线程
        已经为"每帧重排"付过两次账（#69 / Q64 那一族）。签名里把 `ready` 与面板一起带上：
        补完镜像、或「重扫 packs」之后，灰着的那行要能自己亮起来。
        """
        sig = tuple((str(p.get("name", "?")), bool(p.get("ready")),
                     str(p.get("panel") or "")) for p in self._packs) + (self.pack or "",)
        if sig == self._pack_menu_sig:
            return
        self._pack_menu_sig = sig
        self.pack_menu.clear()
        if not self._packs:
            self.pack_menu.addAction("还没扫到固件包").setEnabled(False)
        for p in self._packs:
            name = str(p.get("name", "?"))
            ready = bool(p.get("ready"))
            panel = str(p.get("panel") or "")
            a = self.pack_menu.addAction(
                f"{name}  {panel}" if ready and panel else
                (name if ready else f"{name}  不可用"))
            a.setCheckable(True)
            a.setChecked(name == self.pack)
            a.setEnabled(ready)                 # #28：缺镜像的那台不许被选中
            if ready:
                a.triggered.connect(lambda _=False, n=name: self._pack_chosen(n))
                a.setToolTip(f"soc {p.get('soc') or '?'} · runtime "
                             f"{p.get('runtime') or '?'} · 选它不会自己起机")
            else:
                # 错误串用 packlib 的**原文**（`31-console-copy.md` §8：控制台别自己造文案）
                bad = list(p.get("missing") or []) + list(p.get("md5_bad") or [])
                a.setToolTip("；".join(str(x) for x in bad)
                             if bad else str(p.get("reason") or "镜像不可用"))
        # 标签里**别自己写 ▾**：`InstantPopup` 的 QToolButton 已经画了一颗箭头，手动再加
        # 就是截图上那种「设备 x5pro ▾ ⌄」双箭头（真窗复核时抓到的）。参照系是「缩放」。
        self.btn_pack.setText(f"设备 {self.pack or '—'}")

    def _pack_chosen(self, name: str) -> None:
        if not name or name == self.pack:
            return            # 点的就是正在管的这台：不重画、不写日志、不动 VM
        if harness.vm_running():
            self._confirm_switch_while_running(name)
            return
        self._apply_pack(name)

    def _confirm_switch_while_running(self, name: str) -> None:
        """跑着的时候不许直接换目标：这一屏**整个是照着一台在跑的机器算的**（画面、探针、
        命令页、状态盘那一格）。改了目标不等于那些跟着改 —— 只会让人以为看到的就是新那台。
        所以先把那台停下来，再改目标。

        默认键是「取消」（#15/#71 那一族：回车不该落在"停掉你正在看的这台"上）；
        同意之后**只做关机这一步，不自动起机** —— 起机要 3~6 分钟，那是人的决定（#71）。
        """
        cur = harness.current_pack() or self.pack or "?"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("切换设备")
        box.setText(f"当前运行设备为 {cur}，是否切换至 {name}？")
        box.setInformativeText(
            f"将先关闭 {cur}（约 10–40 秒），再将控制台目标切换为 {name}。\n"
            f"不会自动启动；请按「启动」。\n"
            f"两台设备的状态盘、面板尺寸与镜像相互独立，不能同时管理。")
        ok = box.addButton(f"关闭并切换至 {name}", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not ok:
            self._note(f"操作已取消；仍管理 {cur}。")
            return
        self._pending_pack = name
        self._busy = True
        self.act_start.setEnabled(False)
        threading.Thread(target=lambda: self.bridge.dpctl.emit("关机", *harness.stop_vm()),
                         daemon=True).start()

    def _apply_pack(self, name: str, announce: bool = True) -> None:
        """改目标，并把所有**按包算**的读数一起换掉。

        必须一起清掉的是旧那台的**客机**读数（`_m_live`）：留着它，「配置」页会写成
        「下次 2G（新包）｜客机看到 1.93 GiB（旧那台）」—— 两个数不是同一台机器的，
        这正是 #30 那一族（不许拿别人的实测值给这台作证）。

        `announce=False` 给"顺手替他选上、但同一刻就要起机"那条路用（`_start_clicked`）：
        这时候写「按「启动」起这台」是在指一个他已经按过的键。
        """
        self.pack = name
        self._pending_pack = ""
        self._set_title()
        # 画面上的一切都**属于旧那台**：最后一帧、缩放档、被拖过的窗口形状。换目标还留着它们，
        # 人看到的就是"新设备名字下挂着旧机器的画面"（再叠上旧面板的裁切线，像是被裁/错位）。
        # 清帧不是"看不见了"——空态会写「等待虚拟机画面…」，新机第一帧到了自己套窗口。
        self.canvas.clear()
        self._zoom = None
        self._user_sized = False
        self._refit_next_frame = True
        self._refresh_zoom_button()
        self._m_live = {}
        on_page = (self.tabs.widget(self.tabs.currentIndex())
                   is getattr(self, "machine_page", None))
        self._refresh_machine(ask_guest=on_page)
        if self._decision is not None:
            self.lbl_status.setText(self._status_text(self._decision))
        self._refresh_pack_menu()      # 打勾与按钮上的包名立刻跟上，不等下一轮探针
        self._sync_key_actions()       # 实体键是**按包**的（只有 x6pro 有 fake-keys.pl）
        if announce:
            self._note(f"目标已切换为 {name}；按「启动」开机。")
        self._kick_probe()

    def _tb_action(self, tb, text, slot, checkable=False) -> QAction:
        a = QAction(text, self, checkable=checkable)
        a.setToolTip(TOOLTIPS.get(text.split()[0], ""))
        a.triggered.connect(slot)
        tb.addAction(a)
        return a

    def _build_status(self) -> None:
        bar = QStatusBar()
        bar.setFixedHeight(STATUS_BAR_PX)
        bar.setSizeGripEnabled(False)
        self.lbl_status = QLabel("正在收集状态…")
        bar.addWidget(self.lbl_status)
        self.setStatusBar(bar)

    def _build_drawer(self) -> None:
        lay = QVBoxLayout(self.drawer)
        lay.setContentsMargins(0, 0, 0, 0)
        # `39` §2 #7：原工具条上的「手动刷新」删掉（200 ms 轮询正常时它永远不需要按，
        # 它的存在反而在说"画面可能不可信"），换成抽屉顶上这颗开关：
        # 暂停 = 停下来看一帧；继续 = 立刻补一帧（#29/#53 那个"绕开节奏取一帧"的能力没丢）。
        # **两行排**：抽屉最窄 300 px，三个控件挤一行会各自被截断（真窗口实测过）。
        head = QWidget()
        head_lay = QVBoxLayout(head)
        head_lay.setContentsMargins(10, 6, 10, 4)
        head_lay.setSpacing(4)
        row1 = QHBoxLayout()
        self.chk_pause = QCheckBox("暂停画面")
        self.chk_pause.setToolTip(TOOLTIPS["暂停画面"])
        self.chk_pause.toggled.connect(self._pause_toggled)
        row1.addWidget(self.chk_pause)
        row1.addStretch(1)
        head_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        # 「立即断电」：底层现象是"拔插头"（`dpctl down --force`），不进主区（#71）。
        self.btn_cut = QPushButton("立即断电")
        self.btn_cut.setToolTip("不请 guest 关机，直接收 QEMU 进程 —— 相当于拔插头，"
                                "笔里没写回的东西会丢。日常请用工具条上的「关机」。")
        self.btn_cut.clicked.connect(self._cut_clicked)
        row2.addWidget(self.btn_cut)
        # `--fresh` 的入口只放这里、且**必须**二次确认（决策 #15/#71）：它抹的是这一台的
        # `vm/state-<包>.img`（最大 8 GiB），不可逆。机身跑着时禁用（盘在用）。
        self.btn_fresh = QPushButton("重建状态盘…")
        self.btn_fresh.setToolTip("把这一台的状态盘（/userdata + /userdisk）抹掉重建 —— "
                                  "不可逆；会自动先复制一份 .bak 留档。需先关机。")
        self.btn_fresh.clicked.connect(self._fresh_clicked)
        row2.addWidget(self.btn_fresh)
        row2.addStretch(1)
        head_lay.addLayout(row2)
        lay.addWidget(head)
        self.tabs = QTabWidget()
        self.serial = self._log_pane()
        self.serial.setMaximumBlockCount(4000)
        self.tabs.addTab(self.serial, "串口")
        self.applog = self._build_applog()
        self.tabs.addTab(self.applog, "guest 应用")
        self.tabs.addTab(self._build_shell(), "命令")
        self.tabs.addTab(self._build_files(), "文件")
        self.tabs.addTab(self._build_machine(), "配置")
        self.tabs.addTab(self._build_bt(), "蓝牙")
        self.tabs.addTab(self._build_snd(), "声音")
        self.tabs.setTabToolTip(0, "宿主 vm\\serial.log —— 不需要 VM 在跑（决策 #7）")
        self.tabs.setTabToolTip(2, "在笔里直接跑命令（走 SSH，不占 QMP）")
        self.tabs.setTabToolTip(3, "取出来 / 放进去 / 装 miniapp（sftp 通道，文件名带空格也安全）")
        self.tabs.setTabToolTip(4, "这台虚拟机给多少内存 / 几个 vCPU / 多大一块盘"
                                   "（改的都是**下次起机**的事，跑着改不了）")
        self.tabs.setTabToolTip(5, "客机内的虚拟蓝牙（不是电脑蓝牙；连不到真实设备，需 USB 直通）")
        self.tabs.setTabToolTip(6, "把客机的声音送到宿主；快速启动的快照不含音频")
        # `39` §2 #2：三页全空时别把人扔在一个空画布上 —— 默认停在唯一的**可交互**那页。
        self.tabs.setCurrentIndex(2)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        lay.addWidget(self.tabs)

    def _on_tab_changed(self, index: int) -> None:
        """进「配置」页刷一次读数：这一页的数**全是别人的动作改的**（起机、关机、扩容），
        停在原地的话"改了没生效"就只能靠猜。"""
        if self.tabs.widget(index) is getattr(self, "machine_page", None):
            self._refresh_machine()

    def _build_shell(self) -> QWidget:
        """窗口内的 guest shell —— Hyper-V 没有，这是我们**独有**的一条（`38` §3 P0）。

        复用既有的 `dpctl sh -c`（`harness.run_dpctl`，**不经 shell**，与决策 #23 同源），
        输出与警告共用一个格：这里本来就是这个控制台的时间线。
        """
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.cmdlog = self._log_pane(SHELL_PLACEHOLDER)
        self.cmdlog.setMaximumBlockCount(4000)
        self.shell_in = QLineEdit()
        # `39` §5 #9：placeholder 会被裁切，长的说明进 tooltip —— 被截断的省略号
        # 会让人以为是自己打错了。
        self.shell_in.setPlaceholderText(SHELL_INPUT_PLACEHOLDER)
        self.shell_in.setToolTip("例：uptime / ls /userdata / ps  （回车执行）")
        self.shell_in.setClearButtonEnabled(True)
        self.shell_in.returnPressed.connect(self._shell_submit)

        # 警告的出口（Q49）：**只在 `disk-full` 在响时出现**，平时整行不占位。
        # 它不删东西、也不自己跑 —— 只把命令填进下面那格，回车由人按（只读、可逆）。
        self.btn_corefile = QPushButton("看这个目录")
        self.btn_corefile.setToolTip(
            "把 `ls -l /userdisk/corefile | head -40` 填到下面那格，**不自动执行**。\n"
            "这一档数的是目录条目，不是 core 转储（a6pro 实测 750 项里 core 0 个）—— 看一眼就知道。")
        self.btn_corefile.clicked.connect(self._look_corefile)
        self.btn_corefile.setVisible(False)
        # Q70 那条警告的出口，同一套纪律：**只填格、不自己跑**（回车由人按）。
        self.btn_apps = QPushButton("看这几个应用")
        self.btn_apps.setToolTip(
            "把 `ls -ld <落点…>` 填到下面那格，**不自动执行**。\n"
            "回 `No such file or directory` 的那些，就是注册表里列着、盘上却没文件的应用 ——\n"
            "它们在列表里没图标、点开黑屏。补回来走「文件」页装对应的 .amr（预设包在\n"
            "`/oem/secondary/etc/miniapp/resources/presetpkgs/`）。")
        self.btn_apps.clicked.connect(self._look_apps)
        self.btn_apps.setVisible(False)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(8, 4, 8, 2)
        row.addWidget(self.btn_corefile)
        row.addWidget(self.btn_apps)
        row.addStretch(1)

        v.addWidget(self.cmdlog, 1)
        v.addLayout(row)
        v.addWidget(self.shell_in, 0)
        return box

    def _build_files(self) -> QWidget:
        """抽屉第 4 页「文件」（`42-file-transfer-features.md` §2，人点名的需求）。

        **只回答两个问题**：把这个放进去 / 把那个拿出来。不做文件管理器 —— 不列树、不改名、
        不删除、不多选拖拽批量（那些是 `dpctl sh` 的活，做了就要维护第二套"什么能跑"的语义）。

        三件护栏：**落点旁显示还剩多少**（§2.2：`/userdata` 就是状态盘）、
        **覆盖前自动留档**（§2.4）、**结果看得见**（成功失败都写进这一页的日志）。
        通道是 sftp（§2.8）：文件名带空格/中文都照原样，二进制安全。
        """
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        v.addWidget(self._file_label("取回来（笔 → 宿主）"))
        self.f_get_remote = QLineEdit("/userdata/")
        self.f_get_remote.setPlaceholderText("笔里的路径")
        v.addWidget(self.f_get_remote)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.f_get_local = QLineEdit()
        self.f_get_local.setPlaceholderText("存到宿主的哪个文件")
        row.addWidget(self.f_get_local, 1)
        b = QPushButton("选择…")
        b.setToolTip("挑一个宿主上要存到哪（文件对话框）")
        b.clicked.connect(self._pick_save_to)
        row.addWidget(b, 0)
        v.addLayout(row)
        self.btn_get = QPushButton("取回来")
        self.btn_get.clicked.connect(self._get_clicked)
        v.addWidget(self.btn_get)

        v.addWidget(self._file_label("放进去（宿主 → 笔）"))
        self.f_put_remote = QLineEdit("/userdata/")
        self.f_put_remote.setPlaceholderText("笔里的落点")
        v.addWidget(self.f_put_remote)
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.f_put_local = QLineEdit()
        self.f_put_local.setPlaceholderText("要放进去的宿主文件")
        row2.addWidget(self.f_put_local, 1)
        b2 = QPushButton("选择…")
        b2.setToolTip("挑一个宿主上要放进去的文件；选完会自动填好落点")
        b2.clicked.connect(self._pick_open_from)
        row2.addWidget(b2, 0)
        v.addLayout(row2)
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        self.btn_put = QPushButton("放进去")
        self.btn_put.clicked.connect(self._put_clicked)
        row3.addWidget(self.btn_put)
        b3 = QPushButton("看剩余")
        b3.setToolTip("问一次落点那个盘还剩多少（走 SSH，不占 QMP）")
        b3.clicked.connect(self._refresh_space)
        row3.addWidget(b3)
        row3.addStretch(1)
        v.addLayout(row3)
        self.lbl_space = QLabel(space_note(FILE_DEFAULT_DIR, None))
        self.lbl_space.setObjectName("spaceNote")
        self.lbl_space.setWordWrap(True)
        v.addWidget(self.lbl_space)

        # 装 miniapp（人点名）：**三行文案不如一个顺序** —— 选文件 → 落地 → 装 → 回执。
        # 安装命令**只在落地 rc=0 之后**才发（否则 `miniapp_cli` 拿到的是一个不存在或半截的
        # 文件，回执里那个错误会把人的排查方向带偏到"安装失败"上）。
        # 标题只写"装什么、收哪种文件"：整条链由下面三行自己演（那一格 → 落点 → 按钮），
        # 写成 `A → B → C` 的标题实测 588 px，抽屉窄一点（416 px）就折成两行压住内容。
        v.addWidget(self._file_label("装 miniapp（.amr）"))
        row_amr = QHBoxLayout()
        row_amr.setSpacing(4)
        self.f_amr_local = QLineEdit()
        self.f_amr_local.setPlaceholderText("宿主上的 .amr 文件")
        self.f_amr_local.setToolTip("也可以用下面那颗「选 .amr…」挑；这一格允许自己粘路径")
        self.f_amr_local.textChanged.connect(self._sync_amr_target)
        row_amr.addWidget(self.f_amr_local, 1)
        b_amr = QPushButton("选 .amr…")
        b_amr.setToolTip("只列 `.amr`；选完这里会写明落到笔里的哪个路径")
        b_amr.clicked.connect(self._pick_amr)
        row_amr.addWidget(b_amr, 0)
        v.addLayout(row_amr)
        self.lbl_amr_target = QLabel("落点 —（还没选文件）")
        self.lbl_amr_target.setObjectName("spaceNote")
        self.lbl_amr_target.setWordWrap(True)
        v.addWidget(self.lbl_amr_target)
        row5 = QHBoxLayout()
        row5.setSpacing(6)
        self.btn_install = QPushButton("推进去并安装")
        self.btn_install.setToolTip(
            "两步连着做：先把文件放进 `/userdata/<原名>`（**覆盖前自动留档**），"
            "成功了再在笔里跑 `miniapp_cli install /userdata/<原名>`。\n"
            "回执（命令、rc、输出尾巴）写在下面那格里。"
            "⚠ `miniapp_cli` 在不在 PATH 本仓没实测过 —— 不在就照原样把 guest 的回执给你，"
            "控制台不替它猜路径。")
        self.btn_install.clicked.connect(self._install_clicked)
        row5.addWidget(self.btn_install)
        row5.addStretch(1)
        v.addLayout(row5)

        # 取证包（`42` §1 ③）：纯读，把"出问题时要交给对方"的四件收成一个目录。
        # 它放在这一页是因为产出的是一个目录/文件 —— 与本页"取/放"同一个心智模型。
        row4 = QHBoxLayout()
        row4.setSpacing(6)
        self.btn_bundle = QPushButton("导出取证包…")
        self.btn_bundle.setToolTip("把串口尾、探针读数、最近一张存图、包声明收成一个目录"
                                   "（只读；不含设备身份/license/状态盘）")
        self.btn_bundle.clicked.connect(self._bundle_clicked)
        row4.addWidget(self.btn_bundle)
        row4.addStretch(1)
        v.addLayout(row4)

        self.filelog = QPlainTextEdit()
        self.filelog.setReadOnly(True)
        self.filelog.setMaximumBlockCount(2000)
        self.filelog.setPlaceholderText(
            "（取 / 放 / 装 的结果都写在这里：命令、路径、rc、字节数、留档说明）")
        v.addWidget(self.filelog, 1)
        return box

    @staticmethod
    def _file_label(text: str) -> QLabel:
        w = QLabel(text)
        w.setObjectName("fileSection")
        return w

    def _pick_save_to(self) -> None:
        remote = self.f_get_remote.text().strip()
        start = os.path.join(harness.VM, os.path.basename(remote) or "从笔里取出来的文件")
        path, _ = QFileDialog.getSaveFileName(self, "取回来的文件存到哪", start)
        if path:
            self.f_get_local.setText(path)

    def _pick_open_from(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "要放进笔里的文件")
        if not path:
            return
        self.f_put_local.setText(path)
        cur = self.f_put_remote.text().strip()
        if cur in ("", "/userdata/", "/userdata"):
            # 顺手的默认落点 = 文档里那条 canonical 例子（`dpctl put a.txt /userdata/a.txt`）
            self.f_put_remote.setText(f"{FILE_DEFAULT_DIR}/{os.path.basename(path)}")

    def _pick_amr(self) -> None:
        """只列 `.amr` —— 这一行不是通用文件行，误选一个 `.so` 装下去是要改设备的。"""
        path, _ = QFileDialog.getOpenFileName(self, "要装的 miniapp（.amr）", "", AMR_FILTER)
        if path:
            self.f_amr_local.setText(path)

    def _sync_amr_target(self, _ignored=None) -> None:
        """落点那一行**跟着那一格走**：粘进去的路径和选出来的要走同一句显示。"""
        local = self.f_amr_local.text().strip()
        self.lbl_amr_target.setText(f"落点 {amr_target(local)}" if local
                                    else "落点 —（还没选文件）")

    def _file_busy(self, busy: bool) -> None:
        self._file_busy_flag = busy
        for w in (self.btn_get, self.btn_put, self.btn_install):
            w.setEnabled(not busy)

    def _file_log(self, text: str) -> None:
        self.filelog.appendPlainText("· " + text)

    def _get_clicked(self) -> None:
        remote, local = self.f_get_remote.text().strip(), self.f_get_local.text().strip()
        if not remote or not local:
            self._file_log("取回来：笔里的路径和存到哪都要填")
            return
        self._file_busy(True)
        threading.Thread(target=self._get_worker, args=(remote, local), daemon=True).start()

    def _get_worker(self, remote: str, local: str) -> None:
        rc = harness.get(remote, local)
        note = f"{os.path.getsize(local)} B" if (rc == 0 and os.path.exists(local)) else ""
        self.bridge.file_op.emit(f"取回来 {remote} → {local}", rc, note)

    def _put_clicked(self) -> None:
        local, remote = self.f_put_local.text().strip(), self.f_put_remote.text().strip()
        if not local or not remote:
            self._file_log("放进去：本地文件和笔里的落点都要填")
            return
        if not os.path.exists(local):
            self._file_log(f"本地没有这个文件：{local}")
            return
        self._file_busy(True)
        threading.Thread(target=self._put_worker, args=(local, remote), daemon=True).start()

    def _land(self, local: str, remote: str) -> tuple[int, str]:
        """把宿主文件放进笔里：**覆盖前先留档**（§2.4），再 `put`。返回 `(rc, 说明)`。

        「放进去」和「装 miniapp」共用这一条 —— 被盖掉的可能是设备身份
        （`system_info.bin`）或桩库（`.so`），现场丢了就回不去。
        """
        note = harness.backup_remote(remote)
        return harness.put(local, remote), note

    def _put_worker(self, local: str, remote: str) -> None:
        rc, note = self._land(local, remote)
        self.bridge.file_op.emit(f"放进去 {local} → {remote}", rc, note)

    def _install_clicked(self) -> None:
        local = self.f_amr_local.text().strip()
        if not local:
            self._file_log("装 miniapp：先选一个宿主上的 .amr（也可以把路径粘进那一格）")
            return
        if not local.lower().endswith(AMR_SUFFIX):
            # 拒绝 + 出口（§7 禁第 2 条：不许只说不行）。这一格旁边就是通用那行。
            self._file_log(f"这一行只装 `{AMR_SUFFIX}`，现在这个是 {os.path.basename(local)}。"
                           "别的文件要用上面那行「放进去」。")
            return
        if not os.path.exists(local):
            self._file_log(f"本地没有这个文件：{local}")
            return
        self._file_busy(True)
        threading.Thread(target=self._install_worker,
                         args=(local, amr_target(local)), daemon=True).start()

    def _install_worker(self, local: str, remote: str) -> None:
        """落地 → **成功了才**发安装命令 → 回执印在本页那格（命令、rc、guest 的输出）。

        为什么安装要等落地 rc=0：文件没进去（或只进去半截）时 `miniapp_cli` 也会回一个
        错，那个错长得像"包有问题"，会把人从"传输没成"带到"安装失败"上去。
        """
        rc, note = self._land(local, remote)
        if rc != 0:
            self.bridge.file_op.emit(
                f"放进去 {local} → {remote}（安装这一步**没发**）", rc,
                note + "\n笔里没有这个文件时装它没有意义 —— 先解决传输，"
                       "再看下面那行按钮重试。")
            return
        irc, cmd, out = harness.install_miniapp(remote)
        size = os.path.getsize(local)
        tail = out.strip()[-1200:] or "（guest 一个字都没回）"
        self.bridge.file_op.emit(cmd, irc,
                                 f"文件已落地 {size} B ｜ {note or '原本没有同名文件'}\n{tail}")

    def _bundle_clicked(self) -> None:
        """导出取证包：**纯读**，只在宿主上写一个新目录（不碰 QMP、不改任何状态）。"""
        pack = self.pack or ""
        values = self._probe.values if self._probe else None
        ok = self._probe.ok if self._probe else ""
        self.btn_bundle.setEnabled(False)

        def work():
            try:
                dest, written, missing = harness.make_bundle(
                    pack, probe_values=values, probe_ok=ok)
                self.bridge.file_op.emit(
                    f"导出取证包 → {os.path.relpath(dest, harness.ROOT)}", 0,
                    f"{len(written)} 件 / 缺 {len(missing)} 件"
                    + ("：" + "、".join(missing) if missing else ""))
                os.startfile(dest)                      # 顺手打开，别让人去找
            except OSError as e:
                self.bridge.file_op.emit("导出取证包失败", 1, str(e))
            finally:
                self.btn_bundle.setEnabled(True)

        threading.Thread(target=work, daemon=True).start()

    def _on_file_op(self, what: str, rc: int, note: str) -> None:
        self._file_busy(False)
        line = f"$ {what}\n    rc={rc}"
        if note:
            # 说明可以是**多行** —— 安装回执要把 guest 的输出带回来。每行都缩进对齐，
            # 否则整段顶到最左，跟下一条 `$` 读不出边界（单行的旧调用者形状不变）。
            line += "  " + "\n    ".join(note.strip().splitlines())
        self.filelog.appendPlainText(line)
        self._refresh_space()
        self._kick_probe()              # 盘用量变了 → 立刻重采一轮（别等 5 s）

    def _refresh_space(self) -> None:
        remote = self.f_put_remote.text().strip() or FILE_DEFAULT_DIR
        path = posixpath.dirname(remote) or "/"

        def work():
            self.bridge.space.emit(harness.free_space(path) or -1, path)

        threading.Thread(target=work, daemon=True).start()

    def _on_space(self, nbytes: int, path: str) -> None:
        self.lbl_space.setText(space_note(path, None if nbytes < 0 else nbytes))

    # ------------------------------------------------------------ 「配置」页 --
    def _build_machine(self) -> QWidget:
        """抽屉第 5 页「配置」：这台虚拟机给多少内存 / 几个 vCPU / 多大一块盘。

        整页的措辞只服务一个事实：**`-m` 与 `-smp` 是 QEMU 命令行参数，跑着的机器改不了**。
        所以每个旋钮都摆**两个读数**（下次起机会用什么 ／ 客机现在看到什么），谁也不许被写成
        "已生效"。状态盘还有**第三个**口径：宿主文件大小 ≠ 客机文件系统大小 ——
        `qemu-img resize` 只动前者，`resize2fs` 才动后者，中间那一格必须说实话。

        值不住在这儿：默认在 runtime，覆盖在 `vm/machine.toml`，两边都问 `dpctl`
        （控制台不复算一遍，否则就是第二个真相 —— Q49/Q62 那一族）。
        """
        box = QWidget()
        self.machine_page = box
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)
        self._m_next: dict = {}
        self._m_live: dict = {}
        self._machine_busy = False

        v.addWidget(self._file_label("机器参数（改完要下次起机才生效）"))
        self.lbl_host = QLabel("宿主 —")
        self.lbl_host.setObjectName("spaceNote")
        self.lbl_host.setWordWrap(True)
        self.lbl_host.setToolTip("给到宿主内存四分之三以上，起得来也会卡（TCG 全在软件里跑）")
        v.addWidget(self.lbl_host)

        self.lbl_mem = QLabel("—")
        self.lbl_mem.setObjectName("spaceNote")
        self.lbl_mem.setWordWrap(True)
        self.lbl_mem.setToolTip("客机看到的那个数**永远小于** `-m`（内核与保留页要吃掉一截），"
                                "所以这一格叫「客机看到」，不叫「当前内存」")
        self.f_mem = QLineEdit()
        self.f_mem.setFixedWidth(64)
        self.f_mem.setToolTip("写成 `512M` / `2G` 这样；清空＝跟着 runtime 默认")
        self.lbl_smp = QLabel("—")
        self.lbl_smp.setObjectName("spaceNote")
        self.lbl_smp.setWordWrap(True)
        self.f_smp = QLineEdit()
        self.f_smp.setFixedWidth(64)
        self.f_smp.setToolTip("1~64；TCG 下一个 vCPU 是一条线程，超过宿主核数只会更慢")
        for lab, val, field, name in (("内存", self.lbl_mem, self.f_mem, "memory"),
                                      ("vCPU", self.lbl_smp, self.f_smp, "smp")):
            row = QHBoxLayout()
            row.setSpacing(4)
            row.addWidget(QLabel(lab))
            row.addWidget(val, 1)
            row.addWidget(field)
            btn = QPushButton("改")
            btn.setToolTip(f"只改这一格（{lab}），另一格不动")
            btn.clicked.connect(lambda _=False, k=name, f=field, n=lab:
                                self._machine_write(k, f, n))
            row.addWidget(btn)
            v.addLayout(row)
        self.btn_apply = QPushButton("重启现在生效")
        self.btn_apply.setToolTip("就是工具条那两步（关机 → 启动），回执写在「命令」页；"
                                  "起机要 3~6 分钟。没改参数时按它没有意义。")
        self.btn_apply.clicked.connect(self._restart_clicked)
        v.addWidget(self.btn_apply)

        v.addWidget(self._file_label("状态盘（/userdata 在这块盘上）"))
        self.lbl_disk = QLabel("—")
        self.lbl_disk.setObjectName("spaceNote")
        self.lbl_disk.setWordWrap(True)
        self.lbl_disk.setToolTip("`/userdata`、`/userdisk` 都在这块盘上（实测：同一份 ext4，`/state` 挂根、另两个挂子目录）。\n"
                                 "盘文件＝宿主上那份 .img；客机 fs＝`df` 看到的。\n"
                                 "两个数本来就差着一截（ext4 元数据），差得**多**才是"
                                 "扩了盘没跑 `resize2fs`。")
        v.addWidget(self.lbl_disk)
        self.lbl_disk_note = QLabel(
            "扩容只改虚拟机状态盘；笔内「空间清理」显示固件认的硬件容量，不随 VM 盘变化。")
        self.lbl_disk_note.setWordWrap(True)
        v.addWidget(self.lbl_disk_note)
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.f_disk = QLineEdit()
        self.f_disk.setFixedWidth(64)
        self.f_disk.setPlaceholderText("MiB")
        self.f_disk.setToolTip("要扩到的总大小（**只许变大**）；比现在小会被拒绝")
        row2.addWidget(self.f_disk)
        self.btn_grow = QPushButton("扩容…")
        self.btn_grow.setToolTip("`qemu-img resize`。**VM 停着才做**（跑着换 raw 文件的底＝撕裂）；"
                                 "只增不减 —— 要变小只有「重建状态盘」那条路（会丢数据）")
        self.btn_grow.clicked.connect(self._grow_clicked)
        row2.addWidget(self.btn_grow)
        row2.addStretch(1)
        v.addLayout(row2)
        self.btn_fs = QPushButton("让客机用上多出来的空间")
        self.btn_fs.setToolTip("在客机里跑 `resize2fs /dev/vdb`（在线扩，**要 VM 在跑**）。\n"
                               "刚建好的盘、没扩过 → 按它也只是原样回来，不会弄坏数据。")
        self.btn_fs.clicked.connect(self._growfs_clicked)
        v.addWidget(self.btn_fs)

        self.machlog = self._log_pane(
            "（这一页的动作写在这里：改了什么、写进哪份文件、扩容的 rc、resize2fs 的回执）")
        v.addWidget(self.machlog, 1)
        return box

    def _machine_note(self, what: str, rc: int, out) -> None:
        line = f"$ {what}\n    rc={rc}"
        if out:
            line += "  " + "\n    ".join(str(out).strip().splitlines())
        self.machlog.appendPlainText(line)

    def _on_machine_note(self, what: str, rc: int, out: str) -> None:
        self.btn_fs.setEnabled(True)
        self._machine_note(what, rc, out)
        self._refresh_machine()

    # ------------------------------------------------------------------ #
    # 蓝牙页（只有带 BT 的机器才有；判据是客机里有没有 hci0）
    # ------------------------------------------------------------------ #
    def _build_bt(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.lbl_bt = QLabel("还没查过 —— 按「刷新」")
        self.lbl_bt.setWordWrap(True)
        lay.addWidget(self.lbl_bt)
        self.lbl_bt_host = QLabel(
            "客机内的虚拟蓝牙，不是电脑蓝牙：当前只模拟发现，连不到真实耳机/音箱；"
            "真实设备需要把 USB 蓝牙适配器直通给虚拟机。")
        self.lbl_bt_host.setWordWrap(True)
        lay.addWidget(self.lbl_bt_host)
        self.lst_bt = QListWidget()
        self.lst_bt.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lst_bt.setToolTip("客机配对库里的设备；虚拟控制器没有实现连接（真实设备需 USB 直通）")
        lay.addWidget(self.lst_bt, 1)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._bt_buttons = []
        for text, tip, slot in (
                ("刷新", "读 hci0 状态与配对库（走 guest shell，不占 QMP）",
                 lambda: self._bt_run("refresh", BT_REPORT_CMD, 40)),
                ("开", "btmgmt power on + le on（客机 hci0，不是电脑蓝牙）",
                 lambda: self._bt_run("up", BT_UP_CMD, 25)),
                ("关", "btmgmt power off",
                 lambda: self._bt_run("down", "btmgmt power off 2>&1", 25)),
                ("扫描 8 秒", "btmgmt --timeout 8 find（不依赖 bluetoothd；只发现模拟设备）",
                 lambda: self._bt_run("scan", BT_SCAN_CMD, 30)),
                ("连接", "虚拟控制器没有实现连接；真实设备需 USB 蓝牙直通", self._bt_connect),
                ("断开", "虚拟控制器没有实现连接；真实设备需 USB 蓝牙直通", self._bt_disconnect)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
            self._bt_buttons.append(b)
        self._bt_buttons[4].setEnabled(False)
        self._bt_buttons[5].setEnabled(False)
        row.addStretch(1)
        lay.addLayout(row)
        self.bt_out = self._log_pane()
        self.bt_out.setMaximumBlockCount(300)
        self.bt_out.setMaximumHeight(120)
        lay.addWidget(self.bt_out)
        return page

    def _bt_run(self, what: str, cmd: str, timeout: int = 30) -> None:
        if self._bt_busy:
            self._bt_show("上一条命令尚未返回")
            return
        if not harness.vm_running():
            self._bt_show("虚拟机未运行；蓝牙服务在客机内，启动后可查看")
            return
        self._bt_busy = True
        self.lbl_bt.setText("读取中…")
        threading.Thread(target=self._bt_worker, args=(what, cmd, timeout), daemon=True).start()

    def _bt_worker(self, what: str, cmd: str, timeout: int) -> None:
        rc, out = harness.run_dpctl(["sh", "-c", cmd], timeout=timeout)
        self.bridge.bt.emit(what, rc, out)

    def _on_bt(self, what: str, rc: int, out: str) -> None:
        self._bt_busy = False
        self.bt_out.appendPlainText(("$ %s  (rc=%s)\n%s"
                                     % (what, rc, (out or "").strip()[-1500:])))
        if what == "refresh":
            self._bt_render(parse_bt_report(out))
            return
        if rc != 0:
            self.lbl_bt.setText(f"{what} 没成（rc={rc}）—— 看下面那格")
            return
        self._bt_run("refresh", BT_REPORT_CMD, 40)   # 改完自动再读一遍，省一次点击

    def _bt_render(self, rep: dict) -> None:
        self.lst_bt.clear()
        for addr, name in rep["devices"]:
            it = QListWidgetItem(f"{name}  ({addr})")
            it.setData(Qt.UserRole, addr)
            self.lst_bt.addItem(it)
        if rep["absent"]:
            self.lbl_bt.setText("这台没有蓝牙：客机里没有 hci0 —— 这一页只对带 BT 的机器有用")
            for b in self._bt_buttons[1:]:
                b.setEnabled(False)
            return
        for b in self._bt_buttons[1:4]:
            b.setEnabled(True)
        st = {"up": "已开", "down": "没开"}.get(rep["state"], "状态未知")
        name = f"（{rep['name']}）" if rep.get("name") else ""
        line = f"客机虚拟控制器 hci0{name} · {st}" + (f" · {rep['addr']}" if rep["addr"] else "")
        line += f" · 配对库 {len(rep['devices'])} 台"
        if rep["powered_off"]:
            line += "（控制器未开启；请先按「开」）"
        self.lbl_bt.setText(line)

    def _bt_selected(self) -> str:
        it = self.lst_bt.currentItem()
        if it is None:
            self._bt_show("先在列表里选一台")
            return ""
        return it.data(Qt.UserRole) or ""

    def _bt_connect(self) -> None:
        self._bt_show("虚拟控制器没有实现连接；真实耳机/音箱需要 USB 蓝牙适配器直通")

    def _bt_disconnect(self) -> None:
        self._bt_show("虚拟控制器没有实现连接；真实耳机/音箱需要 USB 蓝牙适配器直通")

    def _bt_show(self, text: str) -> None:
        self.lbl_bt.setText(text)

    # ------------------------------------------------------------------ #
    # 声音页（声卡是起机时 `--audio` 挂上的 QEMU USB Audio）
    # ------------------------------------------------------------------ #
    def _build_snd(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.lbl_snd = QLabel("还没查过 —— 按「刷新」")
        self.lbl_snd.setWordWrap(True)
        lay.addWidget(self.lbl_snd)
        self.lbl_snd_host = QLabel(
            "宿主输出由 Windows 决定：听不到先查系统输出设备是否静音或音量为 0。")
        self.lbl_snd_host.setWordWrap(True)
        lay.addWidget(self.lbl_snd_host)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._snd_buttons = []
        for text, tip, slot in (
                ("刷新", "读声卡与音量（amixer；不占 QMP）",
                 lambda: self._snd_run("refresh", SND_REPORT_CMD, 30)),
                ("测一下", "把 1 秒 440 Hz 放进客机再播 —— 宿主喇叭应该听到一声嘀；"
                           "播放中选其他操作会中断它", self._snd_test),
                ("静音", "amixer sset ... mute",
                 lambda: self._snd_run("mute", "amixer -c 0 sset '" + SND_CTL + "' mute 2>&1")),
                ("取消静音", "amixer sset ... unmute",
                 lambda: self._snd_run("unmute", "amixer -c 0 sset '" + SND_CTL + "' unmute 2>&1"))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
            self._snd_buttons.append(b)
        row.addStretch(1)
        lay.addLayout(row)
        # 音量滑杆：**松手才发**一次 amixer（拖动过程不刷命令）。
        vrow = QHBoxLayout()
        vrow.setSpacing(6)
        vrow.addWidget(QLabel("客机音量"))
        self.sld_vol = QSlider(Qt.Horizontal)
        self.sld_vol.setRange(0, 100)
        self.sld_vol.setEnabled(False)
        self.sld_vol.setToolTip("松手发一次 amixer sset（QEMU USB Audio 内部是 0~256 档）")
        self.sld_vol.sliderReleased.connect(lambda: self._snd_volume(self.sld_vol.value()))
        vrow.addWidget(self.sld_vol, 1)
        lay.addLayout(vrow)
        self.snd_out = self._log_pane()
        self.snd_out.setMaximumBlockCount(300)
        self.snd_out.setMaximumHeight(120)
        lay.addWidget(self.snd_out)
        lay.addStretch(1)
        return page

    def _snd_cancel(self) -> None:
        job, self._snd_job = self._snd_job, None
        if job is not None:
            job.cancel()
            self.snd_out.appendPlainText("上一条已中断")

    def _snd_run(self, what: str, cmd: str, timeout: int = 30) -> None:
        self._snd_cancel()
        if not harness.vm_running():
            self.lbl_snd.setText("虚拟机未运行；音频服务在客机内，启动后可查看")
            return
        self._snd_gen += 1
        gen = self._snd_gen
        job = harness.GuestJob(["sh", "-c", cmd], timeout=timeout)
        self._snd_job = job
        self.lbl_snd.setText("读取中…")
        threading.Thread(target=self._snd_worker, args=(gen, what, job),
                         daemon=True).start()

    def _snd_worker(self, gen: int, what: str, job) -> None:
        rc, out = job.run()
        self.bridge.snd.emit(gen, what, rc, out)

    def _on_snd(self, gen: int, what: str, rc: int, out: str) -> None:
        if gen != self._snd_gen:            # 被后来的操作顶掉了，回执作废
            return
        self._snd_job = None
        self.snd_out.appendPlainText(("$ %s  (rc=%s)\n%s"
                                      % (what, rc, (out or "").strip()[-1200:])))
        if what == "refresh":
            self._snd_render(parse_snd_report(out))
            return
        if rc != 0:
            self.lbl_snd.setText(f"{what} 没成（rc={rc}）—— 看下面那格")
            return
        self._snd_run("refresh", SND_REPORT_CMD, 30)   # 改完自动再读一遍

    def _snd_render(self, rep: dict) -> None:
        if rep["absent"]:
            self.lbl_snd.setText(
                "客机没有声卡：快速启动的快照不含音频，或起机时用了 --no-audio。"
                "要声音请用普通「启动」冷启动。")
            for b in self._snd_buttons[1:]:
                b.setEnabled(False)
            self.sld_vol.setEnabled(False)
            return
        for b in self._snd_buttons[1:]:
            b.setEnabled(True)
        self.sld_vol.setEnabled(True)
        bits = [f"声卡 card0（{rep['card'] or '?'}）"]
        if rep["desc"]:
            bits.append(rep["desc"])
        if rep["vol"] is not None:
            bits.append(f"音量 {rep['vol']}%" + ("（静音）" if rep["muted"] else ""))
            self.sld_vol.blockSignals(True)     # 回填别触发"用户松手"
            self.sld_vol.setValue(rep["vol"])
            self.sld_vol.blockSignals(False)
        self.lbl_snd.setText(" · ".join(bits))

    def _snd_test(self) -> None:
        self._snd_cancel()
        if not harness.vm_running():
            self.lbl_snd.setText("虚拟机未运行；音频服务在客机内，启动后可查看")
            return
        self._snd_gen += 1
        gen = self._snd_gen
        self.lbl_snd.setText("放测试音…")

        def work() -> None:
            tone = harness.ensure_tone()
            job = harness.GuestJob(["put", tone, SND_TONE], timeout=30)
            self._snd_job = job
            if gen != self._snd_gen:        # 上传还没开始就被新操作顶掉
                job.cancel()
                return
            rc1, out1 = job.run()
            if gen != self._snd_gen:
                return
            job = harness.GuestJob(
                ["sh", "-c", "aplay -D plughw:0,0 " + SND_TONE + " 2>&1"], timeout=30)
            self._snd_job = job
            if gen != self._snd_gen:
                job.cancel()
                return
            rc2, out2 = job.run()
            if gen != self._snd_gen:
                return
            self.bridge.snd.emit(gen, "测试音", rc2 if rc1 == 0 else rc1,
                                 (out1 or "") + (out2 or ""))

        threading.Thread(target=work, daemon=True).start()

    def _snd_show(self, text: str) -> None:
        self.lbl_snd.setText(text)

    def _snd_volume(self, pct: int) -> None:
        self._snd_run("vol", "amixer -c 0 sset '" + SND_CTL + "' %d%% 2>&1" % pct)

    def _refresh_machine(self, ask_guest: bool = True) -> None:
        """宿主侧那几格在 UI 线程读（一份小 toml + 一次 `getsize`，毫秒级）；
        要问客机的（`MemTotal` / `df` / `resize2fs`）丢线程 —— 那条路上有 SSH，
        正是 Q60 那一族的雷（每帧问一次"机器还在跑吗"就能把界面钉死）。

        `ask_guest=False` 用在**换目标**的时候：人不一定在这页，而那一趟 SSH 在新目标
        还没起机时注定拿不到数（VM 是停的）。页面自己进去时会再刷（`_on_tab_changed`）。
        """
        mem, smp, src, err, name = harness.machine_next(self.pack)
        disk = harness.state_mib(self.pack)
        self._m_next = {"memory": mem, "smp": str(smp), "memory_src": src["memory"],
                        "smp_src": src["smp"], "disk_mib": disk, "pack": name}
        self.f_mem.setText(mem)
        self.f_smp.setText(str(smp))
        self.f_disk.setText(str(disk) if disk else "")
        host = harness.DPCTL.host_memory_mb()
        self.lbl_host.setText(
            (f"宿主 {harness.format_size(host * 1024 * 1024)}" if host else "宿主内存读不出")
            + f" / {os.cpu_count()} 核"
            + (f"　⚠ {err}" if err else ""))
        self._render_machine()
        if ask_guest and not self._machine_busy:
            self._machine_busy = True
            threading.Thread(target=self._machine_worker, daemon=True).start()

    def _machine_worker(self) -> None:
        live = harness.guest_machine() or {}
        fs = harness.guest_fs_mib() if live else None
        self.bridge.machine.emit({"live": live, "fs_mib": fs})

    def _on_machine(self, payload: dict) -> None:
        self._machine_busy = False
        live = payload.get("live") or {}
        self._m_live = {"mem_mib": live.get("mem_mib"), "cpu": live.get("cpu"),
                        "fs_mib": payload.get("fs_mib")}
        self._render_machine()

    def _render_machine(self) -> None:
        """两个读数并排写，**缺哪半句就少写哪半句**（没在跑不写 0，读不到不写"无"）。"""
        nxt, live = self._m_next, self._m_live
        self.lbl_mem.setText(f"下次 {nxt.get('memory', '—')}（{nxt.get('memory_src', '?')}）"
                             + (f"｜客机看到 {live['mem_mib'] / 1024:.2f} GiB"
                                if live.get("mem_mib") else ""))
        self.lbl_smp.setText(f"下次 {nxt.get('smp', '—')}（{nxt.get('smp_src', '?')}）"
                             + (f"｜客机看到 {live['cpu']} 个" if live.get("cpu") else ""))
        disk = nxt.get("disk_mib") or 0
        fs = live.get("fs_mib") or 0
        txt = f"盘文件 {disk or '—'} MiB"
        if fs:
            txt += f"｜客机 fs {fs} MiB"
            if disk and disk - fs > disk // 10:
                txt += "（差得多 —— 扩了还没用上：按下面那颗）"
        elif not live:
            txt += "｜客机未运行，文件系统不可读"
        self.lbl_disk.setText(txt)

    def _machine_write(self, key: str, field: QLineEdit, name: str) -> None:
        raw = field.text().strip()
        if key == "memory":
            eff, err = harness.set_machine(self.pack, memory=raw)
        else:
            eff, err = harness.set_machine(self.pack, smp=raw)
        if err:
            self._machine_note(f"{name} 没改", 1, err)
            return
        self._machine_note(
            f"{name} → {eff.get(key) or '（清空＝跟着 runtime 默认）'}", 0,
            "写进了 vm/machine.toml。**下次起机才生效** —— 现在跑着那台还是旧值；"
            "要立刻生效按「重启现在生效」。")
        self._refresh_machine()

    def _grow_clicked(self) -> None:
        """扩状态盘：`qemu-img resize`（宿主文件，毫秒级）—— 拒绝的理由由 `dpctl` 那侧给，
        这一页照原样端出来，不在界面里重讲一遍（两处说法迟早分家）。"""
        digits = "".join(c for c in self.f_disk.text() if c.isdigit())
        if not digits:
            self._machine_note("扩容没发", 1, "要一个 MiB 的整数（比现在大）")
            return
        img = os.path.basename(harness.state_image(self.pack)[0])
        rc, note = harness.grow_state(self.pack, int(digits))
        self._machine_note(f"qemu-img resize {img} → {digits} MiB", rc, note)
        self._refresh_machine()

    def _growfs_clicked(self) -> None:
        """`resize2fs` 走 SSH，最坏 60 s —— 必须离 UI 线程（Q60）。"""
        self.btn_fs.setEnabled(False)
        threading.Thread(target=self._growfs_worker, daemon=True).start()

    def _growfs_worker(self) -> None:
        rc, out = harness.guest_grow_fs()
        self.bridge.machine_note.emit(f"resize2fs {harness.GUEST_STATE_DEV}", rc, out)

    def _build_applog(self) -> QWidget:
        """「guest 应用」页：**首行是身份/热点**（#75），下面留给应用日志（第二片）。

        首行只摆现象 + 两把钥匙，不给结论 —— 拦路页的三种成因同形（#78）。
        """
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.lbl_identity = QLabel("（还没采到身份/热点，等第一轮探针）")
        self.lbl_identity.setObjectName("identityNote")
        self.lbl_identity.setWordWrap(True)
        v.addWidget(self.lbl_identity)
        self.lbl_adapt = QLabel("（还没做应用体检，等第一轮探针）")
        self.lbl_adapt.setObjectName("identityNote")
        self.lbl_adapt.setWordWrap(True)
        v.addWidget(self.lbl_adapt)
        self.applog_pane = self._log_pane(
            "（第二片：在 guest 里 `ls /userdata/applog/` 动态发现文件名）")
        v.addWidget(self.applog_pane, 1)
        return box

    @staticmethod
    def _log_pane(placeholder: str = "") -> QPlainTextEdit:
        w = QPlainTextEdit()
        w.setReadOnly(True)
        if placeholder:
            w.setPlainText(placeholder)
        return w

    def _toggle_drawer(self, on: bool) -> None:
        self._drawer_open = bool(on)
        self.drawer.setVisible(self._drawer_open)
        self.act_drawer.setText("抽屉 ▾" if self._drawer_open else "抽屉 ▸")

    # ------------------------------------------------------------------ #
    # 探针 / 判定（后台线程采，UI 线程判 + 画）
    # ------------------------------------------------------------------ #
    def _kick_probe(self) -> None:
        """起一轮探针。**上一轮没回来就不起新的**。

        5 s 到点时上一轮可能还在跑（宿主 SSH 握手 ~0.45 s + guest 脚本），重叠起来会在
        后台线程读、在 UI 线程写同一份"上次的桥/持有者 pid" —— 那是数据竞争。
        宁可少采一轮，也不要两轮同时改状态。
        """
        if self._probe_busy:
            return
        self._probe_busy = True
        threading.Thread(target=self._probe_worker, name="probe", daemon=True).start()

    def _probe_worker(self) -> None:
        try:
            pr = P.collect(harness.probe_transport)
            other = harness.qmp_owner(exclude_pid=os.getpid())
            sig = R.Signals(
                packs=harness.packs(),
                packs_known=harness.packs_known(),
                pack=self.pack or "",
                vm_running=harness.vm_running(),
                frames_ok=self.canvas.has_frame,
                other_owner=other or "",
                serial_state_error=harness.serial_state_unavailable(),
                ui_started_hint=harness.ui_started_hint(),
                probe=pr.values,
                probe_ok=pr.ok,
                prev_bridge_pids=self._prev_bridge_pids,
                prev_holder_pid=self._prev_holder_pid,
            )
            self.bridge.decision.emit((sig, pr))
        finally:
            self._probe_busy = False

    def _on_decision(self, payload) -> None:
        sig, pr = payload
        dec = R.decide(sig)
        # 本轮的记忆写回（F4：桥记**集合**；#25 的 pid 事件只作信息位）
        if dec.next_bridge_pids:
            self._prev_bridge_pids = dec.next_bridge_pids
        if dec.next_holder_pid is not None:
            self._prev_holder_pid = dec.next_holder_pid
        self._probe = pr
        self._decision = dec
        self._packs = sig.packs
        self._refresh_pack_menu()         # 里面有 sig 闸门：没变就只是比一次元组
        self._refresh_identity(pr)
        self._apply_decision(dec)
        self.lbl_status.setText(self._status_text(dec))

    def _refresh_identity(self, pr) -> None:
        """把这一轮的身份/热点与应用体检摆进抽屉首两行（#75 / Q70）。**没采到就说没采到**，不编。"""
        note = identity_note(pr.values if pr else {})
        self.lbl_identity.setText(note or "（这一轮没采到身份/热点 —— 通道没通）")
        adapt = app_adapt_note(pr.values if pr else {})
        self.lbl_adapt.setText(adapt or "（这一轮没采到探针值 —— 通道没通，体检不判）")

    def _apply_decision(self, dec: R.Decision) -> None:
        # F7：**接管视图的唯一资格是"没有帧可画"**。有帧就画帧，警告一律不擦画面。
        if dec.view == R.VIEW_BOOTING:
            if self.canvas.has_frame:
                self.stack.setCurrentWidget(self.canvas)   # 画面还在动就别拿 6~7 分钟的空态盖它
            else:
                self.empty.set_content(self._empty_content(dec))
                self.stack.setCurrentWidget(self.empty)
        elif dec.view == R.VIEW_NONE:
            self.stack.setCurrentWidget(self.canvas)       # 没帧时画布自己会说"等待虚拟机画面…"
        else:
            self.empty.set_content(self._empty_content(dec))
            self.stack.setCurrentWidget(self.empty)
        self.act_start.setEnabled(self._can_start(dec))
        running = dec.view not in (R.VIEW_VM_DOWN, R.VIEW_NO_PACK)
        st_fast = harness.fast_status(self.pack)
        self.act_fast.setEnabled(bool(st_fast.get("ready")) and self._can_start(dec))
        self.act_fast.setToolTip(
            "从快照恢复上次首页状态（约 6 秒；快照不包含音频）" if st_fast.get("ready")
            else "当前机型尚未创建快照；请先使用「保存快照」")
        _, state_size = harness.state_image(self.pack or "")
        self.act_fast_save.setEnabled(not running and not self._busy and state_size > 0
                                      and bool(self.pack))
        self.act_fast_save.setToolTip(
            "虚拟机正在运行；请先关机再保存快照" if running
            else "关机后保存当前状态为快速启动快照（约 1.5 分钟；不包含音频）")
        self.act_stop.setEnabled(running and not self._busy)
        self.act_restart.setEnabled(running and not self._busy)
        self.btn_cut.setEnabled(running and not self._busy)
        # 重建状态盘：必须**关机**才能做（盘正被 QEMU 用着），且盘得真的存在
        _, size = harness.state_image(self.pack or "")
        self.btn_fresh.setEnabled(not running and not self._busy and size > 0)
        self.act_shot.setEnabled(self.canvas.has_frame)
        # 应急键与存图同一条件：得有画面才点得着；只读视图不注入任何输入
        self.act_escape.setEnabled(self.canvas.has_frame and not self._readonly)
        self._note_flags(dec)

    def _can_start(self, dec: R.Decision) -> bool:
        if self._busy:
            return False
        if dec.view in (R.VIEW_NONE, R.VIEW_BOOTING, R.VIEW_QMP_BUSY):
            return False                    # VM 已经在跑（哪怕画不出来 / 被别的实例占着）
        if not self._packs:                 # #28：缺镜像时不许出现「启动」
            return False
        if self.pack:
            return any(p.get("name") == self.pack and p.get("ready") for p in self._packs)
        return any(p.get("ready") for p in self._packs)

    def _note_flags(self, dec: R.Decision) -> None:
        """警告进抽屉（F7）：视图只画帧，警告不擦画面，正文与出口放抽屉。

        去重键**故意不含数字**：旧写法拿整句话当键，可 `corefile 626` 每轮都在涨
        （a6pro 的 Capframe 日志实测 0.86 个/秒）→ 键永远在变 → **每 5 s 追加一行一模一样的
        警告**（实测截图 `vm/_qwen_ui_after.png` 抽屉里连着 10 行）。键只记"哪几档在响"，
        数字归状态栏那一格（它是活的，日志不是）。
        """
        texts, keys = [], []
        for f in dec.flags:
            if f.code == R.FLAG_TOUCH:
                reason = self._touch_reason(f.vars.get("reason"), f.vars.get("tp"))
                texts.append(f"触摸：{reason}")
                keys.append(f"touch:{reason}")
            elif f.code == R.FLAG_DISK_FULL:
                note, branch = disk_full_note(f.vars.get("state_pct"),
                                              f.vars.get("core_count"))
                texts.append(note)
                keys.append("disk-full:" + branch)
            elif f.code == R.FLAG_APP_MISSING:
                ids = f.vars.get("apps_ids") or []
                texts.append(f"应用没装上：{f.vars.get('apps_bad')}/{f.vars.get('apps_total')} 条"
                             "注册项在盘上没有文件 —— 列表丢图标、点开会黑屏")
                # 键里放**是哪几条**、不放条数（Q49 那条：会动的数字进键 = 每轮重播一句）。
                # ids 由探针截到 12 条，超出的算不进键也无妨：条数那一格是活的。
                keys.append("app-missing:" + ",".join(sorted(ids)))
            elif f.code == R.FLAG_STATE_RO:
                texts.append("状态盘不可用：改动不会保留（串口里有 ### state: unavailable）")
                keys.append(f.code)
            elif f.code == R.FLAG_NO_FRAME:
                # **两件事不许混成一句**。旧文案一律说"QMP 通了但没有画面"，可判据 `frames_ok`
                # 就是 `canvas.has_frame`（`:703`），"**从没连上**"也走这一支 —— 实测：没起 VM 的
                # 控制台在后台重连了 22 次（`vm/_qwen_ui_am.out` 07:17 起每 7 s 一条），
                # 界面还在说"QMP 通了"。而 `_on_state` 本来收得到真状态，只是以前把它丢了。
                if self._sess_state == "connected":
                    texts.append("拿不到帧：QMP 通了但没有画面")
                elif not self._sess_state:
                    texts.append("拿不到帧：还没收到过连接状态（这一轮不下结论）")
                else:
                    retry = ("后台在自动重连，连上会自己出画面"
                             if self.cfg.get("connection.reconnect.enabled", True)
                             else "这一轮不会自己重连 —— 配置里 `connection.reconnect.enabled` "
                                  "是 false（profile / config.toml / 命令行都能设，"
                                  "控制台里没有改它的按钮）")
                    texts.append(f"拿不到帧：QMP 还没连上（{retry}）")
                coarse = ("connected" if self._sess_state == "connected"
                          else "unknown" if not self._sess_state else "down")
                # 键里只放**粗档**，不放 `_sess_state` 原值：实测没 VM 时后端在
                # `connecting` ↔ `disconnected` 之间每 ~3 s 抖一次（`vm/_qwen_state.py`），
                # 原值进键 = 每 3 s 重播一条警告 —— 正是 Q49 刚修掉的那个形状（那次是键里
                # 含一个每轮都在涨的数字）。三档文案与三档键**一一对应**，不会说了另一件事。
                # ⚠ "一一对应"查过依据：这项**在 SCHEMA 里**（`config.py:75`，标签「断线自动重连」），
                # 老客户端 `view/window.py:407` 的设置面板能改它 —— 但控制台不挂那个面板
                # （grep `SettingsDialog` 在 `vmgui/console/` 零命中），且后端只在建会话时读一次
                # （`qmp.py:196`）。哪天给控制台加设置入口，这一档必须并进键，否则关掉重连后
                # 抽屉还挂着"会自己重连"那句旧话。
                keys.append(f"no-frame:{coarse}")
            elif f.code == R.FLAG_UI_STARTING:
                texts.append("笔的界面还在起：触摸桥已就位，还没有合成器/ UI 打开触摸节点"
                             "（实测约 3 分钟接上，不算故障）")
                keys.append(f.code)
            elif f.code == R.FLAG_BRIDGE_RESTARTED:
                texts.append("触摸桥换过 pid（信息位，#25 的判据在 x6pro 上会假报，见 34-F2）")
                keys.append(f.code)
        # 现象 A：通道在、却一个键都没回来 —— 既不出平安也不出故障，说出来就够
        probe = self._probe
        if probe is not None and probe.ok != "no-shell" and not probe.values:
            texts.append("探针没采到值（通道在、一个键都没回来）—— 这一轮不判触摸/状态盘")
            keys.append("probe-empty")
        if harness.packs_state() == "failed":
            texts.append(f"读固件包清单失败：{harness.packs_error()}"
                         "（空态里的「重新扫描」再试一次）")
            keys.append("packs-failed")
        # 放在去重早退**之前**。⚠ 今天这两种放法**行为一致**（千问拿错位版实测过）：键里含
        # `disk-full:<branch>`，所以"可见性要变"必然伴随"键要变"，早退那条路永远走不到不一致。
        # 还是放前面，是因为它让可见性**只依赖 `dec.flags`**，不必再借"日志没被去重"这条运气 ——
        # 哪天键的组成一改（比如把 branch 摘出去），错位版会悄悄坏而测试照样绿。
        self.btn_corefile.setVisible(any(f.code == R.FLAG_DISK_FULL for f in dec.flags))
        self.btn_apps.setVisible(any(f.code == R.FLAG_APP_MISSING for f in dec.flags))
        key = " | ".join(keys)
        if key == self._last_flags_key:
            return
        self._last_flags_key = key
        if texts:
            self._log_append("[警告] " + " | ".join(texts))

    # ------------------------------------------------------------------ #
    # 文案（照 `31-console-copy.md` §3 / §5 抄）
    # ------------------------------------------------------------------ #
    def _empty_content(self, dec: R.Decision):
        v = dec.vars
        if dec.view == R.VIEW_NO_PACK:
            if not self._packs:
                return ("还没有可用的固件",
                        "`packs/` 里一个固件包都没有。镜像不在代码包里（版权归网易有道）。\n"
                        "跑 `dpctl packs` 会列出要放哪几个文件；或按 `docs/固件包开发教程.md` §2 "
                        "复制一个同族包改路径。",
                        [("打开 packs 目录", self._open_packs),
                         ("重新扫描", self._rescan)])
            missing = "、".join(v.get("missing") or ["（未列出的文件）"])
            return ("还没有可用的固件",
                    f"`packs/{v.get('pack', '?')}/images/` 里缺 {missing}。"
                    "镜像不在代码包里（版权归网易有道）。",
                    [("打开 packs 目录", self._open_packs),
                     ("重新扫描", self._rescan)])
        if dec.view == R.VIEW_VM_DOWN:
            tail = _wrap_hard(harness.serial_tail(40).strip()[-600:])
            # "现在管的是哪台"必须写进正文：标题只说"没在跑"，而下面那格串口尾是**上一次开机**
            # 的日志（可能是别的包）—— 刚换完设备时，不写明目标就会让人以为画面/日志都是新机器的。
            pw, ph = harness.panel(self.pack)
            target = (f"目标设备：{self.pack}"
                      + (f"（面板 {pw}×{ph}）" if pw and ph else "")
                      + "；按「启动」开机。\n\n") if self.pack else ""
            return ("虚拟机未运行",
                    target
                    + "以下为宿主 `vm\\serial.log` 中最后一次开机的串口输出；查看不需要虚拟机运行。\n\n"
                    + (tail if tail else "（vm\\serial.log 暂无内容）"),
                    [("启动", self._start_clicked), ("查看上次串口输出", self._focus_serial)])
        if dec.view == R.VIEW_BOOTING:
            up = P.uptime_s(self._probe.values) if self._probe else None
            waited = (f"已等 {int(up // 60)} 分 {int(up % 60)} 秒" if up
                      else f"已等 {int(time.time() - self._started_at)} 秒")
            if dec.has(R.FLAG_UI_STARTING):
                # 有 shell，但笔的界面还没接上触摸 —— 实测起机 ~38 s 空、~185 s 接上
                return (f"笔的界面还在起 · 已等 {waited}",
                        "触摸桥已经在跑（`readers` 里有 `perl`），但还没有合成器或笔的 UI 打开"
                        "触摸节点。\n实测（x6pro）约 3 分钟接上 —— 这段时间**不算故障**，灯不报错。",
                        [])
            return (f"首启进行中 · {waited}（实测 6~7 分钟）",
                    "正在解预设包到 /userdisk，纯软件模拟下就是这么慢。\n"
                    "这段时间界面一直是「正在初始化配置」，还会自己重启一次 —— 都是正常的。\n"
                    "判据：homeModeIconShow ✓ / systemEnvCustomPresetAppInstalled ✗\n"
                    "（§5 第 2 条：**不做进度条**；pid 变化期间不显示「崩溃/掉线」。）",
                    [])
        if dec.view == R.VIEW_QMP_BUSY:
            return ("另一个实例正在显示画面",
                    "QEMU 的 QMP 只服务一个客户端，而这个控制台要靠它抓帧和注入手势。\n"
                    f"当前持有者：{v.get('owner', '未知')}。\n"
                    "`接管` = 关掉那个实例（QEMU 不受影响）；`只看不动` = 保持只读，不注入任何输入。",
                    [("接管", self._take_over), ("只看不动", self._readonly_note)])
        # #3/#4/#5 都只是**警告**（F7）：它们不该接管视图 —— 盘 95% 不等于没得看，
        # 触摸坏了更不等于没得看。正文与出口在抽屉里（`_note_flags`），这里不出空态。
        return ("", "", [])

    def _touch_reason(self, reason, tp) -> str:
        """触摸三环的措辞（#46：第四条已从判据里删除，不再有 `bridge-restarted` 这一支）。

        坐标恒 `(0,0)` 只是**排查提示**，不进判据 —— #42 实测"读者会自己关掉已销毁的节点、
        桥回来后同名 pid 重开新节点"，所以"读者还握着旧节点"是推不出来的。
        """
        return {R.RING_NO_BRIDGE: "触摸桥未运行",
                R.RING_NO_UINPUT: "触摸桥没造出 /dev/uinput 节点",
                R.RING_NO_READER: f"没人打开 {tp or '触摸节点'}"}.get(reason, "未知原因")

    # ------------------------------------------------------------------ #
    # 状态栏串（`31-console-copy.md` §2 + `32-…` §5 的倍数 token）
    # ------------------------------------------------------------------ #
    def _status_text(self, dec: R.Decision) -> str:
        entry = self._pack_entry()
        cw, ch = self.canvas.oriented_size()
        probe = self._probe
        ok = probe.ok if probe else "no-shell"
        vals = probe.values if probe else {}
        alive = ok in ("ok", "serial") and bool(vals)   # 采到了东西才算"活着"
        pct, core = P.state_pct(vals), P.core_count(vals)
        holder = P.holder_pid(vals)
        pstate = harness.packs_state()          # #52：fresh / scanning / failed

        parts = [self.pack or ("无包" if pstate == "fresh" else "固件包 —"),
                 (entry or {}).get("runtime") or "-"]
        # 三态（Q56 / `31-console-copy.md` §10）：**从没收到过帧，就无所谓"上次"**。
        # 旧写法只问"机器在不在跑"，于是"空控制台、VM 从没起过、一帧没收过"会印
        # `帧 560×170（上次）` —— 替没发生过的事背书。判据换成 `has_frame`（与"存图"那颗键同一个）。
        # ⚠ 没帧这一档的尺寸**不许**取 `oriented_size()`：它无帧时兜底的是 `display.crop`
        # （`canvas.py:53-56`），而裁剪线是**上一次会话**写进 config 的、可能是**别的包**的尺寸。
        # 所以这里直接读包声明 `harness.panel(self.pack)` —— 这一格的本意就是"这个包说自己是多大"。
        if self.canvas.has_frame:
            stale = "" if harness.vm_running() else "（上次）"
            parts.append(f"帧 {cw}×{ch}{stale} · {self._zoom_token()}")
        else:
            pw, ph = harness.panel(self.pack) if self.pack else (0, 0)
            parts.append(f"面板 {pw}×{ph}" if pw and ph else "帧 —")
        # `39` §2 #5/#6：**未达阈值就不占位**。常驻的 `状态盘 88%` 与 `corefile 5`
        # 让人以为在报警、也分不出哪个要紧（s7pro 29% 与 a7pro 88% 同串同权重）。
        # ⚠ 但"没读到"与"读到了、没事"**不能共用"不占位"**（#30）：旧写法在 `alive` 却
        # `pct is None` 时让整格蒸发，用户看到的就是"没有状态盘这回事"。所以分三档 ——
        # 整轮没结果 `—` ／ 这一轮没这个字段 `未知` ／ 有结果但未达阈值 不占位。
        # 用词跟「文件」页那条"剩余空间 未知"保持一致（`42` §2.2）。
        if not alive:
            parts.append("状态盘 —")
            parts.append("corefile 目录 —")
            parts.append("应用 —")
        else:
            if pct is None:
                parts.append("状态盘 未知")
            elif pct >= R.DEGRADE_STATE_PCT:
                parts.append(f"状态盘 {pct}%")
            # 这一格也要过阈值才占位（上面那条注释对两格都成立）。旧写法是 `if core:` ——
            # 于是"没事"的机器也常驻一个 `corefile 目录 5 项`，而抽屉里根本不报警：**两个面
            # 对"什么时候该提这件事"两套说法**（Q49 的同一族）。措辞见 `31` §9：这里数的是
            # **目录条目**，不是 core 转储（a6pro 实测 750 项、真 core 0 个）。
            if core is None:
                parts.append("corefile 目录 未知")
            elif core > R.DEGRADE_CORE:
                parts.append(f"corefile 目录 {core} 项")
            # Q70 那一格：与上面两格同一套三档（整轮没结果 `—` ／ 这一轮没这个字段 `未知` ／
            # 有结果但未达阈值 **不占位**）。`apps=-`（这台没有应用注册表）走的是"采到了、
            # 但没有可体检的对象"，与"没采到"不是一件事，所以**不占位**，解释留在抽屉那行。
            adapt = P.app_adapt(vals)
            if "apps" not in vals:
                parts.append("应用 未知")
            elif adapt and adapt[1]:
                parts.append(f"应用 {adapt[1]} 个没装上")
        if alive:
            if holder:
                parts.append(f"触摸 {holder[1]}({holder[0]})")
            elif dec.has(R.FLAG_UI_STARTING):
                parts.append("触摸 起…")        # 起机 38~185 s 实测：还没接上，不算故障
            elif "holder" not in vals:
                # `holder_pid()` 对"字段没回来"和"字段是空的"回同一个 None（`probe.py:205-214`），
                # 而"这一轮没人打开触摸节点"是**结论**、"不知道"不是 —— 不许替前者说话。
                parts.append("触摸 未知")
            else:
                parts.append("触摸 无人")
        else:
            parts.append("触摸 —")
        parts.append("运行 " + self._uptime_token(vals, alive))
        if ok == "serial":
            parts.append("探针 串口")            # 串口输出里混着历史控制台日志，字段可能是旧的
        elif ok == "no-shell":
            parts.append("探针 无输出")
        if self._paused:
            # 画面停了必须说出来 —— 否则一行读数会替一个不再更新的画面背书
            parts.append("画面 暂停")
        elif (self._sess_state and self._sess_state != "connected"
              and not self.canvas.has_frame and harness.vm_running()):
            # 同一条规则的另一半：机器在跑、一帧没收过，而原因住在控制台自己手里
            # （QMP 没连上）。抽屉平时是关着的，状态栏是**唯一**说得出这件事的地方。
            # ⚠ 三个条件缺一不可：`_sess_state == ""` 是"还没收到状态"不是"没连上"（#30）；
            #    已经有帧就不该再说没连上（那是上一轮的旧状态）；VM 没在跑时空态已经讲过了。
            parts.append("画面 未连上")
        text = " · ".join(parts)

        # #31：`z<1` 只是几何事实（宿主屏放不下），原因进提示行，不画虚线
        t = self.canvas.transform()
        z = t.sx if t is not None else float(self.cfg.get("display.initial_zoom", 1.0))
        tip = ""
        if pstate == "scanning":
            tip = "正在读固件包清单。"          # #52：不许承诺"几十秒"（第一趟不算 MD5）
        elif pstate == "failed":
            tip = f"读固件包清单失败：{harness.packs_error()}"
        if not tip and z < 1.0:
            tip = "按宿主屏缩放（1× 放不下）"
        if not tip and alive and P.uptime_s(vals) is None:
            # 那一格写成"未知"就完事了，但**为什么未知**得有个出口（§7 禁第 2 条）。
            # 命令进提示行是允许的（§7 禁第 3 条只禁正文）。
            tip = ("「运行」那一格取的是**客机的 uptime**，这一轮探针没采到它 —— "
                   "不是 0，也别当成刚开机。想自己确认：「命令」页跑 "
                   "cut -d' ' -f1 /proc/uptime")
        self.lbl_status.setToolTip(tip)

        # 形状（#16）：反色实心 = 故障，尾注 = 降级；启动/首启期间"采不到"是预期，不标
        if dec.view in (R.VIEW_VM_DOWN, R.VIEW_BOOTING):
            return text
        if dec.view == R.VIEW_QMP_BUSY or dec.has(R.FLAG_STATE_RO) \
                or dec.has(R.FLAG_NO_FRAME) or ok == "no-shell":
            return "▍ " + text + "　(故障)"
        if dec.has(R.FLAG_DISK_FULL) or dec.has(R.FLAG_APP_MISSING) or ok == "serial" \
                or (alive and holder is None and not dec.has(R.FLAG_UI_STARTING)) \
                or pstate == "failed" \
                or (pstate == "scanning" and harness.packs_scan_age() > 10):
            return text + "　(降级)"
        return text

    def _zoom_token(self) -> str:
        t = self.canvas.transform()
        z = t.sx if t is not None else float(self.cfg.get("display.initial_zoom", 1.0))
        return _zoom_text(z)

    def _uptime_token(self, vals, alive: bool = True) -> str:
        """`运行` 那一格**只印客机的 uptime**，采不到就写"未知"。

        旧写法在 uptime 缺失时回落到 `time.time() - self._started_at`（控制台自己开机的时刻，
        点「启动」时还会重置），于是那一格改口说的是"这个界面开了多久"、标签却还是"运行"。
        两个方向都会坏：接管一台已经跑了 3 小时的机器 → 印 `运行 0 分`；机器中途自己重启
        （正是这一格的用途，`30-console-ui.md` §4 那行写的就是"判是不是刚自己重启过"）→
        数字继续单调往上涨，**把重启抹平了**。`31-console-copy.md` §5 早就禁过墙钟
        （"重启过会倒退"），只是这条路径没照做。
        ⚠ 附带好处：这一支不再问 `harness.vm_running()`（Q60 那条热路径上的又一省）。
        """
        if not alive:
            return "—"
        up = P.uptime_s(vals)
        return f"{int(up // 60)} 分" if up is not None else "未知"

    def _pack_entry(self):
        return next((p for p in self._packs if p.get("name") == self.pack), None)

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def _start_clicked(self) -> None:
        if not self._packs:
            self._note("没有可用的固件包；请按空态提示放置镜像文件")
            return
        pack = self.pack
        if not pack:
            ready = [p["name"] for p in self._packs if p.get("ready")]
            if len(ready) != 1:
                # 旧文案让人回命令行 `--pack` —— 在这个界面里那等于"这条路没有"。
                # 现在直接把「设备」菜单展开，挑一台就是下一步（人点名：控制台没有切换设备选项）。
                self._note(f"有 {len(ready)} 台可用；请先在「设备」中选择（菜单已展开）")
                self.btn_pack.showMenu()
                return
            self._apply_pack(ready[0], announce=False)
            self._note(f"仅 {ready[0]} 可用，已直接启动")
            pack = ready[0]
        self._busy = True
        self.act_start.setEnabled(False)
        self._started_at = time.time()
        threading.Thread(target=self._start_worker, args=(pack,), daemon=True).start()

    def _start_worker(self, pack: str) -> None:
        rc, out = harness.start_vm(pack)
        self.bridge.dpctl.emit("启动", rc, out)

    def _fast_start_clicked(self) -> None:
        pack = self.pack
        if not pack:
            self._note("请先在「设备」中选择目标机型")
            return
        if not harness.fast_status(pack).get("ready"):
            self._note("当前机型尚未创建快照；请先使用「保存快照」")
            return
        self._busy = True
        self.act_start.setEnabled(False)
        self.act_fast.setEnabled(False)
        self._started_at = time.time()
        threading.Thread(target=self._fast_start_worker, args=(pack,), daemon=True).start()

    def _fast_start_worker(self, pack: str) -> None:
        rc, out = harness.start_vm_fast(pack)
        self.bridge.dpctl.emit("快速启动", rc, out)

    def _fast_save_clicked(self) -> None:
        if harness.vm_running():
            self._note("虚拟机正在运行；请先关机再保存快照")
            return
        pack = self.pack
        if not pack:
            self._note("请先在「设备」中选择目标机型")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("保存快速启动快照")
        box.setText("将当前状态保存为快速启动快照？")
        box.setInformativeText(
            "流程：关机 → 以无音频模式启动至首页 → 保存虚拟机状态，预计约 1.5 分钟。\n"
            "已有快照将被覆盖。普通「启动」仍为带音频的冷启动。")
        ok = box.addButton("保存快照", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not ok:
            self._note("操作已取消；现有快照未变更")
            return
        self._busy = True
        self.act_fast_save.setEnabled(False)
        threading.Thread(target=self._fast_save_worker, args=(pack,), daemon=True).start()

    def _fast_save_worker(self, pack: str) -> None:
        rc, out = harness.save_fast(pack)
        self.bridge.dpctl.emit("保存快照", rc, out)

    def _stop_clicked(self) -> None:
        self._busy = True
        threading.Thread(target=lambda: self.bridge.dpctl.emit("关机", *harness.stop_vm()),
                         daemon=True).start()

    def _restart_clicked(self) -> None:
        """重启：**看得见两步**（决策 #71）—— 每一步都进「命令」格，不藏起来。"""
        pack = self.pack
        self._busy = True
        self.act_restart.setEnabled(False)

        def work():
            for label, rc, out in harness.restart_vm(pack):
                self.bridge.dpctl.emit(label, rc, out)

        threading.Thread(target=work, daemon=True).start()

    def _cut_clicked(self) -> None:
        """立即断电（`dpctl down --force`）：不请 guest 关机，直接收进程。"""
        self._busy = True
        threading.Thread(target=lambda: self.bridge.dpctl.emit(
            "立即断电", *harness.stop_vm(force=True)), daemon=True).start()

    def _fresh_clicked(self) -> None:
        """「重建状态盘…」—— 二次确认（#15/#71），确认后才留档 + `up --fresh`。

        确认弹窗的按钮顺序刻意让**默认键是「取消」**（回车不该落在破坏性动作上）；
        文案由 `fresh_warning_text` 生成，带盘名 + 大小。
        """
        pack = self.pack or ""
        path, size = harness.state_image(pack)
        if size <= 0:
            self._note("没有状态盘可重建（首次启动会自动新建一份）")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("重建状态盘")
        box.setText(f"重建 {pack} 的状态盘？")
        box.setInformativeText(fresh_warning_text(pack, path, size))
        ok = box.addButton("重建", QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)          # 回车 = 取消
        box.exec()
        if box.clickedButton() is not ok:
            self._note("已取消重建状态盘")
            return
        self._busy = True
        self.act_start.setEnabled(False)
        self.btn_fresh.setEnabled(False)
        self._note(f"开始重建 {pack} 的状态盘（先留档 .bak，再起机）…")

        def work():
            rc, out = harness.start_vm_fresh(pack)
            self.bridge.dpctl.emit("重建状态盘", rc, out)

        threading.Thread(target=work, daemon=True).start()

    def _on_dpctl(self, action: str, rc: int, out: str) -> None:
        self._busy = False
        self._log_append(f"$ {action}  (rc={rc})\n{out.strip()[-2000:]}")
        self._note(f"{action}：rc={rc}。详情见「命令」。")
        pending, self._pending_pack = self._pending_pack, ""
        if pending and action == "关机":
            # 「关机并换到 X」的第二半。**只在真关成了**之后才改目标：关不成却把界面切成
            # 另一台，那一屏读数就成了假的（跑着的是 A，界面声称在管 B）。
            if rc == 0:
                self._apply_pack(pending)
                return
            self._note(f"关机失败（rc={rc}）；目标仍为 {self.pack or '未定'}，"
                       f"未切换至 {pending}。如需立即切换，请先使用「立即断电」。")
        self._kick_probe()

    def _look_corefile(self) -> None:
        """Q49 警告的出口：把列目录的命令**填好**，回车由人按。

        不替用户 `_shell_submit()`：警告是探针每 5 s 刷出来的，可能正落在人家敲到一半的
        命令上 —— 出口不许自己发起一次 SSH。覆盖前若那格有内容，先把旧命令**回显**到日志，
        免得吃掉正在写的东西还一声不响。
        """
        cur = self.shell_in.text().strip()
        if cur:
            self._log_append(f"· 上面那格原来写着「{cur}」（没执行，先留在这一行）")
        self.shell_in.setText(COREFILE_LS_CMD)
        self.shell_in.setFocus()

    def _look_apps(self) -> None:
        """Q70 警告的出口：把那几个应用的**落点**列成一条 `ls -ld` 填好，回车由人按。

        与 `_look_corefile` 同一套纪律：不 `_shell_submit()`（警告每 5 s 刷一轮，可能正落在
        人家敲到一半的命令上），覆盖前先把旧内容回显。
        """
        f = self._decision.flag(R.FLAG_APP_MISSING) if self._decision else None
        cmd = app_missing_ls_cmd(f.vars.get("apps_at") if f else [])
        if not cmd:
            self._log_append("· 这一轮没带回这些应用的落点路径（`installPath` 是空的），"
                             "没法列 —— 直接看「guest 应用」页那几行")
            return
        cur = self.shell_in.text().strip()
        if cur:
            self._log_append(f"· 上面那格原来写着「{cur}」（没执行，先留在这一行）")
        self.shell_in.setText(cmd)
        self.shell_in.setFocus()

    def _shell_submit(self) -> None:
        cmd = self.shell_in.text().strip()
        if not cmd:
            return
        if self._shell_busy:
            self._log_append("· 上一条命令还没回来")
            return
        self.shell_in.clear()
        self._shell_busy = True
        self.shell_in.setEnabled(False)
        self._log_append(f"$ {cmd}")
        threading.Thread(target=self._shell_worker, args=(cmd,), daemon=True).start()

    def _shell_worker(self, cmd: str) -> None:
        rc, out = harness.run_dpctl(["sh", "-c", cmd], timeout=60)
        self.bridge.shell.emit(cmd, rc, out)

    def _on_shell(self, cmd: str, rc: int, out: str) -> None:
        self._shell_busy = False
        self.shell_in.setEnabled(True)
        self.shell_in.setFocus()
        self._log_append((out.rstrip() or "(无输出)") + f"\n[rc={rc}]")
        self._kick_probe()                    # 命令会改机器状态 → 立刻重采一轮

    def _shot_clicked(self) -> None:
        dest = os.path.join(harness.VM, "screen.png")
        if self.session.screenshot(dest, "png"):
            self._note(f"截图请求已提交 → {os.path.relpath(dest, harness.ROOT)}")
        else:
            self._note("未获取到画面：虚拟机未运行，或 QMP 已被其他实例占用")

    def _tap_guest(self, gx: int, gy: int) -> None:
        """在 guest 坐标上点一下（按下 → 按住 TAP_HOLD_MS → 抬起）。走 QMP，与点画布同一条链。"""
        if self._readonly:
            self._note("只读视图：不注入任何输入")
            return
        self.canvas.set_marker((gx, gy))
        self.session.pointer(gx, gy, 1, True)
        QTimer.singleShot(TAP_HOLD_MS, lambda: self.session.pointer(gx, gy, 0, True))

    def _escape_clicked(self) -> None:
        """「点左上角」应急键（#74）：把左上角那个返回位按**面板比例**点一下。

        为什么需要它：拦路页（"开始激活"）实测**整页纯黑 + 一颗发光按钮，无 ×、无返回**，
        `sendkey esc/backspace` 对它**零作用** —— 触摸链一断人就被锁死。
        这颗键按的是画面上的坐标，**不依赖 guest 的 UI 有没有给我们留出口**。
        """
        cw, ch = self.canvas.content_size()
        if not cw or not ch:
            self._note("当前无画面；请等待首帧后再试")
            return
        gx, gy = escape_tap_point(cw, ch, self.pack or "")
        self._tap_guest(gx, gy)
        self._note(f"已点击左上角返回位置（面板坐标 {gx},{gy}）")

    def _pause_toggled(self, on: bool) -> None:
        """「暂停画面」开关：暂停 = 不再按节奏取帧；继续 = 立刻补一帧（`39` §2 #7）。"""
        self._paused = bool(on)
        # 跨过这条边界的**第一帧不算节奏样本**：暂停期间"存图"和"继续"都会强制抓一帧
        # （`qmp.py:287` 认 force），那一帧与上一帧的间隔里装着**整段暂停时长** ——
        # 旧写法照记 → 24 格里多进一个 180 000，读数成了
        # `取帧 7 711 ms（220~180 000）`（23 个 220 ms 样本 + 那一个，均值算过），
        # 把"你刚才没在取帧"讲成"机器取一帧要 7.7 秒"（Q64 第二条）。
        # 两个方向都要置位：置一次只挡得住一帧。
        self._gap_discard = True
        self.session.set_paused(self._paused)
        self._refresh_rate_label()
        if self._decision is not None:
            self.lbl_status.setText(self._status_text(self._decision))

    def _focus_serial(self) -> None:
        if not self._drawer_open:
            self._toggle_drawer(True)
            self.act_drawer.setChecked(True)
        self.tabs.setCurrentWidget(self.serial)

    def _open_packs(self) -> None:
        path = os.path.join(harness.ROOT, "packs")
        try:
            os.startfile(path)                    # noqa: S606 (Windows)
        except (AttributeError, OSError) as e:
            self._note(f"打不开 {path}: {e}")

    def _rescan(self) -> None:
        harness.rescan()
        self._note("已重新扫描 packs/")
        self._kick_probe()

    def _take_over(self) -> None:
        """接管 = 关掉那个占着 QMP 的实例（QEMU 不受影响）。"""
        owner = harness.qmp_owner(exclude_pid=os.getpid())
        if not owner:
            self._note("没有别的实例占着 QMP")
            return
        label, _, pid = owner.partition(" pid ")
        fn = {"vmgui": harness.DPCTL.stop_vmgui,
              "console": harness.DPCTL.stop_console}.get(label)
        if fn is None or not pid.isdigit():
            self._note(f"不认识这个持有者：{owner}")
            return
        fn()
        self._note(f"已接管：收掉 {owner}（QEMU 不受影响）")
        self._kick_probe()

    def _readonly_note(self) -> None:
        self._readonly = True
        self._note("只读视图：不注入任何输入（#16：降级只加尾注，不加形状）")

    def _note(self, text: str) -> None:
        self._log_append("· " + text)

    def _log_append(self, text: str) -> None:
        """往「命令」格追加 —— 构造时那句占位说明先擦掉（它只是空态引导，不该留成历史）。"""
        if self.cmdlog.toPlainText().startswith(SHELL_PLACEHOLDER):
            self.cmdlog.clear()
        self.cmdlog.appendPlainText(text)

    def _refresh_serial(self) -> None:
        tail = harness.serial_tail(300)
        if tail == self.serial.toPlainText():
            return
        sb = self.serial.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.serial.setPlainText(tail)
        if at_bottom:
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ #
    # 会话事件（帧 / 分辨率 / 状态 / 存图）—— 都在 UI 线程里跑
    # ------------------------------------------------------------------ #
    def _on_frame(self, frame) -> None:
        if self._refit_next_frame:
            # 刚换过设备：旧窗口的形状是给旧面板拖的，不作数。放在这里而不是 `_apply_pack`
            # 里，是因为清帧/换标题会带出一个"意料之外"的 resize 事件，把 `_user_sized` 又置回 True。
            self._refit_next_frame = False
            self._user_sized = False
        now = time.monotonic()
        if self._last_frame_t and not self._gap_discard:
            self._frame_gaps.append((now - self._last_frame_t) * 1000.0)
        self._last_frame_t = now
        self._gap_discard = False
        self.canvas.set_frame(frame)
        self._apply_crop_from_frame()
        self._fit_window()
        self._refresh_rate_label()
        if self._decision is not None:
            self.lbl_status.setText(self._status_text(self._decision))

    def _apply_crop_from_frame(self) -> None:
        """首帧一到就复核裁剪线（`33-crop-and-coordinates.md` §3）。

        写入者只有控制台一个，规则只有 `harness.crop_for` 一个（决策 #35）——
        面板 ≥ 帧时**不裁**（声明过期或换包没重启），提示行要写"按帧"而不是假装裁过。
        """
        gw, gh = self.canvas.guest_size
        if not gw or not gh:
            return
        pw, ph = harness.panel(self.pack)
        cur = self.cfg.get("display.crop") or {}
        new = harness.crop_for(pw, ph, gw, gh)
        if (cur.get("enabled"), cur.get("w"), cur.get("h")) == \
                (new["enabled"], new["w"], new["h"]):
            return
        self.cfg.set("display.crop", new)
        self.canvas.set_config(self.cfg)
        self.canvas.updateGeometry()

    def _on_resolution(self, w: int, h: int) -> None:
        self.canvas.updateGeometry()
        self._fit_window()          # 分辨率变了就重新套一次（除非用户已经拖过窗口）

    def _on_state(self, state: str, detail: str) -> None:
        self._sess_state = state              # connected / connecting / disconnected
        self._pointer_mode = "absolute"

    def _on_shot(self, dest: str, ok: bool, err: str) -> None:
        self._note(f"截图{'已保存' if ok else '保存失败'}: {dest}" + (f"  ({err})" if err else ""))

    def _canvas_avail(self) -> tuple[int, int]:
        """画布控件的**最大**可用区：宿主屏可用区扣掉工具条 + 状态栏 + 抽屉 + 边距。

        **不能用当前窗口尺寸算** —— 首帧可能比 `show()` 先到，那时窗口还是 Qt 给的默认
        尺寸。实测（离屏跑真 QEMU）就撞到了：窗口 640 宽 + 抽屉 300，画布只剩 340，
        算出 `0.2×`。按屏的可用区算，先开出来的窗口与首帧到达后**一致**，
        也不会在首帧那一刻跳一次（`32-first-frame-sizing.md` §2/§0.3）。
        """
        scr = self.screen()
        geo = scr.availableGeometry() if scr is not None else None
        base_w = geo.width() if geo is not None else self.width()
        base_h = geo.height() if geo is not None else self.height()
        drawer = self.drawer.width() if self._drawer_open else 0
        chrome_h = self.toolbar.sizeHint().height() + STATUS_BAR_PX + 8
        return max(200, base_w - drawer - 24), max(200, base_h - chrome_h - 48)

    def _fit_window(self) -> None:
        """把窗口套到「帧 × z」上；**用户拖过之后就不再动**（`32-…` §0.3 / §4.3）。

        为什么不是"只按首帧定一次"：实测（s7pro，2026-10-18）—— `up` 完 2 s 抓到的第一帧是
        **640×480**（guest 的 DRM mode 还没设，QEMU 还是早期 scanout），真正到声明的
        1020×240 要等客机起完。按首帧定一次会把窗口锁在错的尺寸上，所以改成"跟着分辨率走，
        直到用户接手"。

        `z` 的来源分两种：**用户选的固定倍率**（`self._zoom`，锁住）或**自动适合窗口**
        （`scaling.initial_scale`）。倍率**总是**应用到画布；窗口尺寸仍受 `_user_sized` 约束
        —— 用户拖过窗口之后，他选的倍率照样生效，但窗口归他自己。
        """
        cw, ch = self.canvas.oriented_size()
        if not cw or not ch:
            return
        z = self._zoom if self._zoom else scaling.initial_scale(cw, ch, *self._canvas_avail())
        self.cfg.set("display.initial_zoom", z)
        self.canvas.set_config(self.cfg)
        self.canvas.updateGeometry()
        if self._user_sized or self.isFullScreen():
            return                          # 用户拖过 / 正在全屏：窗口归他（或归窗口管理器），只改画布

        hint = self.canvas.sizeHint()
        drawer = self.drawer.width() if self._drawer_open else 0
        chrome_h = self.toolbar.sizeHint().height() + STATUS_BAR_PX + 24
        w, h = hint.width() + drawer, hint.height() + chrome_h
        scr = self.screen()
        if scr is not None:                        # 别开出一个比屏还大的窗口
            geo = scr.availableGeometry()
            w, h = min(w, geo.width()), min(h, geo.height())
        if (w, h) == (self.width(), self.height()):
            return                          # 尺寸已经一致：不请求，也不会来事件
        self._self_size = (w, h)
        self._expect_resize = True          # 下一个 resize 事件是**我们**的（Qt 可能钳它）
        self.resize(w, h)

    def resizeEvent(self, event) -> None:                       # noqa: N802
        """判"用户拖过窗口"靠**事件序列**，不靠尺寸数值比对。

        三条踩过的坑：

        * 设"self-resize 标志然后在末尾复位"不行：Qt 的 resize 事件可能**延后投递**，
          事件到达时标志早复位了 → 程序自己的 resize 被当成用户拖拽。
        * 拿实际尺寸和请求尺寸**精确**比对也不行：Qt 会因为布局最小尺寸 / 屏幕钳制把请求改掉，
          实测被改掉过 **24 px**（请求 `800x127`，实际 `800x151`）→ 同样误判。
        * `show()` 时 Qt 会先按默认几何摆一次窗口，那时我们还没请求过任何尺寸 —— 不算用户接管。

        所以：**我们一请求 resize，就预期紧接着来一个事件**（不管 Qt 把它钳成多少），
        在那个事件里把基线校正成实际值；此后尺寸再变才算用户拖的。
        """
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        if self._expect_resize:
            self._expect_resize = False
            self._self_size = (w, h)        # 校正基线：接受 Qt 的钳制
            return
        if self._self_size == (0, 0):
            return                          # show() 的初始几何，还没请求过任何尺寸
        sw, sh = self._self_size
        if abs(w - sw) <= RESIZE_TOLERANCE_PX and abs(h - sh) <= RESIZE_TOLERANCE_PX:
            self._self_size = (w, h)        # 几像素的布局微调：吸收掉，别累积漂移
            return
        log.debug("尺寸 %dx%d 与我们的 %dx%d 差得超过 %d px → 视为用户接管",
                  w, h, sw, sh, RESIZE_TOLERANCE_PX)
        self._user_sized = True

    # ------------------------------------------------------------------ #
    # 输入（单点；滚轮 → 一次整屏 swipe 由 canvas 的按钮位表达，决策 #9）
    # ------------------------------------------------------------------ #
    def _on_mouse_move(self, pos, buttons) -> None:
        if not self._touch_active and not self._on_picture(pos):
            return                      # 画面外的悬停：不钳到边缘去打扰客机
        self._deliver(pos, buttons, absolute=True)

    def _on_mouse_press(self, pos, buttons) -> None:
        if not self._on_picture(pos):
            return                      # 点黑边不算触摸（钳到边缘 = 点在画面最上/最下一行）
        self._touch_active = True
        self._deliver(pos, buttons, absolute=True)

    def _on_mouse_release(self, pos, buttons) -> None:
        if not self._touch_active:
            return                      # 没在画面里按下过，就没有"抬起"要发
        self._touch_active = False      # 手指拖到画面外再抬，也得把按压松开
        self._deliver(pos, buttons, absolute=True)

    def _sync_key_actions(self) -> None:
        """按包刷新三颗实体键：亮不亮、发什么码都来自 patches/vm-keys.env。"""
        codes = harness.keys_for(self.pack)
        for name, a in self._key_actions.items():
            code = codes.get(name)
            a.setEnabled(bool(code))
            a.setToolTip(a.property("tip_ok") if code else
                         "当前固件未配置该按键（packs/<名>/patches/vm-keys.env）")

    def _send_key(self, name: str) -> None:
        """按一次"实体键"：码按包查，再往 fake-keys 的 FIFO 写（Q87/Q88/Q92）。"""
        code = harness.keys_for(self.pack).get(name)
        label = {"home": "Home", "vol_down": "音量减", "vol_up": "音量加"}.get(name, name)
        if not code:
            self._note(f"当前固件未配置 {label} 键")
            return
        if not harness.vm_running():
            self._note("虚拟机未运行；实体按键由客机处理")
            return
        cmd = (f"if [ -p /tmp/fake-keys.fifo ]; then echo {code} > /tmp/fake-keys.fifo; "
               f"else echo NO_FIFO; fi")
        threading.Thread(
            target=lambda: self.bridge.shell.emit(
                f"key {name}={code}", *harness.run_dpctl(["sh", "-c", cmd], timeout=20)),
            daemon=True).start()

    def _on_picture(self, pos) -> bool:
        t = self.canvas.transform()
        return t is not None and mapper.inside(pos.x(), pos.y(), t)

    def _deliver(self, pos, buttons, absolute: bool) -> None:
        if self._readonly:
            return
        t = self.canvas.transform()
        if t is None:
            return
        gx, gy = mapper.screen_to_guest(pos.x(), pos.y(), t)
        self.canvas.set_marker((gx, gy))
        self.session.pointer(gx, gy, buttons, absolute)

    def keyPressEvent(self, event) -> None:              # noqa: N802
        # F11 全屏必须在窗口这一层消化掉，**不能**落到 keymap：那里 F11 映射成客机键码 87，
        # 按下去只是把 F11 塞给笔（笔不认），人看到的就是"按了 F11 没反应"。
        if event.key() == Qt.Key_F11:
            self._toggle_fullscreen()
            return
        if self.isFullScreen() and event.key() == Qt.Key_Escape:
            self._toggle_fullscreen()                     # 全屏下 Esc 退出（不然只能再按 F11）
            return
        if event.key() == Qt.Key_Tab:
            self.act_drawer.toggle()
            return
        code = self.keymap.code_for(event.key(), event.text())
        if code:
            self.session.key(code, True)
        else:
            super().keyPressEvent(event)

    def _toggle_fullscreen(self) -> None:
        """F11：窗口铺满屏幕；再按 F11 或 Esc 回来。

        全屏的是**窗口**，工具条/状态栏/抽屉都还在（抽屉用 Tab 收）；画面本身仍按「适合」摆 ——
        全屏之后画布更高，画面会更大，但**不裁**（`build_transform` 的 fit 语义）。
        """
        self._expect_resize = True          # 这次 resize 是我们自己发起的，不算"用户拖过窗口"
        if self.isFullScreen():
            self.showNormal()
            self._fit_window()              # 回到窗口态：按新的画布重套一次「帧 × 倍率」
        else:
            self.showFullScreen()

    def keyReleaseEvent(self, event) -> None:            # noqa: N802
        if event.key() == Qt.Key_F11:
            return                       # 按下被我们拿去切全屏了，抬起也不能发给笔（否则笔收到孤立的 key-up）
        code = self.keymap.code_for(event.key(), event.text())
        if code:
            self.session.key(code, False)
        else:
            super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:                 # noqa: N802
        self.session.stop()
        harness.release_console_pid()
        super().closeEvent(event)


class _EmptyPane(QWidget):
    """视图区的空态：标题 + 正文 + 出口按钮（`31-console-copy.md` §3）。"""

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        self.title = QLabel("")
        self.title.setObjectName("emptyTitle")
        self.title.setWordWrap(True)
        self.body = QLabel("")
        self.body.setObjectName("emptyBody")
        self.body.setWordWrap(True)
        # Q48 的第二道闸：即便将来有人喂进一条没折好的长行，也别让空态把窗口撑到屏外。
        self.setMaximumWidth(1100)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.row = QWidget()
        self.row_lay = QHBoxLayout(self.row)
        self.row_lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        lay.addWidget(self.row)
        lay.addStretch(2)

    def set_content(self, content) -> None:
        title, body, buttons = content
        self.title.setText(title)
        self.body.setText(body)
        while self.row_lay.count():
            item = self.row_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, (label, slot) in enumerate(buttons):
            b = QPushButton(label)
            if i == 0:
                # 一组出口里的**第一个**是主按钮（`31-…` §3 的排法就是主动词在前）
                b.setObjectName("primary")
            b.setToolTip(TOOLTIPS.get(label.split()[0], ""))
            b.clicked.connect(lambda _=False, s=slot: s())
            self.row_lay.addWidget(b)
        self.row_lay.addStretch(1)
