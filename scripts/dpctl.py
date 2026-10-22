#!/usr/bin/env python3
"""dpctl - development harness for the dictpen VM (a framework, not a machine).

Drives the QEMU virtual dictionary pen so it is usable as a development
target: boot it with a GUI, transfer files, run commands inside the pen's
shell, and debug it from the PC with adb.

The VM is a **framework**: it contains no firmware of its own.  Everything
that varies per device - the images, the panel size, the display HAL, the
boot hooks - lives in a detachable **firmware pack** under `packs/<name>/`,
and you pick one at boot time.

  dpctl up --pack NAME     start the VM from packs/<NAME>/ (persistent state
                           disk, per pack).  With a single pack installed the
                           flag may be omitted.  The display goes to the vmgui
                           client by default (QEMU itself runs -display none);
                           --display gtk falls back to QEMU's own window, none
                           starts without one.  --rescue boots straight to a
                           root shell (repairs), --no-license skips the
                           activation restore
  dpctl packs [NAME]       list firmware packs (runtime / panel / image state);
                           with NAME: show every resolved path
  dpctl pack export NAME   write packs/<NAME>/ to one .dpm file
  dpctl pack import FILE   install a pack from a .dpm file
  dpctl wait --adb         wait until the guest is usable
  dpctl sh -c "uname -a"   run a command inside the pen
  dpctl shell              interactive shell inside the pen
  dpctl put  FILE DEST     copy a file to the pen
  dpctl get  SRC  FILE     copy a file from the pen
  dpctl adb shell          run adb against the pen (adb connect done for you)
  dpctl adb push a b       any adb subcommand is passed through
  dpctl view               live window showing the pen's GUI (its own resolution)
  dpctl pointer            touch-feedback marker over the QEMU window (Windows)
  dpctl nopointer          take that overlay back down
  dpctl license            restore the activation license (see README)
  dpctl screenshot         save the pen's GUI to vm/screen.png
  dpctl tap X Y            click at (X, Y) on the pen's screen (touch injection)
  dpctl status             show VM / port / adb state
  dpctl down               stop the VM

Only the Python standard library is used.  `adb` is optional: it is looked up
as tools/platform-tools/adb.exe, then on PATH; everything except `dpctl adb`
also works over plain SSH.
"""
import argparse
import base64
import json
import os
import posixpath
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packlib                                   # noqa: E402  固件包（packs/）
import runtimes                                  # noqa: E402  可插拔 boot backend
from packlib import PackError                    # noqa: E402
from runtimes import BootContext, UnsupportedPlatform   # noqa: E402

QEMU = os.path.join(ROOT, "tools", "msys2", "ucrt64", "bin", "qemu-system-aarch64.exe")
QEMU_IMG = os.path.join(ROOT, "tools", "msys2", "ucrt64", "bin", "qemu-img.exe")
KEY = os.path.join(ROOT, "tools", "ssh", "id_devvm")
# 宿主 sftp：`put`/`get` 的首选通道（路径带空格/中文也安全，二进制安全）。
# 客机侧 serve 它的是 dropbear + /usr/libexec/sftp-server（千问在 a6pro 上实测读写都通）；
# ⚠ 其余六包有没有 sftp-server 是**包侧事实**，没测过的不许外推。
SFTP = shutil.which("sftp") or r"C:\Windows\System32\OpenSSH\sftp.exe"

VM = os.path.join(ROOT, "vm")
PID_FILE = os.path.join(VM, "qemu.pid")
QEMU_LOG = os.path.join(VM, "qemu.log")
SERIAL_LOG = os.path.join(VM, "serial.log")
CURRENT_PACK_FILE = os.path.join(VM, "current-pack")   # 当前在跑的包名（截屏/点击用）

SSH_PORT = 2222
SERIAL_PORT = 4444
MONITOR_PORT = 4445
QMP_PORT = 4446
ADB_HOST_PORT = 15037     # outside adb's emulator probe range (5554-5585)
ADB_GUEST_PORT = 5555
BT_PORT = 4447            # guest HCI link (hci_uart) <-> scripts/bt-hci-emu.py
# VNC 推流口（决策 #72 / Q41）：只给"画面"用，QMP 4446 照旧独占给控制台。
# 千问 `netstat` 实测 4448 空闲（4445/4446/4447 在听）。QEMU 11.1.1 支持 `-vnc`
# 且**可带 `websocket=`**（本地 web (b) 那档的钱由此降一档）。
VNC_PORT = 4448
BT_PID_FILE = os.path.join(VM, "bt-emu.pid")
POINTER_PID_FILE = os.path.join(VM, "pointer.pid")
VMGUI_PID_FILE = os.path.join(VM, "vmgui.pid")
CONSOLE_PID_FILE = os.path.join(VM, "console.pid")   # 控制台（可视化操作界面）的记账
LICENSE_SCRIPT = os.path.join(ROOT, "scripts", "restore_license.sh")
SYSINFO_SCRIPT = os.path.join(ROOT, "scripts", "write_system_info.sh")

# The pen app draws for its own (pack-declared) panel; falcon's DRM mode can be
# wider, so on-screen capture is cropped to the pack's panel size to show just
# the pen's screen.  The size is never hard-coded here: it comes from the pack.
BTN_TOUCH = 330           # the pen UI wants a touchscreen, not a mouse click

# 主机密钥记录必须是**真实文件**，不能写"空设备"：Git-Bash/MSYS 的 ssh 不认 `NUL`，
# 会在当前工作目录建一个名叫 NUL 的真文件当 known_hosts —— 本项目就因此长期存着某个
# 旧型号的 [127.0.0.1]:2222 密钥，换包后必刷 REMOTE HOST IDENTIFICATION HAS CHANGED，
# 并连带禁用密码/键盘交互认证（裸 ssh、VSCode、ssh-vm.cmd 都会因此连不上）。
# 所有包共用同一个 host:port 而每个型号一把 dropbear 密钥，所以每次 up 清空它。
SSH_KNOWN_HOSTS = os.path.join(VM, "known_hosts_devvm")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def is_windows():
    return os.name == "nt"


