"""判定：**纯函数**，与采集（``core/probe.py``）和文案分开（决策 #26）。

入 = 探针 dict + 帧状态 + 端口/宿主状态；出 = **两层**结果（决策 F7）：

* ``view`` —— **谁接管视图**。判据一句话：**接管视图的唯一资格是"没有帧可画"**。
  只可能是 ``none`` / ``no-pack`` / ``vm-down`` / ``booting`` / ``qmp-busy``。
* ``flags`` —— 可并存的**警告**，进状态栏与抽屉，**不擦掉画面**。
  ``state-ro`` / ``disk-full`` / ``touch`` / ``no-frame``。

为什么拆两层（`34-slice1-findings.md` F7）：一次只报一个码，会让"盘快满 + 桥断了"的机器
只显示盘满 —— 而用户真正的痛是**点不动**；更要命的是若视图层按"code != none 就接管视图"
实现，一条 95% 的磁盘用量就会把一幅**还在动的画面**擦掉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import probe as P

# ---- view（接管视图的资格）------------------------------------------------- #
VIEW_NONE = "none"
VIEW_NO_PACK = "no-pack"        # #1 还没有可用的固件
VIEW_VM_DOWN = "vm-down"        # #2 虚拟机没在跑
VIEW_BOOTING = "booting"        # 有 pid、还没 shell / 笔的界面还没接上 —— §5「不许做进度条」
VIEW_QMP_BUSY = "qmp-busy"      # #4b 另一个实例正在显示画面（QMP 只服务一个客户端）

# ---- flags（可并存的警告）-------------------------------------------------- #
FLAG_STATE_RO = "state-ro"      # #3 状态盘不可用，改动不会保留
FLAG_DISK_FULL = "disk-full"    # #5 状态盘快满了
FLAG_TOUCH = "touch"            # #4 触摸进不去
FLAG_NO_FRAME = "no-frame"      # VM 在跑、却拿不到帧（且没有别的实例占着 QMP）
FLAG_UI_STARTING = "ui-starting"  # 笔的界面还没接上触摸（起机 38~185 s 实测）—— 不是故障
FLAG_BRIDGE_RESTARTED = "bridge-restarted"   # **信息位**，见下
FLAG_APP_MISSING = "app-missing"  # Q70：注册表列着、盘上没文件 → 列表丢图标／点开黑屏

# 兼容旧名（`30-console-ui.md` §4 的编号仍按这几个字符串引用）
NO_PACK = VIEW_NO_PACK
VM_DOWN = VIEW_VM_DOWN
BOOTING = VIEW_BOOTING
QMP_BUSY = VIEW_QMP_BUSY
STATE_RO = FLAG_STATE_RO
DISK_FULL = FLAG_DISK_FULL
TOUCH = FLAG_TOUCH
OK = VIEW_NONE

# 降级阈值（#5 / 空态 5）：状态盘 ≥95%、corefile >500 时框架的清理本会再裁一轮。
DEGRADE_STATE_PCT = 95
DEGRADE_CORE = 500

# 触摸四环的理由（`30-console-ui.md` §3 表）
RING_NO_BRIDGE = "no-bridge"        # 桥没在跑 / 没读源设备
RING_NO_UINPUT = "no-uinput"        # 桥没造出节点
RING_NO_READER = "no-reader"        # 没人打开 <tp>

# "笔的界面还没接上"的容忍窗。实测 x6pro（2026-10-18）：起机 ~38 s 时 `readers` 只有
# `perl`、`holder` 为空，~185 s 时变成 `perl weston`。若窗内就报故障，每次开机都会
# 闪一条假故障；窗给到 600 s（首启解预设包实测 6~7 min），之后**必须**开口报 ——
# 否则 x5pro 那族"UI 读 mouse0 而不读 event*"会永远被吞掉（F5）。
UI_ATTACH_GRACE_S = 600

# **废弃的判据**（F2 实测推翻）：原先用"桥 pid 变了且 holder 没换"推断 §2.6。
# 实测 x6pro：把桥 kill 掉再手动起回来，**weston 用同一个 pid 894 重开了新节点**
# （`holder=894:weston`），于是"pid 没变"根本不能说明它握着旧节点 —— 这条判据
# 在健康机器上会假报。降级成信息位，不再升级为警告，等哪一族实测到 `(deleted)` 再说。
RING_RESTARTED = "bridge-restarted"


def pack_ready(entry: dict) -> bool:
    return bool(entry) and not entry.get("missing") and not entry.get("md5_bad") \
        and entry.get("runtime_ok", True)


@dataclass
class Signals:
    """一轮判定的全部输入。宿主侧字段由会话填，guest 侧字段来自探针。"""
    packs: list = field(default_factory=list)   # [{name,runtime,panel,ready,missing[],md5_bad[],runtime_ok}]
    packs_known: bool = True                    # False = 清单还在后台扫，**别下"没有固件"的结论**
    pack: str = ""                              # 当前（或待选）的包名
    vm_running: bool = False
    frames_ok: bool = False
    other_owner: str = ""                       # 另一个占着 QMP 的实例，如 "vmgui pid 1234"
    serial_state_error: bool = False            # vm\serial.log 里有 "### state: unavailable"
    probe: dict = field(default_factory=dict)
    probe_ok: str = "no-shell"                  # ok | serial | no-shell
    ui_started_hint: bool = False               # 串口见到 `miniapp start finished!`（**信息位**，Q1 后不参与判据）
    # 上一轮记下的桥 pid **集合** —— F4：`pidof perl` 的顺序不保证，记一个 int 会随机
    # 判出"桥重启过"；记集合才只对"桥真的换了"敏感。
    prev_bridge_pids: Optional[tuple] = None
    prev_holder_pid: Optional[int] = None


@dataclass(frozen=True)
class Flag:
    code: str
    vars: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    view: str = VIEW_NONE
    flags: tuple = ()
    vars: dict = field(default_factory=dict)
    next_bridge_pids: Optional[tuple] = None
    next_holder_pid: Optional[int] = None

    # 便利访问（旧代码/文案按"单个码"读的地方）
    @property
    def code(self) -> str:
        return self.view if self.view != VIEW_NONE else (
            self.flags[0].code if self.flags else VIEW_NONE)

    def flag(self, code: str) -> Optional[Flag]:
        return next((f for f in self.flags if f.code == code), None)

    def has(self, code: str) -> bool:
        return self.flag(code) is not None


def _find(name: str, packs: list) -> dict:
    for e in packs:
        if e.get("name") == name:
            return e
    return {}


def decide(sig: Signals) -> Decision:
    """一次判定出两层。view 只占一条，flags 可以同时有好几条（Q29：不在 view 上吞掉）。"""
    values = sig.probe
    chosen = _find(sig.pack, sig.packs) if sig.pack else {}
    pack_bad = bool(sig.packs_known and sig.pack and not pack_ready(chosen))
    no_pack_view = (sig.packs_known and not sig.packs) or pack_bad

    # 这两条根本没有客机：没什么可警告的，直接接管视图
    if no_pack_view:
        return Decision(VIEW_NO_PACK, (), _view_vars(VIEW_NO_PACK, sig, chosen))
    if not sig.vm_running:
        return Decision(VIEW_VM_DOWN, ())
    if sig.probe_ok == "no-shell":
        # 连 shell 都还没有：探针没值，谈不上警告（状态栏另有 #36 的前提处理）
        return Decision(VIEW_BOOTING, (), {})

    # ---- 先算警告（它们的成立与"能不能看到画面"无关）------------------ #
    flags: list = []
    vars = {"holder": values.get("holder", ""), "state_pct": P.state_pct(values),
            "core_count": P.core_count(values), "uptime_s": P.uptime_s(values),
            "bridge": values.get("bridge", ""), "readers": P.readers(values),
            "mice": P.mice(values)}
    # **数据闸**（`37-verify-findings.md` §4 现象 A）：一轮探针**一个键都没解析出来**时，
    # guest 侧的一切都无从谈起 —— 既不许当成平安，也**不许当成故障**。
    # 实测：rv1106 起机期 `probe_ok=ok / values={}` 能连报 12 轮（t=0~154.8 s），
    # 旧实现会给出一条 `touch/no-bridge` 假故障亮三分多钟，把"警告可见"本身搞砸。
    has_data = bool(values)
    pct, core = vars["state_pct"], vars["core_count"]
    cur_holder = P.holder_pid(values)
    cur_holder = cur_holder[0] if cur_holder else None
    bridges = tuple(sorted(P.bridge_pids(values)))
    next_bridge = bridges or sig.prev_bridge_pids
    next_holder = cur_holder if cur_holder is not None else sig.prev_holder_pid

    if sig.serial_state_error:                  # 宿主侧事实，与探针有没有值无关
        flags.append(Flag(FLAG_STATE_RO))
    if has_data:
        if (pct is not None and pct >= DEGRADE_STATE_PCT) or \
                (core is not None and core > DEGRADE_CORE):
            flags.append(Flag(FLAG_DISK_FULL, {"state_pct": pct, "core_count": core}))
        # Q70：**注册表里列着、盘上却没有文件**。这一档与"盘快满"互不遮蔽（F7 的拆两层就是
        # 为这个），也与画面无关 —— 有帧照样画，只是那几个应用点开会黑。
        # 只在 `apps` 采到时判（`app_adapt` 对 `apps=-` 回 None）：没这份注册表的包**不报错**。
        adapt = P.app_adapt(values)
        vars["apps"] = adapt
        if adapt and adapt[1] > 0:
            flags.append(Flag(FLAG_APP_MISSING, {"apps_total": adapt[0],
                                                 "apps_bad": adapt[1],
                                                 "apps_ids": adapt[2],
                                                 "apps_at": adapt[3]}))
        if sig.prev_bridge_pids is not None and bridges \
                and tuple(sorted(sig.prev_bridge_pids)) != bridges:
            flags.append(Flag(FLAG_BRIDGE_RESTARTED, {"bridge": values.get("bridge", "")}))

    # ---- 看不到画面：QMP 被别的实例占着。视图让给它，警告照留 ------ #
    if not sig.frames_ok and sig.other_owner:
        return Decision(VIEW_QMP_BUSY, tuple(flags),
                        dict(vars, owner=sig.other_owner), next_bridge, next_holder)

    if not has_data:
        # 没采到值：整轮**只留宿主侧事实**（帧拿不到是真的），其余一律不判
        if not sig.frames_ok:
            flags.append(Flag(FLAG_NO_FRAME))
        return Decision(VIEW_NONE, tuple(flags), vars, next_bridge, next_holder)

    # ---- 触摸三环 + "笔的界面还在起" ---------------------------------- #
    ui_starting = False
    if cur_holder is None:
        others = P.ui_readers(values)
        up = P.uptime_s(values)
        bs = P.bridge_state(values)
        if bs == "up" and not others and (up is None or up < UI_ATTACH_GRACE_S):
            # 实测两族的 marker/holder 先后**相反**（x6pro：holder 先；rv1106：marker 先），
            # 所以 `ui_started_hint` **不参与**这条判据，只用 600 s 兜底（#64/Q1）。
            ui_starting = True          # 实测：起机 33~185 s（rv1106 到 ~202 s）这段 holder 为空
        elif bs == "down" or (bs == "up" and "perl" not in P.readers(values)):
            flags.append(Flag(FLAG_TOUCH, _touch_vars(RING_NO_BRIDGE, values)))
        elif bs == "?":
            # 桥的状态**没采到**（`bridge_state({}) == "?"`）→ 不判 ——
            # 这正是现象 A 里"没采到被报成没在跑"的那半边，别再判成故障。
            pass
        elif not P.touches_available(values):
            flags.append(Flag(FLAG_TOUCH, _touch_vars(RING_NO_UINPUT, values)))
        else:
            flags.append(Flag(FLAG_TOUCH, _touch_vars(RING_NO_READER, values)))

    if ui_starting:
        flags.append(Flag(FLAG_UI_STARTING))
        # Q29：**有帧就不许擦画面** —— 判据住在 decide() 里，所以 fixtures 覆盖得到。
        view = VIEW_NONE if sig.frames_ok else VIEW_BOOTING
        return Decision(view, tuple(flags), vars, next_bridge, next_holder)

    if not sig.frames_ok:
        flags.append(Flag(FLAG_NO_FRAME))
    return Decision(VIEW_NONE, tuple(flags), vars, next_bridge, next_holder)


def _touch_vars(reason: str, values: dict) -> dict:
    return {"reason": reason, "tp": values.get("tp", ""),
            "holder": values.get("holder", ""), "readers": P.readers(values)}


def _view_vars(view: str, sig: Signals, chosen: dict) -> dict:
    if view == VIEW_QMP_BUSY:
        return {"owner": sig.other_owner}
    if view == VIEW_NO_PACK:
        if not sig.packs:
            return {"packs": []}
        return {"pack": sig.pack, "missing": list(chosen.get("missing") or []),
                "md5_bad": list(chosen.get("md5_bad") or []),
                "runtime_ok": chosen.get("runtime_ok", True),
                "runtime": chosen.get("runtime", "")}
    return {}
