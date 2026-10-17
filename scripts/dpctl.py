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
import shutil
import socket
import struct
import subprocess
import sys
import time
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
BT_PID_FILE = os.path.join(VM, "bt-emu.pid")
POINTER_PID_FILE = os.path.join(VM, "pointer.pid")
VMGUI_PID_FILE = os.path.join(VM, "vmgui.pid")
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


def pid_alive(pid):
    if not pid:
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
        return int(open(PID_FILE).read().strip())
    except ValueError:
        return None


def qemu_pids():
    """All running qemu-system-aarch64 PIDs (works even if we did not start it)."""
    if not is_windows():
        out = subprocess.run(["pgrep", "-f", "qemu-system-aarch64"],
                             capture_output=True, text=True).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq qemu-system-aarch64.exe",
                              "/NH"], capture_output=True, text=True,
                             errors="replace", timeout=15).stdout
    except Exception:
        return []
    pids = []
    for line in (out or "").splitlines():
        parts = line.split()
        if parts and parts[0].lower().startswith("qemu-system-aarch64") and len(parts) > 1:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


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
    if not os.path.exists(BT_PID_FILE):
        return
    try:
        pid = int(open(BT_PID_FILE).read().strip())
    except ValueError:
        os.remove(BT_PID_FILE)
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True)
    os.remove(BT_PID_FILE)


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
    if not os.path.exists(POINTER_PID_FILE):
        return
    try:
        pid = int(open(POINTER_PID_FILE).read().strip())
    except ValueError:
        os.remove(POINTER_PID_FILE)
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True)
    os.remove(POINTER_PID_FILE)


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


def start_vmgui(python_cmd, profile=""):
    """把 vmgui 作为 VM 的显示前端拉起来（QEMU 用 -display none，画面走 QMP）。"""
    stop_vmgui()
    script = os.path.join(ROOT, "vmgui")
    if not os.path.isdir(script):
        print("  note: 找不到 vmgui/，改用 QEMU 自带 GTK 窗口")
        return False
    log = open(os.path.join(VM, "vmgui.out"), "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
    args = ["-u", "-m", "vmgui"] + (["--profile", profile] if profile else [])
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
    if not os.path.exists(VMGUI_PID_FILE):
        return
    try:
        pid = int(open(VMGUI_PID_FILE).read().strip())
    except ValueError:
        os.remove(VMGUI_PID_FILE)
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True)
    os.remove(VMGUI_PID_FILE)


# --------------------------------------------------------------------------- #
# 固件包（框架里没有机型；一切来自 packs/<name>/）
# --------------------------------------------------------------------------- #
def state_img_for(pack_name):
    """每个包一份状态盘 —— 不同机型共用一份会让 /userdata 的目录结构互相串味。"""
    return os.path.join(VM, f"state-{pack_name}.img")


def write_current_pack(name):
    os.makedirs(VM, exist_ok=True)
    with open(CURRENT_PACK_FILE, "w") as f:
        f.write(name)


def current_pack_name():
    try:
        return open(CURRENT_PACK_FILE).read().strip() or None
    except OSError:
        return None