def port_open(port, host="127.0.0.1", timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 进程探活：Windows 走 Win32，**不走 tasklist**
# --------------------------------------------------------------------------- #
# 为什么较真这件事：`tasklist` 每次调用都 spawn 一个进程（实测 475~545 ms）。控制台
# 的**每一帧**都要在 UI 线程上问一次"机器还在跑吗"（`window.py:_on_frame` →
# `_status_text` 里那个「（上次）」），实测 2.6 帧/秒 × 0.53 s —— 占空比超过 100 %，
# 事件队列永远排不空。用任务管理器的判据（`SendMessageTimeout(WM_NULL)`）量正在跑的
# 控制台：5 s **没回话**，`getlasterror=1460`(ERROR_TIMEOUT)，窗口直接被判"未响应"。
# 同一件事换成 `OpenProcess` + `GetExitCodeProcess` 是 0.01 ms，进程名枚举换成
# Toolhelp32 快照是 5.8 ms。
class _WinProcs:
    STILL_ACTIVE = 259
    ACCESS_DENIED = 5
    QUERY_LIMITED_INFORMATION = 0x1000
    SNAP_PROCESS = 0x2

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c = ctypes
        self.DWORD = wintypes.DWORD

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        self.Entry = PROCESSENTRY32W
        # use_last_error=True：`OpenProcess` 失败的原因要用 GetLastError 分，
        # 而 ctypes 默认会把错误码存到别处去（那样读回来的是上一个调用的）。
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        P = ctypes.POINTER
        k.OpenProcess.restype, k.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetExitCodeProcess.restype, k.GetExitCodeProcess.argtypes = wintypes.BOOL, [wintypes.HANDLE, P(wintypes.DWORD)]
        k.CreateToolhelp32Snapshot.restype, k.CreateToolhelp32Snapshot.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.restype, k.Process32FirstW.argtypes = wintypes.BOOL, [wintypes.HANDLE, P(PROCESSENTRY32W)]
        k.Process32NextW.restype, k.Process32NextW.argtypes = wintypes.BOOL, [wintypes.HANDLE, P(PROCESSENTRY32W)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]

    def pid_alive(self, pid):
        c = self.c
        h = self.k.OpenProcess(self.QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # 打不开句柄有两种意思：**进程在、只是没权限看**，和压根没有这个 pid。
            # 前者必须答"活着"，否则界面会对着一台在跑的机器说谎（空态 #4b）。
            return c.get_last_error() == self.ACCESS_DENIED
        try:
            code = self.DWORD()
            if not self.k.GetExitCodeProcess(h, c.byref(code)):
                return True                  # 查询本身失败时不轻易判死
            return code.value == self.STILL_ACTIVE
        finally:
            self.k.CloseHandle(h)

    def pids_by_name(self, prefix):
        """按映像名前缀枚举 pid。`prefix` 一律小写比较（Windows 上的名字大小写不定）。"""
        c = self.c
        invalid = c.c_size_t(-1).value        # HANDLE 是 c_void_p，INVALID_HANDLE_VALUE 回读成无符号大整数
        snap = self.k.CreateToolhelp32Snapshot(self.SNAP_PROCESS, 0)
        if not snap or snap == invalid:
            return []
        try:
            e, out = self.Entry(), []
            e.dwSize = c.sizeof(e)
            ok = self.k.Process32FirstW(snap, c.byref(e))
            while ok:
                if e.szExeFile.lower().startswith(prefix):
                    out.append(int(e.th32ProcessID))
                ok = self.k.Process32NextW(snap, c.byref(e))
            return out
        finally:
            self.k.CloseHandle(snap)


_WIN = None


def _win_procs():
    global _WIN
    if _WIN is None:
        _WIN = _WinProcs()
    return _WIN


def pid_alive(pid):
    if not pid:
        return False
    if is_windows():
        try:
            return _win_procs().pid_alive(int(pid))
        except Exception:
            return False
    try:
        # errors="replace"：中文版 Windows 的 tasklist 会输出 GBK 提示文本，
        # 严格 UTF-8 解码会在 reader 线程里炸掉并把 stdout 变成 None
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, errors="replace",
                             timeout=15).stdout
        return str(pid) in (out or "")
    except Exception:
        return False


def read_pid():
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except ValueError:
        return None


def qemu_pids():
    """All running qemu-system-aarch64 PIDs (works even if we did not start it)."""
    if not is_windows():
        out = subprocess.run(["pgrep", "-f", "qemu-system-aarch64"],
                             capture_output=True, text=True).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    try:
        return _win_procs().pids_by_name("qemu-system-aarch64")
    except Exception:
        return []


def vm_running():
    if pid_alive(read_pid()):
        return True
    return bool(qemu_pids())


def ssh_base(interactive=False):
    a = ["ssh", "-i", KEY,
         "-o", "StrictHostKeyChecking=no",
         "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
         "-o", "ConnectTimeout=8",
         "-p", str(SSH_PORT)]
    a += [] if interactive else ["-o", "BatchMode=yes", "-T"]
    a += [f"root@127.0.0.1"]
    return a


SSH_TRANSPORT_ERRORS = ("Connection refused", "Connection reset",
                        "kex_exchange_identification", "Connection closed",
                        "Connection timed out", "No route to host")


def _shq(s):
    """POSIX 单引号转义：把一个**参数**安全地塞进 guest 的 `sh` 命令里。

    为什么需要它：busybox 的 `dirname` 只吃一个操作数，旧代码 `mkdir -p $(dirname {dest})`
    在路径带空格时会退化成光杆 `mkdir -p`（不报错、等于没建目录）；`cat > {dest}` 同理会
    把文件名截断、写出 0 字节（千问 `42` §2.3 实测）。参数包成 `'…'` 之后这些都成立。
    """
    return "'" + str(s).replace("'", "'\\''") + "'"


def guest_run(cmd, timeout=60, force_serial=False):
    """Run a command inside the pen.  Returns (rc, output).

    SSH is the primary transport; if it is not up yet (early boot) fall back to
    the serial console so `dpctl sh` works throughout the boot.
    """
    if not force_serial:
        try:
            p = subprocess.run(ssh_base() + [cmd], capture_output=True, timeout=timeout)
            err = p.stderr.decode("utf-8", "replace")
            if p.returncode != 255 or not any(e in err for e in SSH_TRANSPORT_ERRORS):
                return p.returncode, p.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return 124, f"ssh timed out after {timeout}s"
        except FileNotFoundError:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from console import Serial
    try:
        s = Serial(port=SERIAL_PORT, timeout=timeout)
    except OSError as e:
        return 1, f"no transport available (ssh down, serial: {e})"
    out, rc = s.run_cmd(cmd, timeout)   # Serial returns (output, exit_code)
    return rc, out


def find_adb():
    local = os.path.join(ROOT, "tools", "platform-tools", "adb.exe")
    if os.path.exists(local):
        return local
    return shutil.which("adb")


def adb_serial():
    return f"127.0.0.1:{ADB_HOST_PORT}"


ADB_BOOTSTRAP = (
    "mkdir -p /data/misc/adb; "
    "[ -f /data/misc/adb/adb_keys ] || : > /data/misc/adb/adb_keys; "
    "touch /tmp/.adb_auth_verified; "
    "if ! ps | grep -q '[a]dbd'; then "
    "ADB_TCP_PORT=%d ADBD_SHELL=/bin/sh setsid /usr/bin/adbd >/tmp/adbd.log 2>&1 </dev/null & "
    "sleep 1; fi; "
    "ps | grep -q '[a]dbd' && echo ADBD_UP || echo ADBD_DOWN"
) % ADB_GUEST_PORT


def guest_adb_up(timeout=30):
    """Make sure adbd is listening inside the guest (idempotent)."""
    rc, out = guest_run(ADB_BOOTSTRAP, timeout=timeout)
    return "ADBD_UP" in out


def host_adb_connect(verbose=True):
    adb = find_adb()
    if not adb:
        return None, "adb not found (expected tools/platform-tools/adb.exe or on PATH)"
    subprocess.run([adb, "start-server"], capture_output=True, timeout=60)
    p = subprocess.run([adb, "connect", adb_serial()], capture_output=True,
                       text=True, timeout=30)
    return adb, (p.stdout + p.stderr).strip()


def adb_ready(adb, timeout=20):
    """True when the device is connected and responding."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = subprocess.run([adb, "-s", adb_serial(), "get-state"],
                           capture_output=True, text=True, timeout=20)
        if "device" in p.stdout:
            return True
        time.sleep(1)
    return False


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def start_bt_emu():
    """Start the virtual Bluetooth controller answering the guest's HCI link.

    QEMU has no Bluetooth emulation and the guest kernel has no hci_vhci, so
    the guest's hci_uart talks to this process over a serial socket instead."""
    script = os.path.join(ROOT, "scripts", "bt-hci-emu.py")
    if not os.path.exists(script):
        return
    log = open(os.path.join(VM, "bt-emu.out"), "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
    proc = subprocess.Popen([sys.executable, script, "--port", str(BT_PORT)],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags)
    with open(BT_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    print(f"  bt:     virtual HCI controller (pid {proc.pid}, port {BT_PORT})")


def stop_bt_emu():
    """收掉虚拟蓝牙控制器；返回被收掉的 pid（没有则 None）。"""
    return _kill_pid_file(BT_PID_FILE)


def start_pointer():
    """Show the touch-feedback overlay over the QEMU window (Windows hosts).

    Started automatically together with the GUI (switch it off with
    `dpctl up --no-pointer`).  It runs as its own process - the overlay owns a
    Tk main loop, so it cannot share this one."""
    if not is_windows():
        return
    stop_pointer()                                    # never run two overlays
    script = os.path.join(ROOT, "scripts", "dpctl.py")
    log = open(os.path.join(VM, "pointer.out"), "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([sys.executable, "-u", script, "pointer"], cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags)
    with open(POINTER_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    print(f"  pointer: touch-feedback overlay (pid {proc.pid})"
          f"   [hide with `dpctl up --no-pointer` or `dpctl nopointer`]")


def stop_pointer():
    """收掉触摸反馈浮层；返回被收掉的 pid（没有则 None）。"""
    return _kill_pid_file(POINTER_PID_FILE)


def find_pyside_python():
    """找一个能 import PySide6 的解释器 - vmgui 的 GUI 依赖。

    宿主机默认解释器可能太新（PySide6 目前要求 Python < 3.15），所以这里按
    DICTPEN_VMGUI_PYTHON -> 常见安装位置 -> py launcher 的顺序探测。"""
    cands = []
    env = os.environ.get("DICTPEN_VMGUI_PYTHON")
    if env:
        cands.append([env])
    if is_windows():
        local = os.environ.get("LOCALAPPDATA", "")
        for ver in ("Python313", "Python312", "Python311", "Python310"):
            cands.append([os.path.join(local, "Programs", "Python", ver, "python.exe")])
        for ver in ("3.13", "3.12", "3.11", "3.10"):
            cands.append(["py", "-" + ver])
        cands.append(["python"])
    else:
        cands += [["python3"], ["python"]]
    for c in cands:
        try:
            p = subprocess.run(c + ["-c", "import PySide6"],
                               capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            return c
    return None


def start_vmgui(python_cmd, profile="", pack_name=""):
    """把 vmgui 作为 VM 的显示前端拉起来（QEMU 用 -display none，画面走 QMP）。"""
    stop_vmgui()
    script = os.path.join(ROOT, "vmgui")
    if not os.path.isdir(script):
        print("  note: 找不到 vmgui/，改用 QEMU 自带 GTK 窗口")
        return False
    log = open(os.path.join(VM, "vmgui.out"), "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
    args = ["-u", "-m", "vmgui"] + (["--profile", profile] if profile else [])
    if pack_name:
        args += ["--pack", pack_name]
    proc = subprocess.Popen(python_cmd + args, cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags)
    with open(VMGUI_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    # 前端是"起了就返回"，所以要看一眼它有没有秒退：profile 名字写错之类的问题
    # 以前会静默变成"没有窗口"，只留下一个空日志。
    time.sleep(1.5)
    if proc.poll() is not None:
        tail = ""
        try:
            with open(os.path.join(VM, "vmgui.out"), encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-5:]).rstrip()
        except OSError:
            pass
        print(f"  [!] vmgui 启动后立刻退出（rc={proc.returncode}），VM 本身还在跑但没有窗口")
        for line in tail.splitlines():
            print(f"      {line}")
        print(f"      日志：{os.path.relpath(os.path.join(VM, 'vmgui.out'), ROOT)}"
              f"；或改用 `dpctl up --display gtk`")
        return False
    print(f"  gui:    vmgui frontend (pid {proc.pid}, QMP tcp:127.0.0.1:{QMP_PORT})")
    print(f"          log: {os.path.relpath(os.path.join(VM, 'vmgui.out'), ROOT)}")
    return True


def stop_vmgui():
    """收掉 vmgui 前端；返回被收掉的 pid（没有则 None）。"""
    return _kill_pid_file(VMGUI_PID_FILE)


def console_pid():
    """控制台（可视化操作界面）的 pid；没在跑返回 None。"""
    try:
        pid = int(open(CONSOLE_PID_FILE).read().strip())
    except (OSError, ValueError):
        if os.path.exists(CONSOLE_PID_FILE):
            os.remove(CONSOLE_PID_FILE)
        return None
    return pid if pid_alive(pid) else None


def stop_console():
    """收掉控制台；返回被收掉的 pid（没有则 None）。

    `--display none` 下 dpctl 不记前端 pid，所以控制台自己把 pid 写进
    `vm/console.pid`（与 `vm/vmgui.pid` 对称）。

    **调用方只有控制台抽屉里那颗「收掉前端」**（`vmgui/console/window.py`）。
    `dpctl down` **不走这里**（决策 #70），所以别把这里当 `down` 的说明写 ——
    上一版那句「`down` 认这个文件」就是这样把 `dpctl console` 的回显带偏的。
    """
    return _kill_pid_file(CONSOLE_PID_FILE)


def _kill_pid_file(path):
    """按记账文件收掉一个进程；顺手清掉失效的记账。返回 pid 或 None。"""
    pid = None
    try:
        pid = int(open(path).read().strip())
    except (OSError, ValueError):
        pid = None
    if pid:
        # `errors="replace"`：**中文 Windows 上 taskkill 的话是 cp936**（"未找到进程…"），
        # 而 `text=True` 默认按 locale 之外的 utf-8 解 → 读线程抛 `UnicodeDecodeError`，
        # 每条 `dpctl down` 尾巴上印一段 traceback（实测 2026-10-19，bt-emu 那格）。
        # 这里的输出**没人看**，更不许它变成主角。
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, errors="replace")
    try:
        os.remove(path)
    except OSError:
        pass
    return pid


CONSOLE_LOG = os.path.join(VM, "console.out")


def _for_console(text):
    """把当前控制台码页打不出的字换成 `?`，而不是让整条命令死在 `UnicodeEncodeError`。

    窗口是 cp936，而 `console.out` 里是子进程按 **utf-8** 写的日志（`-X utf8`），
    任何 GBK 里没有的字（框线、箭头、半个替换字符）都会撞上 Q68 那一枪 —— 撞上的后果
    不是"少印一行"，是**这段诊断整段没了**，双击的人只剩一个 rc。
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(enc, "replace").decode(enc, "replace")
    except (LookupError, UnicodeError):
        return text.encode("ascii", "replace").decode("ascii")


def start_console(pack=""):
    """拉起「词典笔控制台」（`vmgui --console`）。

    **解释器不在这条命令或 `.cmd` 里挑**，走 `find_pyside_python()` 那一条探测链 ——
    与 `start_vmgui` 同源。两处各挑一遍 python 早晚漂移（README 里"QEMU 参数只住一处"
    是同一条理由）：默认 `python` 可能太新（PySide6 装在上面 import 就失败），
    双击的人看到的只是"窗口没出来"。
    """
    py = find_pyside_python()
    if not py:
        print("[!] 没找到能 import PySide6 的解释器，控制台起不来。")
        print("    装依赖：python -m pip install PySide6")
        print("    或指定：set DICTPEN_VMGUI_PYTHON=<那个能用的 python.exe>")
        return 1
    # 双击两次是这里最常见的形状。两个控制台抢一个 QMP 的后果不是"报错"，
    # 而是**其中一个永远拿不到帧**（QEMU 的 chardev 只服务一个客户端），
    # 所以宁可这里直接拒绝，也不许它变成一条要人去查触摸链的超时。
    try:
        live = int(open(CONSOLE_PID_FILE).read().strip())
    except (OSError, ValueError):
        live = None
    if live and pid_alive(live):
        print(f"[!] 已经有一个控制台在跑（pid {live}）—— 用它，或者先关掉那个窗口。")
        return 1
    owner = qmp_owner(exclude_pid=live)
    if owner:
        print(f"[!] QMP tcp:{QMP_PORT} 已经被 **{owner}** 占着，再起一个只会互相抢画面。")
        print("    先关掉那个窗口（或 `dpctl down`），再来。")
        return 1
    os.makedirs(VM, exist_ok=True)
    args = ["-X", "utf8", "-u", "-m", "vmgui", "--console"]
    if pack:
        args += ["--pack", pack]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
    with open(CONSOLE_LOG, "ab") as log:
        proc = subprocess.Popen(py + args, cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=flags)
    # 前端是"起了就返回"，所以看一眼有没有秒退：秒退在双击的场景里就是
    # "窗口一闪没了"，没有这一段回显，人只会以为脚本坏了。
    time.sleep(1.5)
    if proc.poll() is not None:
        tail = ""
        try:
            with open(CONSOLE_LOG, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-5:]).rstrip()
        except OSError:
            pass
        print(f"  [!] 控制台启动后立刻退出（rc={proc.returncode}）")
        for line in tail.splitlines():
            print("      " + _for_console(line))
        print(f"      日志：{os.path.relpath(CONSOLE_LOG, ROOT)}")
        return 1
    print(f"  console: 词典笔控制台 (pid {proc.pid}, pack={pack or '自动'})")
    print(f"           解释器：{' '.join(py)}")
    print(f"           日志：{os.path.relpath(CONSOLE_LOG, ROOT)}")
    # 别说「或 `dpctl down`」—— 决策 #70 里 `down` **故意不收控制台**
    # （Hyper-V 的「关机」不会顺手关掉管理器，`_stop_helpers` 的 docstring 写了这条）。
    # 给一个不存在的出口比不给更糟：人会以为窗口没了、其实还开着。
    print("           关闭：关掉窗口本身（`dpctl down` 不会替你关它，"
          "虚拟机停了它会自己落到「虚拟机没在跑」）")
    return 0


def cmd_console(args):
    return start_console(getattr(args, "pack", "") or "")


def qmp_owner(exclude_pid=None):
    """谁在占着 QMP 4446：返回描述串（如 "vmgui pid 1234"），没人占返回 None。

    **不做端口探测** —— QEMU 的 chardev 只服务一个客户端，"连上又不读数就断开"会让
    它之后拒绝所有连接（见 README 的 QMP 单客户端禁令）。只看记账文件。

    **QEMU 不在跑时一律返回 None**（决策 #70 / 千问的硬要求）：否则 QEMU 已经关了、
    只剩一个还开着的控制台，界面会对着"谁还活着"说谎（空态 #4b 会以为有人在占画面）。
    """
    if not vm_running():
        return None
    for label, path in (("console", CONSOLE_PID_FILE), ("vmgui", VMGUI_PID_FILE)):
        try:
            pid = int(open(path).read().strip())
        except (OSError, ValueError):
            continue
        if pid == exclude_pid:
            continue
        if pid_alive(pid):
            return f"{label} pid {pid}"
    return None


# --------------------------------------------------------------------------- #
# 固件包（框架里没有机型；一切来自 packs/<name>/）
# --------------------------------------------------------------------------- #
def state_img_for(pack_name):
    """每个包一份状态盘 —— 不同机型共用一份会让 /userdata 的目录结构互相串味。"""
    return os.path.join(VM, f"state-{pack_name}.img")


def grow_state_image(pack_name, want_mib):
    """把某包的状态盘**只往大**扩到 `want_mib` MiB（`qemu-img resize`）→ `(rc, 说明)`。

    三条边界都写进返回串，界面照原样端给人看：

    * **跑着不做**：这是 raw 文件，正被 virtio-blk 读着换底就是撕裂。
    * **只增不减**：raw 截断＝直接丢数据。要变小只有 `up --fresh` 那条路 —— 那条会先
      留档、有确认弹窗，因为丢的是这台机器攒下来的东西。
    * **扩完客机还看不到**：`/state` 是**整盘 ext4（没有分区表）**，盘文件变大了文件系统
      还是旧的；要在跑起来之后进客机 `resize2fs /dev/vdb`（在线扩，实测 a6pro 有
      `/sbin/resize2fs`）。少这一步就是"扩了但用不上"。
    """
    path = state_img_for(pack_name)
    try:
        now = os.path.getsize(path) // (1024 * 1024)
    except OSError:
        return 1, f"还没有状态盘 {os.path.basename(path)}（首次起机会按包声明的大小新建，那时不用扩）"
    if vm_running():
        return 1, f"VM 在跑 —— 扩盘要停着做（现在 {now} MiB）。先按「关机」，再回来按这一次。"
    want = int(want_mib)
    if want <= now:
        return 1, (f"只许变大（现在 {now} MiB，你要 {want} MiB）。"
                   "要变小只能「重建状态盘」—— 那会丢这台机器攒下来的东西。")
    proc = subprocess.run([QEMU_IMG, "resize", "-f", "raw", path, f"{want}M"],
                          capture_output=True)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    after = os.path.getsize(path) // (1024 * 1024)
    if proc.returncode != 0:
        return proc.returncode, out or "qemu-img resize 没给原因"
    return 0, (f"盘文件 {now} → {after} MiB。{out}\n"
               "起机后还要在客机里跑一次 `resize2fs /dev/vdb` 才真用得上（这一页有那颗按钮）。")


FAST_TAG = "ready"


def fast_state_for(pack_name):
    return os.path.join(VM, f"fast-{pack_name}.qcow2")


def fast_meta_for(pack_name):
    return os.path.join(VM, f"fast-{pack_name}.json")


def fast_status(pack_name):
    """快照的轻量状态（控制台 5 s 一轮探针会读它，不能跑 qemu-img）。"""
    state = fast_state_for(pack_name)
    meta = fast_meta_for(pack_name)
    out = {"pack": pack_name, "ready": False, "state": state, "tag": FAST_TAG}
    if not (os.path.exists(state) and os.path.exists(meta)):
        return out
    try:
        with open(meta, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    out.update(data)
    out["state"] = state
    out["ready"] = True
    return out


def _hmp(command, timeout=240):
    """HMP（4445）：控制台不占这条口，savevm/quit 用它不会和 QMP 单客户端打架。

    先读到欢迎语的 `(qemu) ` prompt 再发命令；否则欢迎语的 prompt 会被当成
    命令的回执（第一版就栽在这里，savevm 回执没读到）。
    """
    try:
        with socket.create_connection(("127.0.0.1", MONITOR_PORT), timeout=10) as s:
            s.settimeout(5)
            banner = b""
            end = time.time() + 10
            while b"(qemu) " not in banner and time.time() < end:
                try:
                    part = s.recv(4096)
                except socket.timeout:
                    break
                if not part:
                    break
                banner += part
            s.sendall((command + "\n").encode())
            end = time.time() + timeout
            buf = b""
            while time.time() < end:
                try:
                    part = s.recv(8192)
                except socket.timeout:
                    continue
                if not part:
                    break
                buf += part
                if b"(qemu) " in buf:
                    break
            return buf.decode("utf-8", "replace")
    except OSError as e:
        return f"HMP error: {e}"


def _hmp_quit():
    try:
        _hmp("quit", timeout=10)
    except Exception:
        pass

def save_fast_snapshot(pack_name, force=False):
    """把当前**冷启动状态盘**复制成独立的 qcow2，启动到首页后 savevm。

    快照里没有 usb-audio（不可迁移），所以快跑那一路没有声音；冷启动保留声音。
    """
    if vm_running():
        return 1, "虚拟机在跑 —— 先关机，再存快照。"
    raw = state_img_for(pack_name)
    if not os.path.exists(raw):
        return 1, f"还没有状态盘 {os.path.basename(raw)}；先冷启动一次。"
    fast = fast_state_for(pack_name)
    meta = fast_meta_for(pack_name)
    if os.path.exists(fast) and not force:
        return 1, f"{os.path.relpath(fast, ROOT)} 已存在；要覆盖请加 --force。"
    tmp = fast + ".tmp"
    for p in (tmp,):
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"fast: 复制状态盘 -> {os.path.relpath(fast, ROOT)}（一次性，可能要几十秒）")
    proc = subprocess.run([QEMU_IMG, "convert", "-O", "qcow2",
                           "-o", "lazy_refcounts=on", raw, tmp],
                          capture_output=True, timeout=900)
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        return 1, f"qemu-img convert 失败：{out or '没给原因'}"
    os.replace(tmp, fast)

    pack = packlib.pick_pack(pack_name)
    runtime = runtimes.get(pack.runtime)
    mem, smp, _ms, _me = machine_plan(pack_name, runtime)
    env = dict(os.environ)
    env["DICTPEN_STATE_IMG"] = fast
    env["DICTPEN_STATE_FMT"] = "qcow2"
    env["DICTPEN_AUDIO"] = "none"
    env.pop("DICTPEN_LOADVM", None)
    log_path = os.path.join(VM, "fast-save.log")
    with open(log_path, "ab") as logf:
        r = subprocess.run([sys.executable, "-X", "utf8", os.path.abspath(__file__),
                            "up", "--pack", pack_name, "--display", "none",
                            "--no-audio"], cwd=ROOT, env=env,
                           stdout=logf, stderr=subprocess.STDOUT, timeout=180)
    if r.returncode != 0:
        return 1, f"启动快照盘失败（见 {os.path.relpath(log_path, ROOT)}）"

    subprocess.run([sys.executable, "-X", "utf8", os.path.abspath(__file__),
                    "wait", "--timeout", "120"], cwd=ROOT, env=env,
                   capture_output=True, timeout=150)
    found = False
    end = time.time() + 120
    while time.time() < end:
        q = subprocess.run([sys.executable, "-X", "utf8", os.path.abspath(__file__),
                            "sh", "-c", "ps | grep '[m]iniapp'"], cwd=ROOT,
                           env=env, capture_output=True, timeout=20)
        if q.returncode == 0:
            found = True
            break
        time.sleep(2)
    if not found:
        _hmp_quit()
        return 1, "客机里没等到 miniapp，快照没建（见 vm/fast-save.log）。"
    # 等 app 的首页画出来（冷启动实测 ~25–30 s）；快照宁可晚一点，也不要半启动。
    time.sleep(30)
    out = _hmp("savevm " + FAST_TAG)
    if "Error" in out:
        _hmp_quit()
        return 1, f"savevm 失败：{out.strip()[-300:]}"
    snaps = _hmp("info snapshots")
    _hmp_quit()
    for _ in range(20):
        if not vm_running():
            break
        time.sleep(0.5)
    if FAST_TAG not in snaps:
        return 1, f"快照没有出现在 info snapshots 里：{snaps.strip()[-300:]}"
    info = {"pack": pack_name, "tag": FAST_TAG,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "memory": mem, "smp": smp, "audio": False}
    with open(meta, "w", encoding="utf-8", newline="\n") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return 0, (f"快照已建立：{os.path.relpath(fast, ROOT)}"
               f"（{mem}/{smp} vCPU，无声音；冷启动仍保留声音）")


def cmd_fast(args):
    try:
        pack = packlib.pick_pack(args.pack)
    except PackError as e:
        print(f"dpctl: {e}")
        return 2
    if args.mode == "status":
        st = fast_status(pack.name)
        if not st["ready"]:
            print(f"{pack.name}: 还没有快速启动快照（先 `dpctl fast save`）")
            return 0
        print(f"{pack.name}: 快照可用  {os.path.relpath(st['state'], ROOT)}\n"
              f"  created={st.get('created')}  tag={st.get('tag')}  "
              f"memory={st.get('memory')}  smp={st.get('smp')}  audio={st.get('audio')}")
        return 0
    rc, msg = save_fast_snapshot(pack.name, force=args.force)
    print(msg)
    return rc


def write_current_pack(name):
    os.makedirs(VM, exist_ok=True)
    with open(CURRENT_PACK_FILE, "w") as f:
        f.write(name)


def current_pack_name():
    try:
        # `open(...).read()` 这种一行式在 3.13 上每次调用都吐一条 ResourceWarning
        # （控制台现在会走这条路，`-W default` 的测试里刷屏）—— 文件句柄本来就该关。
        with open(CURRENT_PACK_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 宿主给这一台的机器参数（内存 / CPU）
# --------------------------------------------------------------------------- #
MACHINE_FILE = os.path.join(VM, "machine.toml")
MACHINE_KEYS = ("memory", "smp")
MEMORY_RE = re.compile(r"^\d+[KMGT]?$", re.I)


def parse_size_mb(text):
    """`2G` / `512M` / `2048`（裸数字＝MiB）→ MiB；认不出来返回 0（调用方据此拒绝，不猜）。"""
    s = str(text or "").strip().upper()
    if not s or not MEMORY_RE.match(s):
        return 0
    unit = s[-1]
    if not unit.isalpha():
        return int(s)
    n = int(s[:-1])
    if unit == "K":
        return n // 1024
    return n * {"M": 1, "G": 1024, "T": 1024 * 1024}[unit]


def host_memory_mb():
    """宿主总物理内存（MiB）；读不到返回 0。`GlobalMemoryStatusEx`，不 spawn 进程。"""
    if not is_windows():
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
        except (ValueError, OSError, AttributeError):
            return 0
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return 0
    except OSError:
        return 0
    return st.ullTotalPhys // (1 << 20)


def machine_overrides(pack_name=None):
    """`vm/machine.toml` 里这一台的覆盖；没文件 / 没这一节 = 空 dict（＝全默认）。

    为什么不写进 `pack.toml`：内存与 vCPU 是**宿主给这台虚拟机多少**，不是**这台机器是
    什么** —— 而 `pack.toml` 是包的一部分、会随 `dpctl pack export` 导出
    （`packlib.DIST_DEFAULT` 第一项就是它）。把本机偏好写进去等于把它发给别人。

        [a6pro]
        memory = "3G"
        smp = 6

    文件坏了也当"没覆盖"（起机不该被一个偏好文件挡住），但 `machine_plan` 会把坏在哪儿
    带出来 —— 不许静默。
    """
    data, _err = _read_machine_file()
    if not pack_name:
        pack_name = current_pack_name()
    if not pack_name:
        return {}
    sec = data.get(pack_name) or {}
    return {k: sec[k] for k in MACHINE_KEYS if k in sec}


def _read_machine_file():
    """→ `(整份 dict, 错误串)`。没有文件是 `( {}, "")`，坏文件是 `( {}, 原因)`。"""
    try:
        with open(MACHINE_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        return {}, ""
    try:
        return tomllib.loads(raw.decode("utf-8-sig")), ""
    except (ValueError, UnicodeDecodeError) as e:
        return {}, f"{os.path.basename(MACHINE_FILE)} 读不出来：{e}"


def set_machine_override(pack_name, **kv):
    """改某一节，**别的包那几节原样保留**。返回 `(生效 dict, 错误串)` —— 不写坏值。

    ⚠ 契约：**只改点名的键**；`v` 是 `None` 或空串＝**删掉那一个键**（回到 runtime 默认）。
    没点名的键不删不动 —— 所以调用方别把"这格没填"传成 `None`（控制台踩过，见
    `vmgui/console/harness.set_machine`）。
    """
    data, err = _read_machine_file()
    if err:
        return {}, err                       # 坏了就整份重写会丢别的包那几节，先让人修
    sec = dict(data.get(pack_name) or {})
    for k, v in kv.items():
        if k not in MACHINE_KEYS:
            return {}, f"不认的机器参数：{k}"
        if v in (None, ""):
            sec.pop(k, None)
            continue
        if k == "memory":
            if not parse_size_mb(v):
                return {}, f"内存要写成 `512M` / `2G` 这样（现在：{v!r}）"
            sec["memory"] = str(v).strip().upper() if str(v).strip()[-1:].isalpha() \
                else f"{str(v).strip()}M"
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            return {}, f"vCPU 要一个整数（现在：{v!r}）"
        if not 1 <= n <= 64:
            return {}, f"vCPU 要在 1~64 之间（现在：{n}）"
        sec["smp"] = n
    if sec:
        data[pack_name] = sec
    else:
        data.pop(pack_name, None)
    os.makedirs(VM, exist_ok=True)
    with open(MACHINE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 宿主给每台虚拟机多少内存 / 几个 vCPU（不改固件，只改这台虚拟机的机器参数）。\n"
                "# 由控制台「配置」页写；手改也行。键：memory = \"2G\"（K/M/G/T），smp = 4。\n"
                "# **下次起机才生效** —— QEMU 的 `-m` / `-smp` 是命令行参数，跑着的机器改不了。\n"
                "# 不写进 packs/<名>/pack.toml：那份会随包导出。\n")
        for name in sorted(data):
            f.write(f"\n[{name}]\n")
            for k in MACHINE_KEYS:
                if k in data[name]:
                    v = data[name][k]
                    f.write(f'{k} = "{v}"\n' if isinstance(v, str) else f"{k} = {v}\n")
    return machine_overrides(pack_name), ""


def machine_plan(pack_name, runtime=None):
    """这一台**下次起机**会用的机器参数：`(内存, vCPU, 每个值的来源, 错误串)`。

    来源必须一起回 —— 控制台上"当前跑着 2G / 下次起机 3G"这两个读数要能各自说清是谁定的
    （runtime 默认 / 包声明 / machine.toml），否则"改了没生效"永远说不清。
    """
    ov = machine_overrides(pack_name)
    default_mem = getattr(runtime, "default_mem", "") or "2G"
    default_smp = int(getattr(runtime, "default_smp", 0) or 4)
    mem = ov.get("memory") or default_mem
    smp = int(ov.get("smp") or default_smp)
    src = {"memory": "machine.toml" if "memory" in ov else "runtime 默认",
           "smp": "machine.toml" if "smp" in ov else "runtime 默认"}
    _, err = _read_machine_file()
    return mem, smp, src, err


def machine_detail(pack_name, runtime=None):
    """一行话：`-m 3G -smp 6（内存=machine.toml、vCPU=runtime 默认）`。CLI 与控制台共用。"""
    mem, smp, src, err = machine_plan(pack_name, runtime)
    note = f"内存={src['memory']}、vCPU={src['smp']}"
    if err:
        note += f"；⚠ {err}"
    return f"-m {mem} -smp {smp}（{note}；下次起机生效）"


def machine_plan_for(pack_name=None):
    """按**包名**算下次起机的机器参数（控制台只有名字，不该自己去凑 runtime）。

    runtime 找不到（未实现的平台 / 包读不出来）时回退到基类默认 —— 这一格宁可说
    "runtime 默认"，也不要空着让人以为没配。
    """
    name = pack_name or current_pack_name() or ""
    runtime = None
    try:
        pack = packlib.load_pack(name) if name else None
        if pack:
            runtime = runtimes.get(pack.runtime)
    except (PackError, UnsupportedPlatform):
        runtime = None
    return machine_plan(name, runtime) + (name,)


def panel_size(name=None):
    """包声明的面板尺寸；取不到就 (0, 0)（= 不裁剪，用整帧）。

    name 为空时看"当前在跑的包"；控制台起机前要的是**待选包**的尺寸，所以留了参数。
    """
    name = name or current_pack_name()
    if not name:
        return 0, 0
    try:
        pack = packlib.load_pack(name)
    except PackError:
        return 0, 0
    return pack.xres, pack.yres


def pack_report(name=None, verify=True):
    """结构化地列包 —— `cmd_packs` 是它的表格版，控制台用这个（清单 §2）。

    每项：``{name, runtime, runtime_ok, reason, soc, panel, xres, yres, rootfs,
    state_img, state_size_mb, memory, smp, memory_source, smp_source, client_profile,
    missing[], md5_bad[], ready}``。
    ``memory`` / ``smp`` 是**下次起机**会用的值（`vm/machine.toml` 覆盖 runtime 默认），
    旁边那两个 ``*_source`` 说清是谁定的 —— 控制台「配置」页要并排显示"当前跑着"和
    "下次起机"两个读数，缺了来源就说不清"改了到底生效没有"。
    ``missing`` / ``md5_bad`` / ``reason`` 都是 packlib / runtimes 的**原始串** ——
    控制台别自己造错误文案（`31-console-copy.md` §8）。
    读不出来的包也回同一套键（只是 ready=False），调用方不必特判。

    ``verify=False`` 跳过 ``[checks]`` 的 MD5 核对 —— 那一趟要**读完整份镜像**
    （GB 级，几十秒）。控制台第一趟只查"在不在"（`missing` 很便宜），
    之后再把 MD5 补上；否则状态栏的 runtime 字段要空等一分钟（实测过）。
    """
    def blank(n, reason):
        return {"name": n, "runtime": "", "runtime_ok": False, "reason": reason,
                "soc": "", "panel": "", "xres": 0, "yres": 0, "rootfs": "",
                "state_img": "", "state_size_mb": 0, "client_profile": "",
                "memory": "", "smp": 0, "memory_source": "", "smp_source": "",
                "missing": [], "md5_bad": [], "ready": False}

    names = packlib.pack_names()
    if name:
        if name not in names:
            return [blank(name, f"没有固件包 {name!r}")]
        names = [name]
    out = []
    for n in names:
        try:
            pack = packlib.load_pack(n)
        except PackError as e:
            out.append(blank(n, str(e)))
            continue
        runtime_ok, reason, runtime = True, "", None
        try:
            runtime = runtimes.get(pack.runtime)
        except UnsupportedPlatform as e:
            runtime_ok, reason = False, str(e)
        missing = [w for w, _ in pack.missing()]
        if runtime is not None:
            missing += list(runtime.validate(pack))
        md5_bad = [rel for rel, _, _ in pack.verify_md5()] if verify else []
        mem, smp, src, _err = machine_plan(pack.name, runtime)
        rec = blank(pack.name, reason)
        rec.update({
            "runtime": pack.runtime, "runtime_ok": runtime_ok, "soc": pack.soc,
            "panel": f"{pack.xres}x{pack.yres}" if pack.xres else "",
            "xres": pack.xres, "yres": pack.yres, "rootfs": pack.rootfs,
            "state_img": state_img_for(pack.name),
            "state_size_mb": pack.state_size_mb or 512,
            "memory": mem, "smp": smp,
            "memory_source": src["memory"], "smp_source": src["smp"],
            "client_profile": pack.client_profile,
            "missing": missing, "md5_bad": md5_bad,
            "ready": not missing and not md5_bad and runtime_ok,
        })
        out.append(rec)
    return out


def cmd_packs(args):
    """列出固件包及其就位情况（给 NAME 时打印该包解析后的全部路径）。"""
    names = packlib.pack_names()
    if not names:
        print(packlib.EMPTY_ADVICE)
        return 1
    if args.name:
        names = [args.name]
    for err in runtimes.load_errors():
        print(f"  [!] runtime 插件未加载：{err}")
    print(f"{'包':<12} {'runtime':<20} {'soc':<8} {'面板':<9} 镜像")
    rc = 0
    for name in names:
        try:
            pack = packlib.load_pack(name)
        except PackError as e:
            print(f"  {name}: {e}")
            rc = 1
            continue
        print("  " + pack.summary())
    if not args.name:
        return rc

    pack = packlib.load_pack(names[0])
    runtime = None
    try:
        runtime = runtimes.get(pack.runtime)
    except UnsupportedPlatform as e:
        print(f"\n  [!] {e}")
        rc = 1
    print()
    for label, value in (("包目录", pack.dir), ("runtime", pack.runtime),
                         ("soc", pack.soc or "-"), ("版本", pack.version or "-"),
                         ("rootfs", pack.rootfs), ("boot", pack.boot),
                         ("内核", runtime.kernel(pack) if runtime else "-"),
                         ("基础 initramfs", runtime.initrd(pack) if runtime else "-"),
                         ("虚拟屏", f"{pack.xres}x{pack.yres}" if pack.xres else "-"),
                         ("vmgui profile", pack.client_profile or "-"),
                         ("状态盘", state_img_for(pack.name)),
                         ("机器参数", machine_detail(pack.name, runtime)),
                         ("说明", pack.note or "-")):
        exists = "" if label in ("runtime", "soc", "版本", "虚拟屏",
                                 "vmgui profile", "说明", "状态盘", "机器参数") or not runtime \
            else ("  [就位]" if value and os.path.exists(value) else "  [缺失]")
        print(f"  {label:<16} {value}{exists}")
    print("  " + "补丁: " + (", ".join(os.path.basename(d)
                                       for d, _, _ in pack.patch_sources()) or "（无）"))
    miss = pack.missing()
    for rel, want, got in pack.verify_md5():
        print(f"  [!] {rel} MD5 不符：期望 {want}，实际 {got}")
        rc = 1
    if miss:
        print("\n  缺文件，放入包目录即可（见包内 README）；"
              "固件需自行下载（`python scripts/bootstrap.py` 会给指引）")
        return 1
    return rc


def cmd_pack(args):
    """包的分发：export（打成单文件）/ import（装回来）。"""
    try:
        if args.action == "export":
            out = packlib.export_pack(args.target, args.out or (args.target + ".dpm"))
            size = os.path.getsize(out) / (1 << 20)
            print(f"导出 {args.target} -> {os.path.relpath(out, ROOT)} ({size:.0f} MB)")
            return 0
        name = packlib.import_pack(args.target, force=args.force)
        print(f"已导入固件包：{name}"
              f"（`dpctl packs {name}` 查看，`dpctl up --pack {name}` 启动）")
        return 0
    except PackError as e:
        print(f"dpctl: {e}")
        return 1


def cmd_up(args):
    if vm_running():
        print(f"VM already running (pid {read_pid() or qemu_pids()}); use `dpctl down` first")
        return 1
    try:
        pack = packlib.pick_pack(args.pack)
    except PackError as e:
        print(f"dpctl: {e}")
        return 2
    try:
        runtime = runtimes.get(pack.runtime)
    except UnsupportedPlatform as e:
        print(f"dpctl: 包 {pack.name} 声明的 {e}")
        print("      换机型前先确认平台：RV1106 同族直接写 pack.toml 即可，"
              "异构平台要先在 scripts/runtimes/ 加一个 boot backend"
              "（见 README《换成其他机型（再打一个固件包）》）")
        return 2
    miss = pack.missing() + [(what, "") for what in runtime.validate(pack)]
    if miss:
        print(f"dpctl: 固件包 {pack.name} 缺文件/缺参数：")
        for what, path in miss:
            print(f"  {what:<10} {path}")
        print("  `python scripts/dpctl.py packs " + pack.name + "` 查看明细；"
              "固件需自行下载（见包内 README）")
        return 2
    bad = pack.verify_md5()
    if bad:
        print(f"dpctl: 固件包 {pack.name} 的镜像校验不过（可删掉该文件重新下载）：")
        for rel, want, got in bad:
            print(f"  {rel}  期望 {want}  实际 {got}")
        return 2

    if getattr(args, "fast", False):
        st = fast_status(pack.name)
        if not st["ready"]:
            print(f"dpctl: {pack.name} 还没有快速启动快照；先跑 `dpctl fast save`")
            return 2
        if args.fresh:
            print("dpctl: --fast 不能和 --fresh 一起用")
            return 2
        if getattr(args, "rescue", False) or getattr(args, "dev", False) or getattr(args, "sn", None):
            print("dpctl: --fast 只用于恢复首页快照，不能和 --rescue/--dev/--sn 一起用")
            return 2
        mem_now, smp_now, _ms, _me = machine_plan(pack.name, runtime)
        if st.get("memory") != mem_now or st.get("smp") != smp_now:
            print(f"dpctl: 快照是 {st.get('memory')}/{st.get('smp')} vCPU 建的，"
                  f"当前机器是 {mem_now}/{smp_now} vCPU；改回去或重建快照")
            return 2
        args.audio = False
        os.environ["DICTPEN_STATE_IMG"] = st["state"]
        os.environ["DICTPEN_STATE_FMT"] = "qcow2"
        os.environ["DICTPEN_LOADVM"] = FAST_TAG
        print(f"fast: 从 {os.path.relpath(st['state'], ROOT)} 恢复首页"
              "（无声音；要声音请冷启动）")

    os.makedirs(VM, exist_ok=True)
    # 每次启动都是一把新的主机密钥（换包 / --fresh 都会换），先把记录清空，否则
    # StrictHostKeyChecking=no 也照样会刷 REMOTE HOST IDENTIFICATION HAS CHANGED
    # 并连带禁用密码认证。见 SSH_KNOWN_HOSTS 的注释。
    open(SSH_KNOWN_HOSTS, "w").close()
    base_initrd = runtime.initrd(pack)
    if not os.path.exists(base_initrd):
        print(f"dpctl: 框架缺少基础 initramfs：{base_initrd}\n"
              "  先跑 `python scripts/bootstrap.py`（见 README《重建环境》）")
        return 2
    # 框架通用 initramfs + 包的 patches/pack.env/EDID -> 本次启动专用的 initrd
    initrd = packlib.ensure_initrd(pack, base_initrd, runtime.initramfs_extra(pack), VM)

    state_img = os.environ.get("DICTPEN_STATE_IMG") or state_img_for(pack.name)
    state_size = pack.state_size_mb or 512
    if args.fresh and os.path.exists(state_img):
        os.remove(state_img)
    if not os.path.exists(state_img):
        subprocess.run([QEMU_IMG, "create", "-f", "raw", state_img, f"{state_size}M"],
                       check=True, capture_output=True)
        print(f"created {os.path.relpath(state_img, ROOT)} ({state_size}M, formatted on first boot)")

    append = ("console=ttyAMA0 panic=-1 DICTPEN_INIT=/sbin/init DICTPEN_OVERLAY=1 "
              "drm_kms_helper.edid_firmware=Virtual-1:edid/pen.bin")
    if args.dev:
        append += " DICTPEN_DEVMODE=1"
    if args.license is False:
        append += " DICTPEN_LICENSE=0"
    # 真设备凭证：开机钩子（S00sysinfo）读这四个，写进 system_info.bin
    for k, v in (("sn", getattr(args, "sn", None)),
                 ("uuid", getattr(args, "uuid", None)),
                 ("model", getattr(args, "model", None)),
                 ("sku", getattr(args, "sku", None))):
        if v:
            append += f" DICTPEN_{k.upper()}={v}"
    if args.rescue:
        # 救援模式：照常挂载持久分区/overlay，但不启动 pen 的 init，
        # 串口上直接给一个 root shell（用来修坏掉的 /userdata）。
        append = append.replace("DICTPEN_INIT=/sbin/init", "DICTPEN_INIT=/bin/sh")

    # 显示前端：默认交给 vmgui（QEMU 自身 -display none，画面走 QMP），
    # gtk 保留为回退。选 vmgui 前先确认宿主机有带 PySide6 的解释器，
    # 免得启动完才发现没窗口可用。
    # `vnc`（决策 #72）：画面改走 QEMU 的 VNC 推流（4448），QMP 4446 照旧独占 ——
    # 只换帧的来路，输入仍走 QMP（`backends/vnc.py` 尚未落地，这一挡先只保证能起）。
    frontend = None
    vnc = False
    if args.rescue or args.headless or args.display == "none":
        display = "none"
    elif args.display == "vnc":
        display, vnc = "none", True
    elif args.display == "gtk":
        display = "gtk"
    else:
        py = find_pyside_python()
        if py and os.path.isdir(os.path.join(ROOT, "vmgui")):
            display, frontend = "none", py
        else:
            print("note: 没有可用的 vmgui（需要 PySide6；可设 DICTPEN_VMGUI_PYTHON），"
                  "改用 -display gtk")
            display = "gtk"

    # 与机型无关的部分（内核/驱动/显示设备由 runtime 组装）。
    mem, smp, mem_src, _mach_err = machine_plan(pack.name, runtime)
    give = parse_size_mb(mem)
    host = host_memory_mb()
    if host and give * 4 > host * 3:          # 超过宿主内存 3/4：TCG 会把宿主换页换到不能动
        print(f"note: 这台要了 {mem}，宿主总共 {host} MiB —— "
              f"超过四分之三，起得来也会卡。（改：`vm/machine.toml` 的 [{pack.name}] memory）")
    audio = bool(getattr(args, "audio", False))
    ctx = BootContext(kernel=runtime.kernel(pack), initrd=initrd, append=append,
                      state_img=state_img, mem=mem, smp=smp, audio=audio)
    # 声音：dsound 走宿主默认声卡；`hda-duplex` 是 QEMU 自带的那颗 HDA codec。
    # 声音：dsound 走宿主默认声卡；hda-duplex 必须显式绑 audiodev，否则 QEMU 报
    # `no default audio driver available`（实测）。客机侧要 snd 模块（见 initrd 清单）。
    # 走 USB 声卡（人选的方案②）：HDA 那条路客机侧 `Cannot probe codecs`，改用 xhci + usb-audio。
    audio_kind = os.environ.get("DICTPEN_AUDIO", "usb")
    # 宿主后端可换：dsound 在某些 Windows 端点上会“客机在跑、宿主没声”；sdl/wav 用来 A/B。
    audio_dev = os.environ.get("DICTPEN_AUDIODEV", "dsound")
    if audio_dev == "wav":
        audio_dev += ",id=snd0,path=" + os.path.join(VM, "audio-capture.wav")
    else:
        audio_dev += ",id=snd0"
    if not audio:
        audio_args = []
    elif audio_kind == "hda":
        audio_args = ["-audiodev", audio_dev,
                      "-device", "intel-hda",
                      "-device", "hda-duplex,audiodev=snd0"]
    else:
        audio_args = ["-audiodev", audio_dev,
                      "-device", "qemu-xhci,id=xhci",
                      "-device", "usb-audio,bus=xhci.0,audiodev=snd0"]
    cmd = [QEMU] + runtime.platform_args(pack, ctx) + audio_args + [
           "-device", "virtio-keyboard-pci",
           "-device", "virtio-tablet-pci",
           "-netdev", (f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22,"
                       f"hostfwd=tcp:127.0.0.1:{ADB_HOST_PORT}-:{ADB_GUEST_PORT}"),
           "-device", "virtio-net-pci,netdev=n0",
           "-display", display,
           "-monitor", f"tcp:127.0.0.1:{MONITOR_PORT},server,nowait",
           "-qmp", f"tcp:127.0.0.1:{QMP_PORT},server,nowait",
           "-chardev", f"socket,id=s0,host=127.0.0.1,port={SERIAL_PORT},server=on,"
                       f"wait=off,logfile={SERIAL_LOG}",
           "-serial", "chardev:s0",
           # second serial port: the guest attaches hci_uart to /dev/ttyAMA1
           # (btattach) and scripts/bt-hci-emu.py is the controller at the other
           # end of this socket, so the guest gets a working hci0.
           "-chardev", f"socket,id=s1,host=127.0.0.1,port={BT_PORT},server=on,"
                       f"wait=off",
           "-serial", "chardev:s1"]
    if vnc:
        # 画面走推流（决策 #72）：`-display none` 仍在，只是多开一个 VNC 服务端。
        # 实测 QEMU 11.1.1 两者可共存（`-M none -display none -vnc 127.0.0.1:15999`）。
        cmd += ["-vnc", f"127.0.0.1:{VNC_PORT}"]
    # DICTPEN_TRACE="virtio_gpu*" logs QEMU trace events to vm/trace.log - the
    # way to see whether the guest actually flushes its framebuffer to the host.
    if os.environ.get("DICTPEN_TRACE"):
        cmd += ["-trace", f"enable={os.environ['DICTPEN_TRACE']}",
                "-D", os.path.join(VM, "trace.log")]
    if os.environ.get("DICTPEN_LOADVM"):
        cmd += ["-loadvm", os.environ["DICTPEN_LOADVM"]]
    log = open(QEMU_LOG, "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                            creationflags=flags)
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    write_current_pack(pack.name)
    print(f"VM started (pid {proc.pid})"
          + (", GUI window should appear" if display == "gtk" else ""))
    print(f"  log:    {os.path.relpath(QEMU_LOG, ROOT)}")
    print(f"  pack:   {pack.name}  ({os.path.relpath(pack.dir, ROOT)})")
    print(f"          runtime={pack.runtime}  rootfs={os.path.basename(pack.rootfs)}"
          f"  panel={pack.xres}x{pack.yres}")
    print(f"          machine: -m {mem} -smp {smp}"
          f"（内存={mem_src['memory']}、vCPU={mem_src['smp']}；改：`vm/machine.toml`）")
    print(f"          initrd={os.path.relpath(initrd, ROOT)}"
          f"  state={os.path.relpath(state_img, ROOT)}")
    print(f"  ssh:    dpctl shell          (root@127.0.0.1:{SSH_PORT})")
    print(f"  adb:    dpctl adb            (127.0.0.1:{ADB_HOST_PORT})")
    start_bt_emu()
    if frontend:
        # 不要先探测 QMP 端口：QEMU 的 chardev 只服务一个客户端，"连上又不读数
        # 就断开"会把它的 listen 卡死（之后全部连接被拒）。vmgui 自己会退避重试，
        # 且连上后就握住连接等问候语，不会半开即弃。
        start_vmgui(frontend, pack.client_profile, pack.name)
    elif args.pointer is not False and display == "gtk":
        start_pointer()
    return 0


def _stop_helpers():
    """收掉所有辅助进程（vmgui / 蓝牙控制器 / 指针浮层），并打印"收了谁"。

    悄悄杀掉用户正在看的窗口比不杀更糟（清单 Q15 ④）—— 所以记账后一定要说出来。

    **不收控制台**（决策 #70 / Q39 选 (a)）：控制台是"人正开着的管理窗口"，
    Hyper-V 的「关机」不会顺手关掉管理器。QEMU 停了它自己会每 5 s 探到
    `vm_running()==False` 并落空态 #2（"虚拟机没在跑"），窗口继续活着。
    """
    stopped = []
    for label, fn in (("vmgui", stop_vmgui),
                      ("bt-emu", stop_bt_emu), ("pointer", stop_pointer)):
        pid = fn()
        if pid:
            stopped.append(f"{label} pid {pid}")
    if stopped:
        print("  collected: " + ", ".join(stopped))
    return stopped


def cmd_down(args):
    pid = read_pid()
    if not vm_running():
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        _stop_helpers()
        print("VM is not running")
        return 0
    if getattr(args, "force", False):
        # 「立即断电」：不请 guest 关机，直接收进程（决策 #71）。
        print("force: killing qemu without asking the guest to power off ...")
    else:
        print("asking the guest to power off ...")
        guest_run("poweroff", timeout=8)
        for _ in range(15):
            if not vm_running():
                break
            time.sleep(1)
    if vm_running():
        print("guest did not stop; killing qemu")
        for p in qemu_pids():
            subprocess.run(["taskkill", "/F", "/PID", str(p)],
                           capture_output=True, text=True, errors="replace")
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    _stop_helpers()
    print("VM stopped")
    return 0


def cmd_status(args):
    pid = read_pid()
    pids = qemu_pids()
    if pids:
        shown = pid if pid in pids else pids[0]
        extra = "" if len(pids) == 1 else f" (+{len(pids) - 1} more)"
        print(f"VM:      running (pid {shown}{extra})")
    else:
        print("VM:      stopped")
    print(f"ssh:     127.0.0.1:{SSH_PORT}  {'open' if port_open(SSH_PORT) else 'closed'}")
    print(f"adb:     127.0.0.1:{ADB_HOST_PORT}  "
          f"{'open' if port_open(ADB_HOST_PORT) else 'closed'}")
    print(f"serial:  127.0.0.1:{SERIAL_PORT}  "
          f"{'open' if port_open(SERIAL_PORT) else 'closed'}")
    print(f"bt:      127.0.0.1:{BT_PORT}  "
          f"{'open' if port_open(BT_PORT) else 'closed'}")
    cpid = console_pid()
    print(f"console: {'running (pid %d)' % cpid if cpid else 'not running'}"
          f"   [{os.path.relpath(CONSOLE_PID_FILE, ROOT)}]"
          f"  (QMP {QMP_PORT} 的归属，见 `dpctl status` 与空态 #4)")
    # VNC 单开一行、**不复用 console 那行**（决策 #72 / 千问的硬要求）：复用会让
    # 空态 #4b 的"谁在占画面"读错。只在 `--display vnc` 起机时才有 open。
    print(f"vnc:     127.0.0.1:{VNC_PORT}  "
          f"{'open' if port_open(VNC_PORT) else 'closed'}"
          f"   (--display vnc 的画面推流口；与 QMP {QMP_PORT} 互不影响)")
    # 状态盘按包分，先看当前包那份；还没有就列出所有包的
    cur = current_pack_name()
    states = [state_img_for(cur)] if cur else []
    if os.path.isdir(VM):
        states += [os.path.join(VM, f) for f in sorted(os.listdir(VM))
                   if f.startswith("state-") and f.endswith(".img")]
    states = [p for p in dict.fromkeys(states) if os.path.exists(p)]
    if states:
        for p in states:
            print(f"state:   {os.path.relpath(p, ROOT)} "
                  f"({os.path.getsize(p) // (1024 * 1024)} MiB)")
    else:
        print("state:   (not created yet)")
    try:
        pack = packlib.pick_pack(cur or None)
        miss = pack.missing()
        print(f"pack:    {pack.name}  runtime={pack.runtime}"
              f"  panel={pack.xres}x{pack.yres}"
              f"  rootfs={os.path.basename(pack.rootfs)}"
              + ("" if not miss else "  [缺 " + ",".join(w for w, _ in miss) + "]"))
    except PackError as e:
        print(f"pack:    {e}".splitlines()[0])
    if pids:
        rc, out = guest_run("mount | grep -E ' /state | /userdata ' | tr '\\n' '|'",
                            timeout=15)
        if rc == 0:
            print(f"guest:   {out.strip()}")
    adb = find_adb()
    if adb:
        p = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=30)
        for line in p.stdout.splitlines()[1:]:
            if line.strip():
                print(f"device:  {line.strip()}")
    else:
        print("adb:     not found (tools/platform-tools/adb.exe)")
    return 0


def cmd_wait(args):
    t0 = time.time()
    deadline = t0 + args.timeout
    while time.time() < deadline:
        rc, out = guest_run("echo READY", timeout=20)
        if "READY" in out:
            print(f"guest shell ready after {time.time() - t0:.0f}s")
            break
        time.sleep(3)
    else:
        print(f"timeout after {args.timeout}s waiting for the guest")
        return 1
    if args.adb:
        if not guest_adb_up():
            print("warning: adbd did not start inside the guest")
            return 1
        adb, msg = host_adb_connect()
        if not adb:
            print(msg)
            return 1
        if adb_ready(adb):
            print(f"adb device ready: {adb_serial()}")
        else:
            print(f"adb connect failed: {msg}")
            return 1
    return 0


def cmd_sh(args):
    cmd = args.command
    rc, out = guest_run(cmd, timeout=args.timeout, force_serial=args.serial)
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    return rc


def cmd_license(args):
    """Restore the pen's activation license (scripts/restore_license.sh).

    The pen activates by fetching a license bound to its device code, so this
    only gets anywhere once /userdata/cfg/system_info.bin carries a registered
    SN and UUID - the VM's copy has those fields empty (see README)."""
    if not os.path.exists(LICENSE_SCRIPT):
        print(f"no such script: {os.path.relpath(LICENSE_SCRIPT, ROOT)}")
        return 1
    if not vm_running():
        print("the VM is not running (`dpctl up` first)")
        return 1
    if push_file(LICENSE_SCRIPT, "/userdata/restore_license.sh") != 0:
        return 1
    rc, out = guest_run("sh /userdata/restore_license.sh", timeout=240)
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    return rc


def cmd_sysinfo(args):
    """Write SN/UUID/MODEL/SKU into the guest's system_info.bin.

    Use this against a running VM: it pushes scripts/write_system_info.sh and
    runs it.  After it succeeds, `dpctl license` can fetch a real license, and
    `dpctl restart` reboots into the activation-ready state."""
    if not os.path.exists(SYSINFO_SCRIPT):
        print(f"no such script: {os.path.relpath(SYSINFO_SCRIPT, ROOT)}")
        return 1
    if not vm_running():
        print("the VM is not running (`dpctl up` first)")
        return 1
    if not args.sn or not args.uuid:
        print("`--sn` and `--uuid` are both required")
        return 1
    if push_file(SYSINFO_SCRIPT, "/userdata/write_system_info.sh") != 0:
        return 1
    cmd = f"sh /userdata/write_system_info.sh -s {args.sn} -u {args.uuid}"
    if args.model:
        cmd += f" -m {args.model}"
    if args.sku:
        cmd += f" -k {args.sku}"
    rc, out = guest_run(cmd, timeout=60)
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    return rc


def cmd_shell(args):
    if not port_open(SSH_PORT):
        print("ssh port is not open; is the VM running? (dpctl up)")
        return 1
    return subprocess.call(ssh_base(interactive=True))


def _sftp_quote(path):
    """sftp 批处理里的路径：**双引号包死**（空格/中文/括号都照原样，不经二次 shell）。

    实测（千问 `42-file-transmission` §2.8，a6pro）：`sftp> put "本地 带空格.txt"
    "/tmp/qw en.txt"` 落盘文件名**原样带空格**、21 B 与源文件一致；而现方案 `cat > /tmp/qw2 en.txt`
    落盘 `/tmp/qw2`、**0 字节**、rc=1 —— 静默写坏，比报错更难查。
    """
    return '"' + str(path).replace('"', '\\"') + '"'


def sftp_run(lines, timeout=180):
    """用 sftp 批处理跑一串命令（`put`/`get`/`ls`…）。返回 ``(rc, 合并输出)``。

    批处理文件走宿主临时目录：**命令内容不经过任何宿主 shell**，所以空格、中文、引号
    都安全（这正是 `cat >` 那一版做不到的）。宿主 sftp 用 `-P` 指端口（ssh 是小写 `-p`）。
    """
    batch = os.path.join(tempfile.gettempdir(), "dpctl-sftp-batch.txt")
    with open(batch, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    cmd = [SFTP, "-i", KEY,
           "-o", "StrictHostKeyChecking=no",
           "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
           "-o", "ConnectTimeout=8",
           "-o", "BatchMode=yes",
           "-P", str(SSH_PORT), "-b", batch, "root@127.0.0.1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"sftp 不可用: {e}"
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return p.returncode, out


def _sftp_ok(out):
    """sftp 的输出里只要没有那几行"失败"的话就算成功（它 rc 不总是可信）。"""
    bad = ("No such file", "Permission denied", "not found", "Failure", "Couldn't")
    return not any(b in out for b in bad)


def push_file(local, dest):
    """把宿主文件放进 guest。

    通道优先级：**sftp**（路径带空格也安全、二进制安全）→ 回落 ssh `cat`（**引号包死**）。

    ⚠ 旧版回落是 `cat > {dest}` 裸拼路径：a6pro 实测带空格时**静默写出 0 字节、
    文件名还截断**（千问 `42` §2.3/§2.8）。所以这里把 dest 用单引号转义，至少让回落不再说谎。
    """
    if not os.path.exists(local):
        return 1, f"no such file: {local}"
    parent = posixpath.dirname(dest) or "/"
    # 目录先建好：sftp 的 put 不会自己建目录；`mkdir -p` 的参数用单引号转义（空格安全）
    guest_run(f"mkdir -p {_shq(parent)}", timeout=20)
    rc, out = sftp_run([f"put {_sftp_quote(local)} {_sftp_quote(dest)}"])
    if rc == 0 and _sftp_ok(out):
        print(f"pushed {local} -> {dest}  (sftp)")
        return 0, out
    log_note = out.strip()[-500:]
    with open(local, "rb") as f:
        p = subprocess.run(ssh_base() + [f"cat > {_shq(dest)}"], stdin=f,
                           capture_output=True)
    if p.returncode == 0:
        print(f"pushed {local} -> {dest}  (ssh cat 回落)")
        return 0, (log_note + "\n" + p.stdout.decode("utf-8", "replace"))
    return p.returncode, (log_note + "\n" + p.stderr.decode("utf-8", "replace"))


def get_file(remote, local):
    """把 guest 文件取到宿主（sftp → 回落 ssh `cat`）。返回 ``(rc, 输出)``。

    控制台此前**只能传进去、不能掏出来**（`harness.py` 只有 `put`）—— 而包 README 里
    最高频的动作恰恰是掏（`dpctl get /usr/lib/libydhal_devices.so`、取 `corefile`）。
    """
    parent = os.path.dirname(os.path.abspath(local))
    if parent:
        os.makedirs(parent, exist_ok=True)
    rc, out = sftp_run([f"get {_sftp_quote(remote)} {_sftp_quote(local)}"])
    if rc == 0 and _sftp_ok(out) and os.path.exists(local):
        print(f"pulled {remote} -> {local}  (sftp, {os.path.getsize(local)} B)")
        return 0, out
    log_note = out.strip()[-500:]
    p = subprocess.run(ssh_base() + [f"cat {_shq(remote)}"], capture_output=True)
    if p.returncode != 0:
        return p.returncode, (log_note + "\n" + p.stderr.decode("utf-8", "replace"))
    with open(local, "wb") as f:
        f.write(p.stdout)
    print(f"pulled {remote} -> {local}  (ssh cat 回落, {len(p.stdout)} B)")
    return 0, (log_note + "\n" + p.stdout.decode("utf-8", "replace"))


def cmd_put(args):
    rc, out = push_file(args.local, args.remote)
    if rc != 0:
        sys.stderr.write(out)
    return rc


def cmd_get(args):
    rc, out = get_file(args.remote, args.local)
    if rc != 0:
        sys.stderr.write(out)
    return rc


def cmd_adb(args):
    adb = find_adb()
    if not adb:
        print("adb not found. Put Android platform-tools at "
              "tools/platform-tools/ or add adb to PATH.")
        return 1
    if not port_open(ADB_HOST_PORT):
        print("adb port is not open; is the VM running? (dpctl up)")
        return 1
    if not guest_adb_up():
        print("could not start adbd inside the guest "
              "(check: dpctl sh -c 'cat /tmp/adbd.log')")
        return 1
    adb, msg = host_adb_connect()
    if not adb_ready(adb):
        print(f"adb connect failed: {msg}")
        return 1
    rest = args.args or ["devices", "-l"]
    return subprocess.call([adb, "-s", adb_serial()] + rest)


# --------------------------------------------------------------------------- #
# screen capture (host side, via the QEMU monitor)
# --------------------------------------------------------------------------- #
def screendump(dest=None, timeout=20):
    """Ask QEMU to dump the current display to a PPM file; return its path."""
    dest = dest or os.path.join(VM, "_screen.ppm")
    os.makedirs(VM, exist_ok=True)
    if os.path.exists(dest):
        os.remove(dest)
    s = socket.create_connection(("127.0.0.1", MONITOR_PORT), timeout=timeout)
    s.settimeout(timeout)
    try:
        time.sleep(0.2)
        try:
            s.recv(65536)
        except OSError:
            pass
        s.sendall(f"screendump {dest}\n".encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                sz = os.path.getsize(dest)
                time.sleep(0.15)
                if os.path.getsize(dest) == sz:
                    break
            time.sleep(0.1)
    finally:
        s.close()
    if not os.path.exists(dest):
        raise RuntimeError("screendump produced no file (is the VM running?)")
    return dest


def read_ppm(path):
    raw = open(path, "rb").read()
    i, parts = 0, []
    while len(parts) < 4:
        while raw[i:i + 1].isspace():
            i += 1
        if raw[i:i + 1] == b"#":
            while raw[i:i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not raw[j:j + 1].isspace():
            j += 1
        parts.append(raw[i:j])
        i = j
    i += 1
    return int(parts[1]), int(parts[2]), raw[i:]


def crop_rgb(rgb, gw, gh, x0, y0, cw, ch):
    out = bytearray()
    for y in range(ch):
        k = ((y0 + y) * gw + x0) * 3
        out += rgb[k:k + cw * 3]
    return bytes(out)


def png_bytes(w, h, rgb, zoom=1):
    if zoom > 1:
        rows = bytearray()
        for y in range(h):
            row = rgb[y * w * 3:(y + 1) * w * 3]
            big = bytearray()
            for x in range(w):
                big += row[x * 3:x * 3 + 3] * zoom
            rows += big * zoom
        w, h = w * zoom, h * zoom
        data = bytes(rows)
    else:
        data = rgb

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))

    rows = b"".join(b"\x00" + data[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def _swap_rb(rgb: bytes) -> bytes:
    b = bytearray(rgb)
    for i in range(0, len(b) - 2, 3):
        b[i], b[i + 2] = b[i + 2], b[i]
    return bytes(b)


def _pack_swap_rb() -> bool:
    name = current_pack_name()
    if not name:
        return False
    try:
        return bool(packlib.load_pack(name).swap_rb)
    except PackError:
        return False


def grab_pen_screen(cw=0, ch=0):
    """Return (w, h, rgb) of the pen's own screen area.

    `cw/ch` <= 0 means "what the running pack declares" (falls back to the whole
    frame when there is no pack info)."""
    if cw <= 0 or ch <= 0:
        cw, ch = panel_size()
    gw, gh, rgb = read_ppm(screendump())
    if 0 < cw <= gw and 0 < ch <= gh:
        w, h, rgb = cw, ch, crop_rgb(rgb, gw, gh, 0, 0, cw, ch)
    else:
        w, h = gw, gh
    if _pack_swap_rb():
        rgb = _swap_rb(rgb)
    return w, h, rgb


def _write_tone(path, seconds=1.0, rate=48000, freq=440, amp=12000):
    """48 kHz / 2ch / S16 的测试音（厂商播放路径固定这个格式）。"""
    import math
    import struct
    import wave

    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<hh", v, v)
    w = wave.open(path, "wb")
    try:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    finally:
        w.close()
    return path


def _parse_endpoint(text):
    """把 scripts/audio-endpoint.ps1 的输出解析成 dict（volume/muted/peak）。"""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "volume" and val.endswith("%"):
            try:
                out["volume"] = int(val[:-1])
            except ValueError:
                pass
        elif key == "muted":
            out["muted"] = val.lower() == "true"
        elif key == "peak max":
            try:
                out["peak"] = float(val.rstrip("%"))
            except ValueError:
                pass
        elif key == "endpoint":
            out["endpoint"] = val
    return out


def _endpoint_probe(extra=()):
    ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio-endpoint.ps1")
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", ps1] + list(extra),
                       capture_output=True, timeout=180)
    raw = p.stdout + p.stderr
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    return p.returncode, text, _parse_endpoint(text)


def cmd_audio_check(args):
    """客机放一段测试音，确认它真的到达宿主默认输出。"""
    if os.name != "nt":
        print("audio-check: 只在 Windows 宿主上可用（端点状态走 Core Audio）")
        return 1
    if not vm_running():
        print("audio-check: 虚拟机没在跑，先 dpctl up")
        return 1

    rc, text, st = _endpoint_probe()
    if rc != 0:
        print("audio-check: 读不到宿主默认播放设备")
        print(text.strip()[-400:])
        return 1
    print("宿主端点: %s | 音量 %s%% | 静音 %s"
          % (st.get("endpoint", "?"), st.get("volume", "?"), st.get("muted", "?")))
    if st.get("muted") or (st.get("volume") or 0) <= 0:
        print("失败：宿主默认播放设备静音或音量为 0，客机放什么都不会响。")
        print("  修：powershell -NoProfile -ExecutionPolicy Bypass -File "
              "scripts/audio-endpoint.ps1 -Unmute -SetVolume 80")
        return 1

    _rc, out = guest_run("command -v tinyplay >/dev/null 2>&1 && echo tinyplay || "
                         "{ command -v aplay >/dev/null 2>&1 && echo aplay; } || echo none",
                         timeout=20)
    player = out.strip().splitlines()[-1] if out.strip() else "none"
    if player not in ("tinyplay", "aplay"):
        print("失败：客机里没有 tinyplay/aplay，没法做放音自检")
        return 1

    local = os.path.join(VM, "audio-check.wav")
    _write_tone(local)
    try:
        rc, out = push_file(local, "/tmp/vm-audio-check.wav")
        if rc != 0:
            print("失败：测试音没推进客机")
            print(out.strip()[-300:])
            return 1
        if player == "tinyplay":
            play = "for i in 1 2 3 4 5; do tinyplay /tmp/vm-audio-check.wav -D 0 -d 0; done"
        else:
            play = "for i in 1 2 3 4 5; do aplay -q -D plughw:0,0 /tmp/vm-audio-check.wav; done"
        guest_run("nohup sh -c %s >/tmp/vm-audio-check.log 2>&1 &" % _shq(play), timeout=20)

        rc, text, st2 = _endpoint_probe(["-PeakSeconds", str(args.seconds)])
        peak = st2.get("peak")
        if peak is None:
            print("audio-check: 没读到端点峰值")
            print(text.strip()[-400:])
            return 1
        print("测试音: %s 播放 5 次，端点峰值 %.1f%%" % (player, peak))
        if peak < 1.0:
            print("失败：峰值 < 1%，声音没有到达宿主默认输出。")
            print("  查：dpctl sh -c 'cat /tmp/vm-audio-check.log'、"
                  "dpctl sh -c 'cat /proc/asound/cards'")
            return 1
        print("通过：客机放音能到宿主默认输出。")
        return 0
    finally:
        if args.keep:
            print("保留客机测试音: /tmp/vm-audio-check.wav")
        else:
            guest_run("rm -f /tmp/vm-audio-check.wav /tmp/vm-audio-check.log", timeout=20)
            try:
                os.remove(local)
            except OSError:
                pass


def cmd_screenshot(args):
    w, h, rgb = grab_pen_screen(args.w, args.h)
    out = args.out
    with open(out, "wb") as f:
        f.write(png_bytes(w, h, rgb, args.zoom))
    print(f"captured {w}x{h} -> {out}" + (f" (x{args.zoom})" if args.zoom > 1 else ""))
    return 0


def cmd_view(args):
    """Live view of the pen's GUI in a window sized to the pen's resolution."""
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter is not available; use `dpctl screenshot` instead")
        return 1
    root = tk.Tk()
    scale = f" x{args.zoom}" if args.zoom > 1 else ""
    w0, h0 = (args.w, args.h) if args.w > 0 and args.h > 0 else panel_size()
    root.title(f"dictpen {w0}x{h0}{scale}" if w0 else f"dictpen{scale}")
    root.configure(bg="black")
    label = tk.Label(root, bg="black", borderwidth=0)
    label.pack()
    state = {"img": None, "fail": 0}

    def refresh():
        try:
            w, h, rgb = grab_pen_screen(args.w, args.h)
            img = tk.PhotoImage(data=base64.b64encode(png_bytes(w, h, rgb, args.zoom)))
            state["img"] = img
            label.configure(image=img)
            state["fail"] = 0
        except Exception as e:
            state["fail"] += 1
            if state["fail"] == 1:
                sys.stderr.write(f"capture failed: {e}\n")
        root.after(max(200, int(args.interval * 1000)), refresh)

    refresh()
    root.mainloop()
    return 0


def cmd_pointer(args):
    """Touch-feedback pointer drawn over the QEMU window (Windows only).

    The pen UI is a touchscreen UI: it has no hover and gives no hint where a
    click will land, so the mouse feels blind.  This puts a transparent,
    click-through, always-on-top overlay on the QEMU window and draws a small
    ring at the cursor (filled while the button is held) - i.e. a visible marker
    for "the guest's finger is here".  Everything stays on the host; the guest
    is untouched.
    """
    if not is_windows():
        print("pointer: only implemented for the Windows host")
        return 1
    try:
        import ctypes
        import tkinter as tk
        from ctypes import wintypes
    except ImportError as e:
        print(f"pointer: {e}")
        return 1

    pid = read_pid() or next(iter(qemu_pids()), None)
    if not pid or not pid_alive(pid):
        print("pointer: the VM is not running (`dpctl up` first)")
        return 1

    u = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x80
    WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x8000000
    SWP_FLAGS = 0x2 | 0x1 | 0x4 | 0x10 | 0x20      # NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED
    VK_LBUTTON = 0x01
    KEY = "#010203"                                 # colour keyed away (transparent)

    def qemu_window():
        """The QEMU display window: visible, biggest client area (0x0 hints are
        helper windows QEMU/GTK also creates)."""
        best = [None, 0]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            wpid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if (wpid.value != pid or not u.IsWindowVisible(hwnd)
                    or u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW):
                return True
            r = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[0], best[1] = hwnd, area
            return True

        u.EnumWindows(cb, 0)
        return best[0]

    root = tk.Tk()
    root.withdraw()
    prev_fg = u.GetForegroundWindow()               # do not steal the user's focus
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-transparentcolor", KEY)
    win.configure(bg=KEY)
    canvas = tk.Canvas(win, bg=KEY, highlightthickness=0, borderwidth=0)
    canvas.pack(fill="both", expand=True)
    win.update_idletasks()
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                    u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
    if prev_fg:
        u.SetForegroundWindow(prev_fg)

    # reusable canvas items: recreating them every frame is far too slow for a
    # layered window (each change repaints the whole window -> the marker trailed)
    ring_out = canvas.create_oval(0, 0, 0, 0, outline="#ffffff", width=3, state="hidden")
    ring_in = canvas.create_oval(0, 0, 0, 0, outline="#00b0ff", width=2, state="hidden")
    dot = canvas.create_oval(0, 0, 0, 0, fill="#ff3b30", outline="", state="hidden")

    k = ctypes.windll.kernel32                        # OpenProcess lives here
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    def proc_exited():
        """Cheap liveness check - spawning `tasklist` every frame was the killer.
        Answers False whenever it cannot tell, so the overlay never dies early."""
        h = k.OpenProcess(0x1000, False, pid)         # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value != 259                  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)

    cache = {"win": None, "at": 0.0}
    geo = {"last": None, "wg": None, "probe": 0, "panel": None}
    debug = bool(os.environ.get("DICTPEN_POINTER_DEBUG"))
    print("pointer: overlay is live - press Ctrl+C here to stop")

    def hide():
        for i in (ring_out, ring_in, dot):
            canvas.itemconfig(i, state="hidden")

    def tick():
        geo["probe"] += 1
        if geo["probe"] % 20 == 0 and proc_exited():
            root.destroy()
            return
        now = time.time()
        h = cache["win"]
        if not h or now - cache["at"] > 1.0 or not u.IsWindow(h):
            h = qemu_window()
            cache["win"], cache["at"] = h, now
        if not h:                                     # minimised / hidden: pause
            hide()
            geo["last"] = None
            root.after(300, tick)
            return
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        u.GetClientRect(h, ctypes.byref(rect))
        u.ClientToScreen(h, ctypes.byref(origin))
        cw, ch = rect.right - rect.left, rect.bottom - rect.top
        cur = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(cur))
        mx, my = cur.x - origin.x, cur.y - origin.y
        inside = 0 <= mx < cw and 0 <= my < ch
        held = bool(u.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        state = (cw, ch, origin.x, origin.y, mx, my, held)
        if state != geo["last"]:
            geo["last"] = state
            if geo["wg"] != state[:4]:
                win.geometry("%dx%d+%d+%d" % state[:4])
                geo["wg"] = state[:4]
            if not inside:
                hide()
            else:
                # the guest screen is aspect-fitted and centred in the window and
                # QEMU clamps touches into it, so clamp the marker the same way
                pw, ph = geo["panel"] or panel_size() or (cw, ch)
                geo["panel"] = (pw, ph)
                scale = min(cw / pw, ch / ph) or 1.0
                gw, gh = pw * scale, ph * scale
                gx, gy = (cw - gw) / 2.0, (ch - gh) / 2.0
                mx = min(max(mx - gx, 0.0), gw - 1) + gx
                my = min(max(my - gy, 0.0), gh - 1) + gy
                r = max(5.0, 6.0 * scale)
                for i in (ring_out, ring_in):
                    canvas.coords(i, mx - r, my - r, mx + r, my + r)
                    canvas.itemconfig(i, state="normal")
                canvas.itemconfig(ring_in, outline="#ff3b30" if held else "#00b0ff")
                canvas.coords(dot, mx - 3, my - 3, mx + 3, my + 3)
                canvas.itemconfig(dot, state="normal" if held else "hidden")
            if debug:
                print("pointer: window %dx%d at (%d,%d) marker (%d,%d) held=%d"
                      % (cw, ch, origin.x, origin.y, mx, my, held), flush=True)
        root.after(10, tick)

    tick()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_nopointer(args):
    """Take the touch-feedback overlay back down (see `up --pointer`)."""
    if not os.path.exists(POINTER_PID_FILE):
        print("pointer: no overlay is running")
        return 0
    stop_pointer()
    print("pointer: overlay stopped")
    return 0


# --------------------------------------------------------------------------- #
# touch injection over QMP (the HMP monitor cannot send absolute coordinates)
# --------------------------------------------------------------------------- #
def _qmp_blocked_hint(exc) -> str:
    """连上了却拿不到 QMP 应答时，**指名占用者**（决策 #73 / Q42）。

    千问实测：`dpctl up` 会自动拉起 vmgui 独占 4446，随后 `dpctl tap` **挂 20 s
    `timed out`** —— 人会对着这个超时去查触摸链，而真正坏的是"两个客户端抢一个 QMP"。
    """
    owner = qmp_owner()
    if owner:
        return (f"QMP tcp:{QMP_PORT} 连上了但没有人应答（{exc}）。"
                f"QEMU 的 QMP 只服务一个客户端，现在占着它的是 **{owner}**"
                f"（`dpctl up` 会自动拉起 vmgui；控制台是 `dpctl console`，"
                f"或直接双击 `vm-console.cmd`）。"
                f"要注入手势：先关掉那个客户端，或 `dpctl down` 后 `up --display none`。")
    return (f"QMP tcp:{QMP_PORT} 连上了但没有人应答（{exc}），"
            f"而记账文件里没有别的客户端 —— 原因待查（见 `dpctl status`）。")


def qmp_send(batches, timeout=15):
    """Open the QMP socket and run the given command dicts in order."""
    try:
        s = socket.create_connection(("127.0.0.1", QMP_PORT), timeout=timeout)
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        # QEMU gives a QMP chardev exactly one client, and the default
        # `dpctl up` starts vmgui, which owns 127.0.0.1:4446 for the whole
        # session.  A bare "WinError 10061" then reads as "the VM is down" -
        # it is not, and `dpctl screenshot` (HMP on 4445) keeps working.
        raise RuntimeError(
            f"QMP tcp:{QMP_PORT} 拿不到连接（{e}）。VM 本身在跑：QEMU 的 QMP 只服务"
            f"一个客户端，默认启动时 vmgui 已经占着它。要注入手势，请 "
            f"`dpctl down` 后用 `dpctl up --display none`（或 --display gtk）重启。"
        ) from e
    f = s.makefile("rwb")
    try:
        # 第二个客户端能连上（内核 backlog 收下），但 chardev 只服务第一个，
        # 于是**读 greeting 时超时** —— 这一档过去只留一句 "timed out"（Q42）。
        try:
            json.loads(f.readline().decode("utf-8", "replace"))   # greeting
        except (TimeoutError, OSError) as e:
            raise RuntimeError(_qmp_blocked_hint(e)) from e

        def run(cmd):
            f.write((json.dumps(cmd) + "\n").encode())
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    raise RuntimeError("qmp connection closed")
                obj = json.loads(line.decode("utf-8", "replace"))
                if "return" in obj or "error" in obj:
                    return obj

        run({"execute": "qmp_capabilities"})
        return [run(c) for c in batches]
    finally:
        try:
            f.close()
        except OSError:
            pass
        s.close()


def cmd_tap(args):
    """Inject one touch tap at (x, y) in pen-screen pixels.

    QEMU's mouse path only ever sends BTN_LEFT, but the pen's UI wants a
    touchscreen: ABS_X/ABS_Y plus BTN_TOUCH.  Both are advertised by the
    virtio tablet, so we can send them by hand over QMP.

    事件顺序是按内核 mousedev 的"触摸板"路径设计的（QEMU tablet 带
    BTN_TOOL_FINGER，/dev/input/mouseN 走 mousedev_touchpad_event）：
      * BTN_TOUCH 必须先于 ABS——只有触摸期间的 ABS 才被换算成移动量；
      * pkt_count 在每个 SYN +1，前 2 个不产生移动，所以要发三批 ABS；
      * 整个触摸要保持 > tap_time（默认 200ms），否则 mousedev 的
        tap-to-click 会模拟出第二次点击（双击落在换页后的新页面上）；
      * BTN_LEFT 的按下/抬起仍要发——a6pro/a7pro/x7 的触摸桥靠它翻译，
        本机（s6pro）的应用则通过 mouse0 的 dx/dy 移动量跟踪坐标。
    """
    # 不要用 port_open 先探端口：QEMU 的 QMP chardev 只服务一个客户端，"连上又不
    # 读数就断开"会让它不再 listen，之后所有连接都被拒。直接发命令、失败再报。
    w, h = (args.w, args.h) if args.w > 0 and args.h > 0 else panel_size()
    if w <= 0 or h <= 0:
        print("tap: 不知道笔屏尺寸（当前没有在跑的包），请显式给 --w/--h")
        return 1
    ax = int(round(args.x * 32767 / w))
    ay = int(round(args.y * 32767 / h))
    absx = {"type": "abs", "data": {"axis": "x", "value": ax}}
    absy = {"type": "abs", "data": {"axis": "y", "value": ay}}
    touch = {"type": "key", "data": {"down": True,
                                     "key": {"type": "number", "data": BTN_TOUCH}}}
    unttouch = {"type": "key", "data": {"down": False,
                                        "key": {"type": "number", "data": BTN_TOUCH}}}
    btns = []
    if not args.no_btn_left:
        btns = [{"type": "btn", "data": {"down": True, "button": "left"}}]
    unbtns = [{"type": "btn", "data": {"down": False, "button": "left"}}] if btns else []
    try:
        down = [touch, absx, absy]
        qmp_send([{"execute": "input-send-event", "arguments": {"events": down}}])
        # 触摸期间的 ABS 才产生移动；前两个 SYN 只累计 pkt_count
        for _ in range(2):
            qmp_send([{"execute": "input-send-event", "arguments": {"events": [absx, absy]}}])
        qmp_send([{"execute": "input-send-event", "arguments": {"events": [absx, absy] + btns}}])
        # 触摸保持超过 mousedev tap_time(200ms)，避免 tap-to-click 二次点击
        time.sleep(max(0.25, args.hold))
        qmp_send([{"execute": "input-send-event", "arguments": {"events": unbtns + [unttouch]}}])
    except Exception as e:
        print(f"tap failed: {e}")
        return 1
    print(f"tap ({args.x},{args.y}) -> abs ({ax},{ay})")
    return 0


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="dpctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("up", help="start the VM (GUI + persistent state disk)")
    p.add_argument("--fresh", action="store_true", help="wipe the persistent state disk")
    p.add_argument("--fast", action="store_true",
                   help="从快速启动快照恢复（无声音；快照用 `dpctl fast save` 建）")
    p.add_argument("--headless", action="store_true", help="no GUI at all")
    p.add_argument("--audio", action=argparse.BooleanOptionalAction, default=True,
                   help="客机声音送到宿主机（默认开：QEMU usb-audio + 宿主 dsound）；"
                        "--no-audio 关掉")
    p.add_argument("--display", choices=["vmgui", "gtk", "none", "vnc"], default="vmgui",
                   help="显示前端：vmgui（默认，QMP + PySide6 客户端）| gtk（QEMU 自带窗口）"
                        "| none（不要窗口）| vnc（画面走 VNC 推流 tcp:127.0.0.1:4448，"
                        "QMP 4446 照旧独占）")
    p.add_argument("--dev", action="store_true", help="keep the miniapp launcher enabled")
    p.add_argument("--rescue", action="store_true",
                   help="boot straight to a root shell (no pen UI) for repairs")
    p.add_argument("--pack", metavar="NAME",
                   help="固件包名，对应 packs/<NAME>/pack.toml"
                        "（只装了一个包时可省略）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pointer", dest="pointer", action="store_true", default=None,
                   help="show the touch-feedback overlay (default: yes with the GUI;"
                        " Windows host only)")
    g.add_argument("--no-pointer", dest="pointer", action="store_false",
                   help="do not show the touch-feedback overlay")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--license", dest="license", action="store_true",
                   default=None,
                   help="restore the activation license at boot (default: yes)")
    g.add_argument("--no-license", dest="license", action="store_false",
                   help="do not try to restore the activation license")
    # 真设备凭证：开机钩子把它们写进 /userdata/cfg/system_info.bin，
    # 然后走 S99license 的服务器取真 license 路线（见 packs/a7pro/README.md）。
    p.add_argument("--sn", dest="sn", metavar="ME…",
                   help="序列号（ME 开头 + 数字），写进 system_info.bin 的 SN 槽")
    p.add_argument("--uuid", dest="uuid", metavar="32HEX",
                   help="32 位小写 hex UUID，写进 system_info.bin 的 UUID 槽")
    p.add_argument("--model", dest="model", metavar="YDP…",
                   help="机型（YDPA7-1 等），决定 client 参数")
    p.add_argument("--sku", dest="sku", metavar="OVERHEAD_…",
                   help="SKU 字符串（OVERHEAD_A71_SKU_CHN_PRO 等）")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("console",
                       help="打开词典笔控制台窗口（vmgui --console），`vm-console.cmd` 就是它")
    p.add_argument("--pack", metavar="NAME", default="",
                   help="控制台要操作的固件包（不给就跟着当前在跑的那台）")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("packs",
                       help="列出固件包（runtime / 面板 / 镜像是否就位）")
    p.add_argument("name", nargs="?", help="只看某个包的明细")
    p.set_defaults(func=cmd_packs)

    p = sub.add_parser("pack", help="固件包的分发（export / import）")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("target", help="export: 包名；import: .dpm 文件路径")
    p.add_argument("-o", "--out", help="export 的输出文件（默认 <包名>.dpm）")
    p.add_argument("--force", action="store_true", help="import 时覆盖同名包")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("fast", help="快速启动快照：save / status（恢复用 up --fast）")
    p.add_argument("mode", choices=["save", "status"])
    p.add_argument("--pack", metavar="NAME")
    p.add_argument("--force", action="store_true", help="覆盖已有快照")
    p.set_defaults(func=cmd_fast)

    p = sub.add_parser("down", help="stop the VM")
    p.add_argument("--force", action="store_true",
                   help="立即断电：不请 guest 关机，直接收 QEMU 进程（决策 #71）")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("status", help="show VM / port / adb state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", help="wait until the guest shell (and adb) is ready")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--adb", action="store_true", help="also bring up adb and wait for it")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("sh", help="run a command inside the pen")
    p.add_argument("-c", "--command", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--serial", action="store_true", help="force the serial console")
    p.set_defaults(func=cmd_sh)

    p = sub.add_parser("shell", help="interactive shell inside the pen")
    p.set_defaults(func=cmd_shell)

    p = sub.add_parser("put", help="copy a file to the pen")
    p.add_argument("local")
    p.add_argument("remote")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("get", help="copy a file from the pen")
    p.add_argument("remote")
    p.add_argument("local")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("adb", help="run adb against the pen (connect is automatic)")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_adb)

    p = sub.add_parser("screenshot", help="save the pen's GUI to a PNG")
    p.add_argument("--out", default=os.path.join(VM, "screen.png"))
    p.add_argument("--w", type=int, default=0, help="裁剪宽（默认取当前包的面板尺寸）")
    p.add_argument("--h", type=int, default=0, help="裁剪高（默认取当前包的面板尺寸）")
    p.add_argument("--zoom", type=int, default=1)
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("view", help="live window showing the pen's GUI at its own resolution")
    p.add_argument("--w", type=int, default=0, help="默认取当前包的面板尺寸")
    p.add_argument("--h", type=int, default=0)
    p.add_argument("--zoom", type=int, default=2, help="upscale factor (default 2)")
    p.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("pointer",
                       help="touch-feedback marker drawn over the QEMU window")
    p.set_defaults(func=cmd_pointer)

    p = sub.add_parser("nopointer",
                       help="stop the touch-feedback overlay started by `up`")
    p.set_defaults(func=cmd_nopointer)

    p = sub.add_parser("license",
                       help="restore the activation license (restore_license.sh)")
    p.set_defaults(func=cmd_license)

    p = sub.add_parser("sysinfo",
                       help="写 SN/UUID/MODEL/SKU 到 guest 的 system_info.bin")
    p.add_argument("--sn", required=True, metavar="ME…",
                   help="序列号（ME 开头 + 数字）")
    p.add_argument("--uuid", required=True, metavar="32HEX",
                   help="32 位小写 hex UUID")
    p.add_argument("--model", metavar="YDP…", help="机型（YDPA7-1 等）")
    p.add_argument("--sku", metavar="OVERHEAD_…", help="SKU 字符串")
    p.set_defaults(func=cmd_sysinfo)

    p = sub.add_parser("audio-check",
                       help="客机放测试音，验证它到达宿主默认输出（端点峰值）")
    p.add_argument("--seconds", type=float, default=8.0, help="采样窗口，默认 8s")
    p.add_argument("--keep", action="store_true", help="保留客机里的测试音与日志")
    p.set_defaults(func=cmd_audio_check)

    p = sub.add_parser("tap", help="inject a touch tap at pen-screen coords (via QMP)")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--w", type=int, default=0, help="笔屏宽（默认取当前包的面板尺寸）")
    p.add_argument("--h", type=int, default=0, help="笔屏高（默认取当前包的面板尺寸）")
    p.add_argument("--hold", type=float, default=0.12)
    p.add_argument("--no-btn-left", action="store_true",
                   help="send only BTN_TOUCH (no left-button event)")
    p.set_defaults(func=cmd_tap)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())