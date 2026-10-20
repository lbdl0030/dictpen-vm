"""状态栏批量探针：guest 侧一次跑完，输出 `key=value`。

纪律（见 `vm/ui-council/31-trae-changeset.md` 与 `90-decisions.md`）：

* **单趟** ``ls -l /proc/[0-9]*/fd`` + awk —— 逐个 pid fork ``ls`` 的写法实测 5.137 s，
  这一版 0.754 s；不这么写，"5 s 一轮"从第一天就超预算（决策 #20）。
* 触摸节点 ``device.tp`` 是**软链**（``/dev/input/by-path/axs_ts``），读者用**解析后的
  真名**（``/dev/input/event2``）打开它 → 必须 ``readlink -f`` 之后两个名字都匹配，
  否则 ``holder`` 恒空、灯恒灭（决策 #18）。
* 本模块**只产 key=value + ``probe_ok``**，判据在 ``core/rules.py``（决策 #26）。

``probe_ok`` 三态（决策 #26）：

=================  ==================================================
``ok``             SSH 往返成功
``serial``         回落串口拿到输出（输出里会混着历史控制台日志）
``no-shell``       两条通道都没通（VM 没在跑 / 还在起）
=================  ==================================================

本模块不 import PySide6，也不自己建连接：传输由调用方注入（``collect(transport)``）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# 应用注册表逐条对盘的那段 awk（Q70）。**单独成常量**是为了让单测能拿临时注册表在宿主上
# 跑同一段判据 —— 客机里那台 busybox awk 与 GNU awk 在这段用到的子集上行为一致
# （`match`/`RSTART`/`substr`/`getline < 文件`/`close`）。
_APPS_AWK = r"""
BEGIN { FS = "\"appid\": \"" }
{
  for (i = 2; i <= NF; i++) {
    seg = $i; total++
    a = substr(seg, 1, 24); sub(/[^0-9].*/, "", a)
    ip = ""; ic = ""
    if (match(seg, /"installPath": "[^"]*"/)) { ip = substr(seg, RSTART, RLENGTH); sub(/^"installPath": "/, "", ip); sub(/"$/, "", ip) }
    if (match(seg, /"icon": "[^"]*"/)) { ic = substr(seg, RSTART, RLENGTH); sub(/^"icon": "/, "", ic); sub(/"$/, "", ic) }
    sub(/\/$/, "", ip)
    miss = ""
    if (ip == "") { miss = "?" }
    else {
      f = ip "/app.js.bin"
      if ((getline ln < f) <= 0) { miss = f } else { close(f) }
      if (miss == "" && ic != "") { g = ip "/" ic; if ((getline ln2 < g) <= 0) { miss = g } ; close(g) }
    }
    if (miss != "") { bad++; if (bad <= max) { ids = ids " " a; at = at " " miss } }
  }
}
END {
  printf "apps=%d\napps_bad=%d\napps_ids=%s\napps_at=%s\n", total, bad, ids, at
}
"""

# guest 侧要读的 cfg.json（含 device.tp）。路径由脚本内固定：两族固件都用这个。
_SCRIPT_SRC = r"""cfg=/etc/miniapp/resources/cfg.json
tp=$(sed -n 's/.*"tp" *: *"\([^"]*\)".*/\1/p' "$cfg" 2>/dev/null | head -1)
real=$(readlink -f "$tp" 2>/dev/null)
L=$(ls -l /proc/[0-9]*/fd 2>/dev/null)
# F1：**精确等值**匹配链接目标，别用子串 —— '/dev/input/event2' 不能命中
#     '/dev/input/event21'（设备数上两位数就会假 holder）。目标取 ' -> ' 之后那一列，
#     去掉内核给"仍被打开但已消失"节点的 ' (deleted)' 后缀。
h=$(echo "$L" | awk -v pat="$real" -v pat2="$tp" '
  BEGIN { n = 0
          if (pat != "") a[++n] = pat
          if (pat2 != "" && pat2 != pat) a[++n] = pat2 }
  /^\/proc\// { pid=$1; sub(/^\/proc\//,"",pid); sub(/\/fd:?$/,"",pid); next }
  { t = $0; sub(/^.* -> /, "", t); sub(/ \(deleted\)$/, "", t)
    for (i = 1; i <= n; i++) if (t == a[i]) { print pid; next } }
' | sort -u | while read p; do echo "$p:$(cat /proc/$p/comm 2>/dev/null)"; done)
echo "holder=$(echo "$h" | head -1)"
r=$(echo "$L" | awk '
  /^\/proc\// { pid=$1; sub(/^\/proc\//,"",pid); sub(/\/fd:?$/,"",pid); next }
  { t = $0; sub(/^.* -> /, "", t); sub(/ \(deleted\)$/, "", t)
    if (t ~ /^\/dev\/input\/event/) print pid }
' | sort -u | while read p; do printf '%s ' "$(cat /proc/$p/comm 2>/dev/null)"; done)
echo "readers=$r"
# F5：x5pro 那族的读者可能是 mouse0(mousedev)，不是 event* —— 单独出键，不混进 readers，
#     等实测确认后再决定灯在那一族怎么说话。
m=$(echo "$L" | awk '
  /^\/proc\// { pid=$1; sub(/^\/proc\//,"",pid); sub(/\/fd:?$/,"",pid); next }
  { t = $0; sub(/^.* -> /, "", t); sub(/ \(deleted\)$/, "", t)
    if (t ~ /^\/dev\/input\/mouse/) print pid }
' | sort -u | while read p; do printf '%s ' "$(cat /proc/$p/comm 2>/dev/null)"; done)
echo "mice=$m"
echo "tp=$tp"
cut -d' ' -f1 /proc/uptime | sed 's/^/uptime=/'
# F3：目录不存在时发 core=-，**不是 0** —— "没采到"绝不许显示成"平安"（#30）
if [ -d /userdisk/corefile ]; then
  echo "core=$(ls /userdisk/corefile 2>/dev/null | wc -l)"
else
  echo "core=-"
fi
df -k /state 2>/dev/null | tail -1 | awk '{print "state="$5}'
# 桥的识别**不能靠 pidof**：rv1106 族（a7pro/x7）客机里没有 pidof
#   `pidof perl` → `sh: pidof: not found`（rc=127）、`busybox pidof` → `applet not found`
# 旧写法在那里恒走 else → `bridge` 恒 `down`，后果是"桥换过 pid"永不亮、且
# `ui-starting` 那一支要求 `bridge_state == "up"` 而**永远进不去** → 起机期直接落
# `touch/no-bridge` 假故障（`37-verify-findings.md` §5 B(a)）。
# 改成单趟扫 /proc：先按 cmdline 认 touchbridge.pl，认不到再退回 comm=perl。
# ⚠ **必须再按 `comm=perl` 过滤一次**：脚本自己是被 `sh -c '<整段脚本>'` 发进去的，
# 于是**探针进程自己的 cmdline 里就写着 `touchbridge\.pl` 这个模式**（实测 a7pro 上
# `bridge=up:13041,13098,497` —— 13041/13098 是 sh 与 grep 自己，真正只有 497）。
# comm 是 `sh`/`grep` 的一律剔掉，只留 `perl`。
bp=$(grep -l 'touchbridge\.pl' /proc/[0-9]*/cmdline 2>/dev/null | sed 's|/proc/\([0-9]*\)/cmdline|\1|')
bp=$(for p in $bp; do [ "$(cat /proc/$p/comm 2>/dev/null)" = "perl" ] && printf '%s ' "$p"; done)
[ -n "$bp" ] || bp=$(grep -l '^perl$' /proc/[0-9]*/comm 2>/dev/null | sed 's|/proc/\([0-9]*\)/comm|\1|')
if [ -n "$bp" ]; then
  echo "bridge=up:$(echo $bp | tr ' ' ',')"
else
  echo "bridge=down"
fi
[ -e /dev/uinput ] || echo "uinput=missing"
[ -e /dev/input/by-path/axs_ts ] || echo "axs_ts=missing"
# ---- 决策 #75：抽屉首行要的两件事（虚拟热点 / 设备身份）都在这趟里取，别另起一轮 ----
# 虚拟热点：目录在 = 这包**配了** AP；再认 hostapd 进程（同 bridge 的做法，不依赖 pidof）。
if [ -d /root/.vm/ap ]; then
  hp=$(grep -l '^hostapd$' /proc/[0-9]*/comm 2>/dev/null | sed 's|/proc/\([0-9]*\)/comm|\1|')
  if [ -n "$hp" ]; then echo "ap=up:$(echo $hp | tr ' ' ',')"; else echo "ap=cfg"; fi
else
  echo "ap=off"
fi
# 设备身份：同 restore_license.sh 的取法（system_info.bin 里的 ME 号 / 32 位 hex），
# 但**只把尾号带回宿主**（全量 SN/UUID 不离开客机）；空值发 empty，文件不在发 na。
SI=""
for D in /data /userdata /userdisk; do
  if [ -f "$D/cfg/system_info.bin" ]; then SI="$D/cfg/system_info.bin"; break; fi
done
if [ -n "$SI" ]; then
  sn=$(grep -aoE 'ME[0-9]{6,}' "$SI" 2>/dev/null | head -1)
  uu=$(grep -aoE '[a-f0-9]{32}' "$SI" 2>/dev/null | head -1)
  if [ -n "$sn" ]; then echo "sn=ok:$(printf '%s' "$sn" | tail -c 5)"; else echo "sn=empty"; fi
  if [ -n "$uu" ]; then echo "uuid=ok:$(printf '%s' "$uu" | tail -c 5)"; else echo "uuid=empty"; fi
else
  echo "sn=na"
fi
# ---- 设备适配性体检（Q70）：注册表里列着、盘上却没有文件的应用 ----
# 症状是人点名那两条：「应用列表丢失图标」＋「部分应用启动黑屏」，同一条根因 —— bootStrap
# 拒绝安装比注册表**旧**的预设包（日志 `[PMS]install package(...) failed, res=4`），
# 注册项留下了、文件没落下，桌面照样把它画进列表。
# 三条判据都是实测换来的，改之前先读：
#   * **只认注册表里的 `installPath`**：装机通道分 `a/`（`miniapp_cli`／商店）与 `b/`（预设），
#     基座**逐族不同、而且不止一棵**（2026-10-19 逐型号实测到**三棵**）：
#     `/userdata/miniapp`（a6pro 22 条／a7pro 30 条都在这，但里面只有 2~3 个预设目录＋注册表）、
#     `/userdisk/secondary/miniapp`（同两份注册表里另外 18~27 条的落点）、
#     **`/userdisk/miniapp`（x5pro 的 24 条全在这一棵，前两棵根本没有注册表）**。
#     少了第三棵，x5pro 的体检会发 `apps=-` —— 把"有 24 条、且逐条都在盘上"说成
#     "这台没有可体检的对象"。猜路径一定错，所以按列表逐个 `[ -f ]`，谁先用谁。
#   * **图标文件名不是常量，也不是"两种"**：预设包 `app_icon.png`、商店包 `icon.png`、
#     服务类 `icon` 是**空串**（a6pro 22 条里 10 条为空），**x5pro 实测到第三种**
#     `question-tutor.png`。空串**不算问题**；写死 `app_icon.png` 会误报 10 条、
#     又会漏掉 `question-tutor.png` 那条真丢图标的。
#   * **一趟 awk 跑完、不 fork**：rv1106 的 busybox 没有 `stat`；`while read` 里每条 3 次
#     `sed` 实测 **1.0 s**（整段探针的预算才 0.754 s），改成 awk 的 `getline < 文件` 判存在，
#     同一台实测 **<1 s**。`$1` 那个覆盖入口是给**离线夹具**留的，生产里没人传。
R=""
for p in /userdata/miniapp/data/mini_app/pkg/packages.json /userdisk/secondary/miniapp/data/mini_app/pkg/packages.json /userdisk/miniapp/data/mini_app/pkg/packages.json; do
  [ -f "$p" ] && R="$p" && break
done
[ -n "$1" ] && [ -f "$1" ] && R="$1"
if [ -z "$R" ]; then
  echo "apps=-"                       # 没这份注册表 = **没采到**，不是"0 个应用没事"（#30/F3）
else
  awk -v max=12 '""" + _APPS_AWK + r"""' "$R"
fi
"""

# **发出去的脚本里不留整行注释**（2026-10-19 复核实测到的自我污染）：走**串口**那一通道时，
# 客机 shell 会把这段脚本**逐行回显**进 `vm/serial.log`（日志里每条前面带 `> `）。注释占
# 载荷 **45.1%**（3307 / 7337 B）—— 等于每轮往日志里灌 3 KB 我自己的话，而 `rules.py` 的
# `serial_state_error` 读的**正是同一份日志**：今天没撞上（脚本里没有 `### state: unavailable`），
# 但"判据读的文件被判据自己的载体污染"这类坑不该靠运气躲。理由留在源码里（就在下面），
# 发给客机的这份按整行 `#` 剔掉。行尾那种 `echo …  # 说明` 是合法 shell 注释，不动。
def _strip_comment_lines(src: str) -> str:
    return "\n".join(ln for ln in src.split("\n")
                     if not ln.lstrip().startswith("#"))


_SCRIPT = _strip_comment_lines(_SCRIPT_SRC)

# 整段当一条命令发进去：guest 侧不需要任何额外文件，也绕开所有引号转义。
#
# ⚠ **不用 base64**：**a6pro** 的客机里 `base64` 是 busybox applet 中**唯一没做符号链接**的
# 一个 —— 实测 `command -v base64` 空、`/bin/base64` 与 `/usr/bin/base64` 都不存在，而
# sed/awk/grep/ls/readlink/cat/df/wc/head/tail/tr/cut/sort **全在**。于是
# `echo <base64> | base64 -d | sh` 在那台上整段哑掉（`sh: base64: not found`、stdout 空、
# rc=0）→ 一轮探针**一个键都回不来**，状态栏从此只剩 `—`。
# 改成 `sh -c '<单引号转义后的脚本>'`，已在**同一台 a6pro** 上复验拿到全部键。
#
# ⚠ **族名要写准**（千问 `43-rv1106-base64-attribution.md` 的更正）：rv1106 有**三个**包，
# `base64` 缺失**只在 a6pro 上实测到**；**a7pro 不缺**（修前 `_qwen_v5_a7pro.out` 用旧传送
# 就拿到了 `holder='605:miniapp'`、`bridge='up:499'`）；**x7 谁都没测**。
# 别把这条当"rv1106 全体"，也别去 a7pro 找一个不存在的坑。
def _shq(s: str) -> str:
    """POSIX 单引号转义（`'` → `'\\''`），把脚本原样交给 `sh -c`。"""
    return "'" + s.replace("'", "'\\''") + "'"


_CMD = "sh -c " + _shq(_SCRIPT)

KEYS = ("holder", "readers", "mice", "tp", "uptime", "core", "state", "bridge",
        "uinput", "axs_ts", "ap", "sn", "uuid",
        "apps", "apps_bad", "apps_ids", "apps_at")

# transport(cmd, timeout) -> (rc, output, channel)，channel ∈ {"ssh", "serial", "none"}
Transport = Callable[..., tuple]


@dataclass(frozen=True)
class Probe:
    """一轮探针的结果。``values`` 只含脚本能取到的键（取不到的键不出现）。"""
    values: dict = field(default_factory=dict)
    channel: str = "none"                 # ssh | serial | none
    error: str = ""

    @property
    def ok(self) -> str:
        """``probe_ok`` 三态（决策 #26）。"""
        return {"ssh": "ok", "serial": "serial", "none": "no-shell"}.get(self.channel,
                                                                        "no-shell")


def script() -> str:
    return _SCRIPT


def apps_awk() -> str:
    """应用体检那段 awk 程序（单测拿它在宿主上跑临时注册表）。"""
    return _APPS_AWK


def command() -> str:
    """可直接交给 ``dpctl sh -c`` / SSH 的那条命令。"""
    return _CMD


def parse(text: str) -> dict:
    """把探针输出解析成 dict；只看第一层 ``key=value``，未知键丢弃。

    同一键出现多次时**取第一次**（脚本对 holder/readers 各只发一行，多行是异常）。
    """
    out: dict = {}
    for line in text.replace("\r", "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in KEYS and key not in out:
            out[key] = value.strip()
    return out


def collect(transport: Transport, timeout: int = 20) -> Probe:
    """跑一轮探针。transport 决定走 SSH 还是串口，并把通道名回传（供 ``probe_ok``）。"""
    try:
        rc, out, channel = transport(_CMD, timeout)
    except TypeError:                                    # 简易 transport：只返回 (rc, out)
        rc, out = transport(_CMD, timeout)
        channel = "ssh" if rc == 0 else "none"
    except Exception as e:                                # 传输层自己炸了：别让它带走界面
        return Probe(channel="none", error=str(e))
    if channel == "none":
        return Probe(channel="none", error=out or "no transport")
    values = parse(out)
    if not values:
        # **一个键都没解析出来** = 没拿到数据。别把它当成功：起机期 SSH 连不上时就是
        # 这个形状（rc=255，stderr 只有一行警告文本，stdout 空）。"没采到"既不许显示成
        # 平安、**也不许显示成故障** —— 这是 #30/F3 的反半条（`37-verify-findings.md` §4）。
        return Probe(channel=channel,
                     error=(out or "").strip()[:200] or "probe returned no values")
    return Probe(values=values, channel=channel)


# --------------------------------------------------------------------------- #
# 取值器：把 key=value 变成判据要的形状；取不到一律返回 None
# --------------------------------------------------------------------------- #
def holder_pid(values: dict) -> Optional[tuple[int, str]]:
    """``holder=894:weston`` -> (894, "weston")；空 -> None。"""
    raw = (values.get("holder") or "").strip()
    if not raw or ":" not in raw:
        return None
    pid, _, comm = raw.partition(":")
    try:
        return int(pid), comm.strip()
    except ValueError:
        return None


def readers(values: dict) -> list[str]:
    return [x for x in (values.get("readers") or "").split() if x]


def mice(values: dict) -> list[str]:
    """持有 `/dev/input/mouse*` 的进程 —— x5pro 那族的 UI 光标走这里（F5）。"""
    return [x for x in (values.get("mice") or "").split() if x]


def ui_readers(values: dict) -> list[str]:
    """除触摸桥之外的读者 —— 用来分"笔的界面还没接上"与"真的没人打开"。

    桥的进程名固定是 `perl`；`readers` 里一旦出现别的名字，说明合成器或笔的 UI 已经
    接上了。实测（x6pro，2026-10-18）：起机 ~38 s 时只有 `perl`，~185 s 时是 `perl weston`，
    中间那段 `holder` 为空 —— 那是**在起**，不是故障。
    """
    return [x for x in readers(values) if x != "perl"]


def bridge_state(values: dict) -> str:
    """``bridge=up:529`` -> "up"；``bridge=down`` -> "down"；没有 -> "?"。"""
    raw = (values.get("bridge") or "").strip()
    if not raw:
        return "?"
    return raw.split(":", 1)[0]


def bridge_pids(values: dict) -> list[int]:
    raw = (values.get("bridge") or "").strip()
    if not raw.startswith("up:"):
        return []
    out = []
    for part in raw[3:].split(","):
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def uptime_s(values: dict) -> Optional[float]:
    try:
        return float((values.get("uptime") or "").strip())
    except ValueError:
        return None


def core_count(values: dict) -> Optional[int]:
    try:
        return int((values.get("core") or "").strip())
    except ValueError:
        return None


def state_pct(values: dict) -> Optional[int]:
    m = re.match(r"^(\d+)%$", (values.get("state") or "").strip())
    return int(m.group(1)) if m else None


def touches_available(values: dict) -> bool:
    """四环里的第 2 环：桥确实造出了节点（`/dev/uinput` 与 `by-path/axs_ts` 都在）。"""
    return values.get("uinput") != "missing" and values.get("axs_ts") != "missing"


def ap_state(values: dict) -> str:
    """虚拟热点三态（决策 #75）：``up`` / ``cfg``（配了但 hostapd 没起来）/ ``off`` / ``?``。

    `off` 是**包配置**的结果（`pack.toml` 的 `ap=false`）：那时「配置网络」页会永远转不完
    （千问实测 `scan_results` 0 个 AP / `wpa_cli scan` → FAIL-BUSY）。所以这一格不是报错，
    是**解释**：界面要说"这包没开虚拟热点"，而不是让人去猜网络坏了。
    """
    raw = (values.get("ap") or "").strip()
    if not raw:
        return "?"
    return raw.split(":", 1)[0]


def app_adapt(values: dict) -> Optional[tuple]:
    """适配性体检 ``(总数, 缺文件的条数, [appid…], [落点…])``；**注册表没读到 → None**。

    三态的根在 `apps=-`（客机里没有那份注册表 / 还没装过任何应用）：那时**整块都不判**，
    绝不能把"没采到"印成"0 个应用，平安"（#30/F3 同一条）。
    """
    raw = (values.get("apps") or "").strip()
    try:
        total = int(raw)
        bad = int((values.get("apps_bad") or "").strip())
    except ValueError:
        return None                     # 缺键、`-`、或形状不认识
    return (total, bad,
            [x for x in (values.get("apps_ids") or "").split() if x],
            [x for x in (values.get("apps_at") or "").split() if x])


def identity_tail(values: dict) -> tuple[Optional[str], Optional[str]]:
    """设备身份尾号 ``(sn, uuid)``。

    三态分得开（#30/#75）：``None`` = 这轮**没采到**（不判）；``""`` = 采到了、但设备里就是**空的**
    （进应用会要求激活的第一候选原因之一）；其余 = **尾号**（全量 SN/UUID 从不回宿主）。
    """
    out = []
    for key in ("sn", "uuid"):
        raw = (values.get(key) or "").strip()
        if not raw or raw == "na":
            out.append(None)
        elif raw == "empty":
            out.append("")
        elif raw.startswith("ok:"):
            out.append(raw[3:])
        else:
            out.append(None)          # 形状不认识 → 当"没采到"，别硬猜
    return out[0], out[1]