def panel_size():
    """当前在跑的包声明的面板尺寸；取不到就 (0, 0)（= 不裁剪，用整帧）。"""
    name = current_pack_name()
    if not name:
        return 0, 0
    try:
        pack = packlib.load_pack(name)
    except PackError:
        return 0, 0
    return pack.xres, pack.yres


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
                         ("说明", pack.note or "-")):
        exists = "" if label in ("runtime", "soc", "版本", "虚拟屏",
                                 "vmgui profile", "说明", "状态盘") or not runtime \
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

    state_img = state_img_for(pack.name)
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
    frontend = None
    if args.rescue or args.headless or args.display == "none":
        display = "none"
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
    ctx = BootContext(kernel=runtime.kernel(pack), initrd=initrd, append=append,
                      state_img=state_img)
    cmd = [QEMU] + runtime.platform_args(pack, ctx) + [
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
    # DICTPEN_TRACE="virtio_gpu*" logs QEMU trace events to vm/trace.log - the
    # way to see whether the guest actually flushes its framebuffer to the host.
    if os.environ.get("DICTPEN_TRACE"):
        cmd += ["-trace", f"enable={os.environ['DICTPEN_TRACE']}",
                "-D", os.path.join(VM, "trace.log")]
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
    print(f"          initrd={os.path.relpath(initrd, ROOT)}"
          f"  state={os.path.relpath(state_img, ROOT)}")
    print(f"  ssh:    dpctl shell          (root@127.0.0.1:{SSH_PORT})")
    print(f"  adb:    dpctl adb            (127.0.0.1:{ADB_HOST_PORT})")
    start_bt_emu()
    if frontend:
        # 不要先探测 QMP 端口：QEMU 的 chardev 只服务一个客户端，"连上又不读数
        # 就断开"会把它的 listen 卡死（之后全部连接被拒）。vmgui 自己会退避重试，
        # 且连上后就握住连接等问候语，不会半开即弃。
        start_vmgui(frontend, pack.client_profile)
    elif args.pointer is not False and display == "gtk":
        start_pointer()
    return 0


def cmd_down(args):
    pid = read_pid()
    if not vm_running():
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        stop_bt_emu()
        stop_pointer()
        stop_vmgui()
        print("VM is not running")
        return 0
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
                           capture_output=True, text=True)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    stop_bt_emu()
    stop_pointer()
    stop_vmgui()
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


def push_file(local, dest):
    """Copy a host file into the guest (adb push, ssh `cat` fallback)."""
    guest_run(f"mkdir -p $(dirname {dest})", timeout=20)
    adb = find_adb()
    if adb and adb_ready(adb, timeout=5):
        p = subprocess.run([adb, "-s", adb_serial(), "push", local, dest],
                           capture_output=True, text=True)
        sys.stderr.write(p.stdout + p.stderr)
        return p.returncode
    with open(local, "rb") as f:
        p = subprocess.run(ssh_base() + [f"cat > {dest}"], stdin=f,
                           capture_output=True)
    if p.returncode == 0:
        print(f"pushed {local} -> {dest}")
    else:
        sys.stderr.write(p.stderr.decode("utf-8", "replace"))
    return p.returncode


def cmd_put(args):
    if not os.path.exists(args.local):
        print(f"no such file: {args.local}")
        return 1
    return push_file(args.local, args.remote)


def cmd_get(args):
    dest = args.local
    adb = find_adb()
    if adb and adb_ready(adb, timeout=5):
        p = subprocess.run([adb, "-s", adb_serial(), "pull", args.remote, dest],
                           capture_output=True, text=True)
        sys.stderr.write(p.stdout + p.stderr)
        return p.returncode
    p = subprocess.run(ssh_base() + [f"cat {args.remote}"], capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode("utf-8", "replace"))
        return p.returncode
    with open(dest, "wb") as f:
        f.write(p.stdout)
    print(f"pulled {args.remote} -> {dest}")
    return 0


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


def grab_pen_screen(cw=0, ch=0):
    """Return (w, h, rgb) of the pen's own screen area.

    `cw/ch` <= 0 means "what the running pack declares" (falls back to the whole
    frame when there is no pack info)."""
    if cw <= 0 or ch <= 0:
        cw, ch = panel_size()
    gw, gh, rgb = read_ppm(screendump())
    if 0 < cw <= gw and 0 < ch <= gh:
        return cw, ch, crop_rgb(rgb, gw, gh, 0, 0, cw, ch)
    return gw, gh, rgb


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
        json.loads(f.readline().decode("utf-8", "replace"))   # greeting

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
    p.add_argument("--headless", action="store_true", help="no GUI at all")
    p.add_argument("--display", choices=["vmgui", "gtk", "none"], default="vmgui",
                   help="显示前端：vmgui（默认，QMP + PySide6 客户端）| gtk（QEMU 自带窗口）"
                        "| none（不要窗口）")
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

    p = sub.add_parser("down", help="stop the VM")
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