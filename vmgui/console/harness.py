"""控制台与既有入口之间的桥：**复用** `scripts/dpctl.py`，不另造协议。

纪律（`vm/ui-council/31-trae-changeset.md` §2）：

* 读操作**进程内调用**（`pack_report` / `panel_size` / `vm_running` / `qmp_owner` /
  `port_open` / `push_file`），避免再起进程；写操作（`up` / `down`）起子进程跑
  **原样的** `dpctl` 命令行，参数以 argv 列表传递（不用 shell）。
* `up` **必须 `--display none`**（决策 #24）：控制台自己就是 4446 的客户端，
  再拉一个 vmgui 会撞 QEMU 的单客户端禁令（决策 #14）。
* 控制台把自己 pid 写进 `vm/console.pid`，`dpctl down` 认这个文件（决策 #24 / Q15 ④）。
"""
from __future__ import annotations

import importlib.util
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import tomllib
import wave
from typing import Optional


def _load_dpctl():
    """把 `scripts/dpctl.py` 当模块载入 —— 模块级只做 sys.path 插入 + import。"""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts")
    path = os.path.join(scripts, "dpctl.py")
    spec = importlib.util.spec_from_file_location("dictpen_dpctl", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


DPCTL = _load_dpctl()

ROOT = DPCTL.ROOT
VM = DPCTL.VM
SERIAL_LOG = DPCTL.SERIAL_LOG
CONSOLE_PID_FILE = DPCTL.CONSOLE_PID_FILE


# --------------------------------------------------------------------------- #
# 读：进程内
# --------------------------------------------------------------------------- #
_PACKS_CACHE = {"t": 0.0, "v": [], "known": False, "scanning": False,
                "t0": 0.0, "error": ""}
PACKS_TTL_S = 60.0               # `pack_report` 会按 [checks] 算镜像 MD5（很贵），必须缓存


def packs(refresh: bool = False) -> list:
    """缓存的包体检报告。**永不阻塞调用方**。

    首次扫描会算镜像的 MD5（x6pro 的 rootfs 是 GB 级，要几十秒），**绝不能挂在
    5 s 一轮的状态栏探针上** —— 实测过一次：探针线程被它堵住，12 s 里一条判定都没送到。
    所以首次扫描丢到后台线程，调用方先拿到空列表；`packs_known()` 为 False 时上层
    **不许**把空列表当成"没有固件包"。

    ``refresh=True``（空态 #1 的「重新扫描」）才同步扫。
    """
    now = time.time()
    if refresh:
        _scan_packs(blocking=True)
    elif not _PACKS_CACHE["known"]:
        _scan_packs(blocking=False)
    elif now - _PACKS_CACHE["t"] > PACKS_TTL_S:
        _scan_packs(blocking=False)
    return _PACKS_CACHE["v"]


def packs_known() -> bool:
    """包清单到底拿到没有（False = 还在后台扫 / 扫失败，别下"没有固件"的结论）。"""
    return _PACKS_CACHE["known"]


def packs_state() -> str:
    """这一格的态：``fresh``（有清单）/ ``scanning``（在读）/ ``failed``（读失败）。

    #52 要求分三段开口，靠它驱动；**failed 必须说出来** —— 否则"`known` 永为 False
    且每轮重试"就变成永久静默，用户只看到「启动」灰着、没有理由。
    """
    if _PACKS_CACHE["error"]:
        return "failed"
    return "fresh" if _PACKS_CACHE["known"] else "scanning"


def packs_error() -> str:
    return _PACKS_CACHE["error"]


def packs_scan_age() -> float:
    """这一轮扫描已经开始多久（秒）。"""
    t0 = _PACKS_CACHE["t0"]
    return (time.time() - t0) if t0 else 0.0


def _scan_packs(blocking: bool) -> None:
    if _PACKS_CACHE["scanning"]:
        return
    _PACKS_CACHE["scanning"] = True
    _PACKS_CACHE["t0"] = time.time()
    # 第一趟只查存在性（快，状态栏的 runtime/面板 与空态 #1 的"缺文件"立刻可用）；
    # 之后的刷新才核 MD5（贵，要读完整份镜像）—— 否则前 1 分钟只能显示 "-"。
    verify = bool(_PACKS_CACHE["known"])

    def work():
        try:
            _PACKS_CACHE["v"] = DPCTL.pack_report(verify=verify)
            _PACKS_CACHE["error"] = ""
            _PACKS_CACHE["t"] = time.time()
            _PACKS_CACHE["known"] = True
        except Exception as e:                    # 失败**不置** known，但要让上层看得见
            _PACKS_CACHE["error"] = str(e)
        finally:
            _PACKS_CACHE["scanning"] = False

    if blocking:
        work()
    else:
        threading.Thread(target=work, name="packs-scan", daemon=True).start()


def rescan() -> list:
    _scan_packs(blocking=True)
    return _PACKS_CACHE["v"]


def panel(name: Optional[str] = None) -> tuple[int, int]:
    return DPCTL.panel_size(name)


def swap_rb(name: Optional[str] = None) -> bool:
    """这台机型的帧要不要交换 R/B（a6pro/a7pro 的 falcon 输出与扫描线相反）。

    控制台换目标时要重新问（`ConsoleWindow._apply_pack`）——帧是逐帧取的，
    按启动那一刻的包定死这个标志，从 a 系列切走或切进来的整屏颜色都是反的。
    """
    return DPCTL.swap_rb_for(name or current_pack())


def current_pack() -> Optional[str]:
    return DPCTL.current_pack_name()


_KEY_CODES = {"HOME_CODE": "home", "VOL_DOWN_CODE": "vol_down", "VOL_UP_CODE": "vol_up"}


def volume_card(pack: Optional[str]) -> int:
    """这台的音量落在哪张声卡（声音页读写它；没声明的包按 card0）。

    厂商 HAL 按 `youdao_hal_audio_config.json` 里那颗 `*_vol_ctl_numid` 往**指定卡**写值，
    而 VM 里每块 QEMU usb-audio 都有同名控件，盯错卡就是"滑杆动了、值不动"。s7pro 的输出
    设备是 card2（`/tmp/headSetStatus=1@@`，HAL 认为数字耳机插着），不是 `speaker_card_id`。
    声明超出 `[audio].cards` 时按最大合法卡号收（配置写错不该往不存在的卡上发命令）。
    """
    if not pack:
        return 0
    path = os.path.join(ROOT, "packs", str(pack), "pack.toml")
    try:
        with open(path, "rb") as f:
            audio = (tomllib.load(f).get("audio", {}) or {})
    except (OSError, tomllib.TOMLDecodeError):
        return 0
    try:
        cards = max(1, int(audio.get("cards") or 1))
        card = int(audio.get("volume_card") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(card, cards - 1))


def keys_for(pack: Optional[str]) -> dict:
    """这台固件能发的实体键（语义名 → input code）。无配置的包（x7）返回全 None。

    配置就在包的 `patches/vm-keys.env`：客机 S21vm-keys 读它起 uinput 替身，
    控制台读同一份决定哪几颗键亮、按下去发什么码。s6pro/x5pro 的 Home 是 139，
    其余有键的机型是 102；音量键只有 x6pro 有。
    """
    out = {"home": None, "vol_down": None, "vol_up": None}
    if not pack:
        return out
    path = os.path.join(ROOT, "packs", str(pack), "patches", "vm-keys.env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                name = _KEY_CODES.get(key.strip())
                val = val.strip().strip("'\"")
                if name and val.isdigit():
                    out[name] = int(val)
    except OSError:
        pass
    return out


def vm_running() -> bool:
    return DPCTL.vm_running()


def qmp_owner(exclude_pid: Optional[int] = None) -> Optional[str]:
    return DPCTL.qmp_owner(exclude_pid)


def ports() -> dict:
    return {name: DPCTL.port_open(port) for name, port in (
        ("ssh", DPCTL.SSH_PORT), ("adb", DPCTL.ADB_HOST_PORT),
        ("serial", DPCTL.SERIAL_PORT), ("bt", DPCTL.BT_PORT),
        ("qmp", DPCTL.QMP_PORT))}


def serial_tail(lines: int = 200, max_bytes: int = 1 << 18) -> str:
    """宿主侧串口日志尾部 —— **不需要 VM 在跑**（决策 #7 的唯一无通道出口）。

    两处加固：

    * 从文件**末尾** seek 读，不 ``readlines()`` 整个文件（开久了日志很大，5 s 一轮
      每轮都重读一遍）；
    * 只取**最后一次开机**那一段。实测（F6，2026-10-18）QEMU 这个 `logfile=` 是
      **截断**写的：`down` 前 543 行 → `up` 后 449 行，`Booting Linux` 恒为 1。
      这条结论一旦变了（有人加轮转/改成 append），空态 #3 会被**上一次**开机的只读
      故障永久钉住 —— 所以按段切一次更稳，代价只是一次 `rfind`。
    """
    seg = _serial_boot_slice(max_bytes)
    return "".join(seg.splitlines(keepends=True)[-lines:])


def _serial_tail_bytes(start: int, size: int) -> str:
    """从 ``start`` 读到文件末尾（最多一块），丢掉被切掉的半行。"""
    try:
        with open(SERIAL_LOG, "rb") as f:
            f.seek(start)
            raw = f.read(size - start)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if start > 0:
        text = text.split("\n", 1)[-1]        # 丢掉被切掉的首行
    return text


BOOT_MARKER = "Booting Linux on physical"

# 段起始偏移的小缓存：一旦在某个 size 下找到了标记位置，之后只要文件没缩就直接复用。
# 为什么要缓存：a6pro 的 serial.log 已长到 **1.8 MB**，而标记留在最前面 —— 每秒重扫全文件
# 只为找它一次，代价白白花在串口尾巴那个 1 s 刷新上。
_BOOT_SEG = {"offset": -1, "size": 0}


def _boot_segment_start(size: int) -> int:
    """本次开机那一段在文件里的起始偏移；**全文件都没有标记时返回 -1**。

    返回 -1 的语义是"本次开机还没开始"（Q38），调用方必须**返回空串**，
    绝不能退回整个 tail —— 那会把上一次开机的旧 needle 放行。
    """
    if 0 <= _BOOT_SEG["offset"] <= size and _BOOT_SEG["size"] <= size:
        return _BOOT_SEG["offset"]                  # 文件没缩 → 缓存还能用
    chunk = 1 << 20
    try:
        with open(SERIAL_LOG, "rb") as f:
            pos = size
            while pos > 0:
                start = max(0, pos - chunk)
                f.seek(start)
                buf = f.read(pos - start)
                idx = buf.rfind(BOOT_MARKER.encode())
                if idx >= 0:
                    off = start + idx
                    _BOOT_SEG.update(offset=off, size=size)
                    return off
                pos = start
    except OSError:
        return -1
    _BOOT_SEG.update(offset=-1, size=size)
    return -1


def _serial_boot_slice(max_bytes: int = 1 << 18) -> str:
    """只看**本次开机**那一段（从最后一次 `Booting Linux on physical` 起）。

    `serial.log` 是**截断**写的（F6 实测），而截断发生在 QEMU 打开 chardev 时、比
    `start_vm()` 返回更晚 —— `up` 刚交接那一瞬文件里还是上一次的内容。所以"找不到标记"
    必须返回**空串**，不然会放行上一次开机的旧 needle（Q38）。

    ⚠ **但"找不到"要分两种**（Q48b，真机撞到）：标记在文件**更前面**（日志长到超过窗口了，
    a6pro 实测 1.8 MB）≠ 本次开机没开始。前者要给内容，后者才给空串 —— 旧写法一律返回
    空串，于是**串口页与空态正文整段变空**（这条是取证包带出来的：它报"串口尾缺失"）。
    """
    try:
        size = os.path.getsize(SERIAL_LOG)
    except OSError:
        return ""
    if size <= 0:
        return ""
    off = _boot_segment_start(size)
    if off < 0:
        return ""                                   # 本次开机还没开始（Q38 的语义）
    # 段首在窗口之外（日志长过窗口）→ 只给尾部窗口；那一段的内容照样是本次开机的
    start = max(off, size - max_bytes)
    return _serial_tail_bytes(start, size)


def serial_state_unavailable() -> bool:
    """串口里有没有 `### state: unavailable` / 只读文件系统（空态 #3 的判据）。"""
    seg = _serial_boot_slice(1 << 20)
    return "### state: unavailable" in seg or "Read-only file system" in seg


# 结尾那个 `!` **必须有**：少了它，客机启动时那行
# `...Waiting miniapp start finished ` 会被逐字节命中 —— 实测（rv1106）假信号在
# `serial.log:527`、真信号在 `:597`，**早了 70 行**，于是 hint 翻 True 比 `holder` 还早，
# 方向正好反了，会报 `no-reader` 假故障（`37-verify-findings.md` §5 B(b)）。
UI_UP_MARKER = "miniapp start finished!"


def ui_started_hint() -> bool:
    """本次开机里笔的界面是否已经起来（串口里见到 `miniapp start finished!`）。

    **当前只作信息位，不参与判据** —— 实测两族的先后正好相反（`37-…` V1/V5 + V7）：

    * x6pro：`holder` **33.1 s** 出现，marker **53.3 s** 才出现（holder 先）；
    * a7pro：marker 落在 **(172.7, 181.8]**、`holder` 落在 **(181.8, 202.4]** —— **marker 先**，
      领先上界约 20 s。

    所以"看见＝已起"在 rv1106 上是**假的**，用它结束 defer 窗口就会报 `no-reader` 假故障。
    在拿到一条两族一致的硬信号之前，`ui-starting` 只按 `UI_ATTACH_GRACE_S` 兜底。
    不需要任何 guest 通道（#7 之后唯一的无通道出口）。
    """
    return UI_UP_MARKER in _serial_boot_slice(1 << 20)


# --------------------------------------------------------------------------- #
# 面板声明 -> 裁剪线（#27 / #35 / `33-crop-and-coordinates.md`）
# --------------------------------------------------------------------------- #
def crop_for(pw: int, ph: int, gw: int = 0, gh: int = 0) -> dict:
    """把包声明的面板尺寸变成 `display.crop` 声明。**原点恒 0**（`33-…` §0.2）。

    ``gw/gh = 0`` 表示还不知道帧（首帧之前）→ 先按面板声明写，首帧到了再复核一次：
    这样先开出来的窗口尺寸与首帧到达后一致，不会在首帧那一刻跳一下（`32-…` §2）。

    面板 ≥ 帧 → **不裁**（`33-…` §3.2）：声明过期或换包没重启时，提示行要写"按帧"，
    而不是假装裁过。
    """
    pw, ph, gw, gh = int(pw), int(ph), int(gw), int(gh)
    off = {"enabled": False, "x": 0, "y": 0, "w": 0, "h": 0}
    if pw <= 0 or ph <= 0:
        return off
    if gw and gh and pw >= gw and ph >= gh:
        return off
    return {"enabled": True, "x": 0, "y": 0, "w": pw, "h": ph}


# --------------------------------------------------------------------------- #
# 记账
# --------------------------------------------------------------------------- #
def claim_console_pid() -> None:
    os.makedirs(VM, exist_ok=True)
    with open(CONSOLE_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_console_pid() -> None:
    try:
        if DPCTL.console_pid() == os.getpid():
            os.remove(CONSOLE_PID_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 写：子进程跑原样的 dpctl 命令行
# --------------------------------------------------------------------------- #
def _argv(args: list) -> list:
    # `-X utf8` 是**必须的**，不是讲究：子进程默认按宿主 ANSI 码页（这台是 cp936）编码自己的
    # stdout，而 `run_dpctl` 已经按 **utf-8** 去解它。实测（a7pro，2026-10-19）
    # `dpctl sh --serial` 因为串口回显里带 UTF-8 中文，子进程在
    # `sys.stdout.write(out)` 那行抛 `UnicodeEncodeError: 'gbk' codec can't encode
    # character '\ufffd'` → **rc=1、一个字节都不回**，控制台「命令」页于是显示一段
    # traceback 而不是命令结果。任何输出里含非 GBK 字符的命令都会中这一枪。
    return ([sys.executable, "-X", "utf8", "-u",
             os.path.join(ROOT, "scripts", "dpctl.py")] + list(args))


def run_dpctl(args: list, timeout: int = 300) -> tuple[int, str]:
    """跑一条 dpctl 子命令，返回 (rc, 合并输出)。不用 shell：参数原样传递。"""
    log = os.path.join(VM, "console-dpctl.log")
    try:
        p = subprocess.run(_argv(args), cwd=ROOT, capture_output=True, timeout=timeout)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"dpctl {' '.join(args)} 超时（{timeout}s）"
    except OSError as e:
        return 1, f"启动 dpctl 失败: {e}"
    try:
        with open(log, "ab") as f:
            f.write(("\n$ dpctl " + " ".join(args) + "\n" + out).encode())
    except OSError:
        pass
    return p.returncode, out


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


class GuestJob:
    """一条可中断的 dpctl 命令。

    声音页的 `aplay` 在声卡忙或后端卡住时不会返回；`run_dpctl` 用
    `subprocess.run` 拿不到句柄，只能等超时。这里保留 Popen，选中另一个操作时
    直接杀掉整棵进程树，而不是把人挡在“上一条命令尚未返回”上。
    """

    def __init__(self, args: list, timeout: int = 300) -> None:
        self.args = list(args)
        self.timeout = timeout
        self._proc = None
        self._lock = threading.Lock()
        self.canceled = threading.Event()

    def cancel(self) -> None:
        self.canceled.set()
        with self._lock:
            p = self._proc
        if p is not None and p.poll() is None:
            _kill_tree(p.pid)

    def run(self) -> tuple[int, str]:
        with self._lock:
            if self.canceled.is_set():
                return 130, "已中断"
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen(_argv(self.args), cwd=ROOT,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 **kw)
            self._proc = p
        if self.canceled.is_set():
            _kill_tree(p.pid)
        try:
            data, _ = p.communicate(timeout=self.timeout)
            rc, out = p.returncode, data.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            _kill_tree(p.pid)
            data, _ = p.communicate()
            rc, out = 124, (data or b"").decode("utf-8", "replace")
            out += f"\ndpctl {' '.join(self.args)} 超时（{self.timeout}s）"
        except OSError as e:
            rc, out = 1, f"启动 dpctl 失败: {e}"
        if self.canceled.is_set():
            rc = 130
            out = (out or "") + "\n已中断"
        try:
            with open(os.path.join(VM, "console-dpctl.log"), "ab") as f:
                f.write(("\n$ dpctl " + " ".join(self.args) + "\n" + out).encode())
        except OSError:
            pass
        return rc, out


def ensure_tone(path: Optional[str] = None, seconds: float = 1.0,
                freq: float = 440.0, rate: int = 44100) -> str:
    """测试音；发布包里没有 `vm/`，所以不能依赖预先放好的 WAV。"""
    path = path or os.path.join(VM, "_tone.wav")
    if os.path.isfile(path) and os.path.getsize(path) > 44:
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(rate * seconds)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def start_vm(pack: Optional[str], fresh: bool = False,
             rescue: bool = False) -> tuple[int, str]:
    """起 VM。**必须 `--display none`** —— 控制台自己占 QMP（决策 #24）。"""
    args = ["up", "--display", "none"]
    if pack:
        args += ["--pack", pack]
    if fresh:
        args.append("--fresh")
    if rescue:
        args.append("--rescue")
    return run_dpctl(args, timeout=600)


def fast_status(pack: Optional[str]) -> dict:
    """快速启动快照的轻量状态（只读 vm/fast-<包>.json，不碰 QMP/大文件）。"""
    if not pack:
        return {"ready": False}
    try:
        return DPCTL.fast_status(str(pack))
    except Exception:
        return {"ready": False}


def start_vm_fast(pack: Optional[str]) -> tuple[int, str]:
    """从首页快照恢复。快照盘是独立的 qcow2，里面没有音频。"""
    args = ["up", "--display", "none", "--fast"]
    if pack:
        args += ["--pack", pack]
    return run_dpctl(args, timeout=180)


def save_fast(pack: Optional[str]) -> tuple[int, str]:
    """先关机，再把冷启动状态盘复制成快照盘并 savevm（约 1.5 分钟）。"""
    args = ["fast", "save", "--force"]
    if pack:
        args += ["--pack", pack]
    return run_dpctl(args, timeout=900)


def stop_vm(force: bool = False) -> tuple[int, str]:
    """关机。``force=True`` 是「立即断电」—— 不请 guest 关机，直接收进程（#71）。"""
    args = ["down"] + (["--force"] if force else [])
    return run_dpctl(args, timeout=90)


def state_image(pack: Optional[str]) -> tuple[str, int]:
    """这一台的状态盘：``(路径, 字节数)``；还没有那份文件时字节数为 0。

    重建（`--fresh`）的确认弹窗**必须**写出盘名与大小（决策 #15/#71）—— 不可逆操作的
    文案要能让人**认得出自己丢的是什么**，所以这两个数从真文件读，不许写"清除数据"这种抽象词。
    """
    name = DPCTL.current_pack_name() if not pack else pack
    if not name:
        return "", 0
    path = DPCTL.state_img_for(name)
    try:
        return path, os.path.getsize(path)
    except OSError:
        return path, 0


def format_size(n: int) -> str:
    """字节 → 人读得懂的一行（MiB / GiB）。弹窗与日志共用同一份口径。"""
    if n <= 0:
        return "还没有状态盘"
    mib = n / (1024 * 1024)
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib:.0f} MiB"


def backup_state_image(pack: Optional[str]) -> tuple[str, str]:
    """重建前**先留档**（决策 #15"先导出再重建"）：复制成 ``<盘>.bak``。

    返回 ``(备份路径, 说明)``；没有源文件就返回空路径（没什么可备份的）。
    """
    src, size = state_image(pack)
    if not src or not size:
        return "", "没有状态盘可留档（首次启动会新建一份）"
    dst = src + ".bak"
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        return "", f"留档失败：{e}"
    return dst, f"已留档 {os.path.basename(dst)}（{format_size(size)}）"


def start_vm_fresh(pack: Optional[str]) -> tuple[int, str]:
    """重建状态盘并起机：先留档，再 `up --fresh`（决策 #15/#71）。"""
    _, note = backup_state_image(pack)
    rc, out = start_vm(pack, fresh=True)
    return rc, (note + "\n" + out)


def restart_vm(pack: Optional[str]) -> list:
    """重启 = **看得见两步**（决策 #71）：先按一次「关机」，再按同包「启动」。

    为什么不是一条命令中间不暴露：`33~185 s` 的起机期正是状态栏最有信息量的一段
    （判据/警告/触摸灯都在那段时间变化），藏起来等于把最有用的读数抹掉。
    """
    steps = [("关机", ["down"])]
    up = ["up", "--display", "none"]
    if pack:
        up += ["--pack", pack]
    steps.append(("启动", up))
    done = []
    for label, argv in steps:
        rc, out = run_dpctl(argv, timeout=600)
        done.append((label, rc, out))
        if rc != 0:                     # 关机失败就别接着起，免得起两个
            break
    return done


def put(local: str, remote: str) -> int:
    """进程内上传 —— 不经 shell（Git-Bash 会把 guest 路径转成宿主路径）。"""
    return DPCTL.push_file(local, remote)[0]


def get(remote: str, local: str) -> int:
    """进程内下载（控制台此前**只能传进去、不能掏出来**，这是补上的那一半）。

    通道是 sftp（`dpctl.get_file`）：文件名带空格也照原样，二进制安全。
    """
    return DPCTL.get_file(remote, local)[0]


def free_space(path: str = "/userdata") -> Optional[int]:
    """guest 上某个目录还剩多少字节；取不到返回 None（**不许**编一个 0 出来）。

    为什么界面必须显示它（千问 `42` §2.2）：`/userdata` **就是状态盘**，一边报"状态盘快满"
    一边开一条往里堆的通道，等于自己拆自己的报警。实测（a6pro）：`df -h /userdata` →
    `487.2M 用 95.9M 剩 381.0M 20%`。
    """
    rc, out = DPCTL.guest_run(f"df -k {DPCTL._shq(path)}", timeout=20)
    if rc != 0:
        return None
    for line in reversed(out.splitlines()):
        parts = line.split()
        if len(parts) >= 4 and parts[0] != "Filesystem":
            try:
                return int(parts[3]) * 1024            # df -k 的 Available 是 1K 块
            except ValueError:
                continue
    return None


def backup_remote(remote: str) -> str:
    """覆盖前先留档：把 guest 上的目标复制成 `<名字>.bak-<时间戳>`，返回说明串。

    `put` 默认覆盖，而这里被覆盖的可能是设备身份（`system_info.bin`）或桩库（`.so`）——
    现场丢了就回不去（千问 `42` §2.4）。目标不存在时返回空串（没什么可备份的）。
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    cmd = (f"if [ -e {DPCTL._shq(remote)} ]; then "
           f"cp -p {DPCTL._shq(remote)} {DPCTL._shq(remote + '.bak-' + ts)} && "
           f"echo 'backup: '{DPCTL._shq(remote + '.bak-' + ts)}; fi")
    rc, out = DPCTL.guest_run(cmd, timeout=20)
    if rc != 0 or "backup:" not in out:
        return ""
    note = out.split("backup:", 1)[1].strip().splitlines()[0]
    return f"已留档 {note}"


MINIAPP_INSTALL_BIN = "miniapp_cli"
MINIAPP_INSTALL_TIMEOUT_S = 180      # 装一个包要解包，比探针/留档那几条给得宽


def install_miniapp(remote: str,
                    timeout: int = MINIAPP_INSTALL_TIMEOUT_S) -> tuple[int, str, str]:
    """在笔里跑 `miniapp_cli install <落点>`。返回 `(rc, 发出去的那条命令, 合并输出)`。

    三条要记的：
    * **路径必须引**：`/userdata` 允许中文名/带空格（`put` 那条通道本来就是这个前提），
      不引就会当成两个参数 —— 用的是 `dpctl` 里那把 `_shq`（busybox 的教训见它的注释）。
    * **命令要原样回传**：这是改设备状态的动作，界面上只印一个 `rc` 而不印"到底发了什么"，
      出了问题就无从对账。
    * `miniapp_cli` **在**：2026-10-19 a6pro 实测 `command -v miniapp_cli` →
      `/usr/bin/miniapp_cli`，`miniapp_cli --help` 的用法表里就写着 `install {amrPath}`
      （同表还有 `uninstall {appid}` / `start {appId} --{page}` / `capture {path}`）。
      改这里之前全仓 grep 是零命中，所以**这条是刚补的实测**，不是推断。

    回落行为归 `dpctl.guest_run`：SSH 优先，未起来时走串口（`dpctl.py:286`）。串口那条
    通道上 `sh` 的回执可能混着历史控制台日志，所以**输出只当线索，不当结论**。
    """
    cmd = f"{MINIAPP_INSTALL_BIN} install {DPCTL._shq(remote)}"
    rc, out = DPCTL.guest_run(cmd, timeout=timeout)
    return rc, cmd, out


# --------------------------------------------------------------------------- #
# 「配置」页的后端：机器参数（内存 / vCPU）与状态盘
# --------------------------------------------------------------------------- #
GUEST_STATE_DEV = "/dev/vdb"
GUEST_STATE_MOUNT = "/state"


def machine_next(pack: Optional[str]):
    """下次起机会用的 `(内存, vCPU, 来源, machine.toml 错误, 包名)` —— **全问 dpctl**。

    控制台不复算：默认值住在 runtime，覆盖住在 `vm/machine.toml`，在这儿再算一遍就是
    第二个真相（Q49/Q62 那一族的定义性症状）。
    """
    return DPCTL.machine_plan_for(pack)


_KEEP = object()


def set_machine(pack: Optional[str], memory=_KEEP, smp=_KEEP):
    """改 `vm/machine.toml` 的**一格**。返回 `(生效值, 错误串)`；坏值不写（校验在 dpctl）。

    没点名的键一律**不转发**：在 dpctl 那一侧 `None` / `""` 的意思是"把这个键删掉"。
    早先无条件写 `memory=memory, smp=smp`，于是按「改 vCPU」连带把**内存那格的覆盖抹了**
    —— 界面上"只改这一格，另一格不动"当场变成假话。实测见 `vm/_qwen_machine_write.py`：
    先 `memory=3G` 再 `smp=2`，第二次之后文件里只剩 `smp = 2`。
    """
    kv = {}
    if memory is not _KEEP:
        kv["memory"] = memory
    if smp is not _KEEP:
        kv["smp"] = smp
    return DPCTL.set_machine_override(pack, **kv)


def guest_machine() -> Optional[dict]:
    """跑着的那台**实际**看到多少内存、几个 CPU；没在跑返回 None（**不许**回 0 顶）。

    ⚠ 口径：guest 的 `MemTotal` **永远小于** `-m` —— 内核与保留页要吃掉一截。实测 a6pro
    `-m 2G` → `MemTotal: 2029188 kB` ≈ 1.93 GiB。所以那一格的名字是**客机看到**，不是
    "当前内存"：把 1.93 与 2 并排写成两个"当前值"，读起来像是被人改小了。
    """
    rc, out = DPCTL.guest_run("grep MemTotal /proc/meminfo; "
                              "grep -c ^processor /proc/cpuinfo", timeout=20)
    if rc != 0:
        return None
    mem = cpu = 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("MemTotal:"):
            digits = "".join(c for c in line if c.isdigit())
            mem = int(digits) // 1024 if digits else 0        # kB → MiB
        elif line.isdigit():
            cpu = int(line)
    return {"mem_mib": mem, "cpu": cpu} if mem else None


def state_mib(pack: Optional[str]) -> int:
    """状态盘**文件**多大（MiB）；没有那份文件 → 0。"""
    _, size = state_image(pack)
    return size // (1024 * 1024)


def guest_fs_mib(path: str = GUEST_STATE_MOUNT) -> Optional[int]:
    """客机里那个文件系统**看到**多大（MiB）；取不到 None。

    与 `state_mib` 是两个数：`qemu-img resize` 只改宿主文件，ext4 还停在旧大小 ——
    两个读数并排放，"扩了但用不上"才看得见。
    """
    rc, out = DPCTL.guest_run(f"df -k {DPCTL._shq(path)}", timeout=20)
    if rc != 0:
        return None
    for line in reversed(out.strip().splitlines()):
        cols = line.split()
        if len(cols) >= 2 and cols[1].isdigit():
            return int(cols[1]) // 1024                       # 1K-blocks → MiB
    return None


def grow_state(pack: Optional[str], want_mib: int) -> tuple[int, str]:
    """扩状态盘（只增不减、VM 停着才做）。边界与理由都在 `dpctl.grow_state_image`。"""
    return DPCTL.grow_state_image(pack or DPCTL.current_pack_name(), want_mib)


def guest_grow_fs() -> tuple[int, str]:
    """让客机用上多出来的空间：在线 `resize2fs /dev/vdb`（**要 VM 在跑**）。

    实测 a6pro：`/state` 是整盘 ext4（`/dev/vdb` 无分区表）、rw 挂载 → 在线扩可行；
    `resize2fs` 在 `/sbin`（busybox 那套里没有 `fsck.ext4`/`losetup`，别照着宿主习惯猜）。
    """
    rc, out = DPCTL.guest_run(f"resize2fs {GUEST_STATE_DEV}", timeout=60)
    return rc, (out.strip() or "（guest 一个字都没回）")



BUNDLE_SERIAL_LINES = 400


def bundle_dir(pack: Optional[str]) -> str:
    """取证包的默认落点（带时间戳，不会互相覆盖）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(VM, f"bundle-{pack or 'nopack'}-{ts}")


def make_bundle(pack: Optional[str], dest_dir: Optional[str] = None, *,
                probe_values: Optional[dict] = None, probe_ok: str = "",
                serial_lines: int = BUNDLE_SERIAL_LINES) -> tuple:
    """把"出问题时要交给对方"的东西收成一个目录（千问 `42` §1 ③，**纯读**）。

    四件 + 一份说明：

    * `serial-tail.log` —— 串口尾 N 行（**不依赖 VM 通道**，决策 #7 那条出口）；
    * `probe.txt` —— 最后一轮探针的 `key=value`（由调用方传进来，**不另起一轮 SSH**）；
    * `screen.png` —— 最近一次存图（还没存过就记成"缺失"，**不编一个空文件**）；
    * `pack.toml` —— 当前包的声明（只读副本）；
    * `README.txt` —— 来源、时间、这台在不在跑、清单与缺失项。

    返回 ``(目录, 写入的文件名列表, 缺失项列表)``。
    """
    pack = pack or current_pack()
    dest = dest_dir or bundle_dir(pack)
    os.makedirs(dest, exist_ok=True)
    written, missing = [], []

    tail = serial_tail(serial_lines)
    if tail.strip():
        with open(os.path.join(dest, "serial-tail.log"), "w", encoding="utf-8") as f:
            f.write(tail)
        written.append("serial-tail.log")
    else:
        missing.append("串口尾（vm\\serial.log 现在没有内容）")

    if probe_values is not None:
        with open(os.path.join(dest, "probe.txt"), "w", encoding="utf-8") as f:
            f.write(f"# 通道={probe_ok or '既不是 ssh 也不是 serial'}\n")
            for k in sorted(probe_values):
                f.write(f"{k}={probe_values[k]}\n")
        written.append("probe.txt")
    else:
        missing.append("探针 values（这一轮没采到）")

    shot = os.path.join(VM, "screen.png")
    if os.path.exists(shot):
        shutil.copyfile(shot, os.path.join(dest, "screen.png"))
        written.append("screen.png")
    else:
        missing.append("最近存图（还没按过「存图」）")

    toml = os.path.join(ROOT, "packs", pack, "pack.toml") if pack else ""
    if toml and os.path.exists(toml):
        shutil.copyfile(toml, os.path.join(dest, "pack.toml"))
        written.append("pack.toml")
    else:
        missing.append(f"包声明（packs/{pack or '?'}/pack.toml 不在）")

    written.append("README.txt")                # 它自己也算一件（清单要如实）
    with open(os.path.join(dest, "README.txt"), "w", encoding="utf-8") as f:
        f.write("词典笔控制台 · 取证包\n")
        f.write(f"包：{pack or '（还不知道是哪个包）'}\n")
        f.write(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"VM：{'在跑' if vm_running() else '没在跑'}\n")
        f.write(f"串口行数上限：{serial_lines}\n\n")
        f.write("清单：\n" + "".join(f"  {n}\n" for n in written))
        f.write("\n缺失：\n" + ("".join(f"  {n}\n" for n in missing) if missing else "  （无）\n"))
        f.write("\n这份包只有只读信息：串口尾、探针读数、最近一张存图、包声明。\n")
        f.write("不包含设备身份（sn/uuid 只留尾号）、不包含 license、不包含状态盘。\n")
    return dest, written, missing


# --------------------------------------------------------------------------- #
# 探针传输：SSH 优先，未就绪回落串口（与 dpctl.guest_run 同序），并把通道报回
# --------------------------------------------------------------------------- #
def probe_transport(cmd: str, timeout: int = 20) -> tuple[int, str, str]:
    if not vm_running():
        return 1, "VM is not running", "none"
    try:
        p = subprocess.run(DPCTL.ssh_base() + [cmd], capture_output=True, timeout=timeout)
        err = p.stderr.decode("utf-8", "replace")
        if p.returncode != 255 or not any(e in err for e in DPCTL.SSH_TRANSPORT_ERRORS):
            return p.returncode, p.stdout.decode("utf-8", "replace"), "ssh"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    rc, out = DPCTL.guest_run(cmd, timeout=timeout, force_serial=True)
    if "no transport available" in (out or ""):
        return 1, out, "none"
    return rc, out, "serial"
