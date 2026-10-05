#!/bin/sh
# write_system_info.sh — 把 SN / UUID / MODEL / SKU 写进 400 字节定长的
# /userdata/cfg/system_info.bin。
#
# 用法:
#   sh write_system_info.sh -s <SN> -u <UUID> [-m MODEL] [-k SKU] [-M MAC]
#
# SN   : 20 字节定长槽，以 NUL 结尾（vendor 用 `^ME[0-9]+` 正则抽取）。
#        例：ME8D7E0F1A2B3C4D5E6F
# UUID : 32 字节定长槽（libydhal_devices.so get_uuid: `mov r2,#0x20` = 32）。
#        32 位小写 hex，例：0123456789abcdef0123456789abcdef
# MODEL: YDP 前缀字符串，例：YDPA7-1（决定激活服务器返回的 client 名）。
# SKU  : OVERHEAD_ 前缀字符串，例：OVERHEAD_A71_SKU_CHN_PRO
# MAC  : 18 字节定长槽（02:00:00:00:00:00 占位），可省略保持原值。
#
# 各字段在 400 字节结构里的偏移（a7pro 4.13.0 实测，从 guest 内的
# /userdata/cfg/system_info.bin 反推）：
#
#   0x00  u32   magic = 1
#   0x04  [18]  MAC "02:00:00:00:00:00"
#   0x15  [42]  SN（全零槽，ME 开头）
#   0x3B  [12]  固件版本 "4.13.0"
#   0x48  [18]  未用（含两个 u32：0x51=8, 0x55=16，估计是 mem/flash 大小）
#   0x59  [85]  MODEL + SKU 区：MODEL 在 0x59，SKU 紧跟其后
#   0xB6  [50]  内核版本 "Linux 6.1.0-50-arm64"
#   0xCC  [42]  未用（保留）
#   0xF6  [14]  屏幕 "800*254"（注意：a7pro 真实面板是 640*172，此值不准）
#   0x100 [160] UUID + 尾部保留
#
# 注意：strides 不均匀（55 / 123 / 64），不是简单的定步长数组；槽位边界由
# 现有内容的 NUL 结尾对齐，反推得到。UUID 槽（32 字节）放在 0x100，是因为
# get_uuid 从 system_info.bin 里提取 32 位 hex 作为最后一步——0x100 之后
# 的 160 字节是唯一足够大的连续全零区，且 restore_license.sh 的
# `grep -aoE '[a-f0-9]{32}'` 要求整段连续无 NUL。

set -e

SN=""
UUID=""
MODEL=""
SKU=""
MAC=""

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SN=$2; shift 2 ;;
        -u) UUID=$2; shift 2 ;;
        -m) MODEL=$2; shift 2 ;;
        -k) SKU=$2; shift 2 ;;
        -M) MAC=$2; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "[!] unknown arg: $1"; exit 2 ;;
    esac
done

SYSINFO=/userdata/cfg/system_info.bin
TMP=/tmp/system_info.$$.bin
# 之前一次未清掉的临时文件（崩溃时可能残留），先清掉，免得 $TMP 撞上
rm -f /tmp/system_info.*.bin 2>/dev/null

# ---------- 校验 ----------
[ -f "$SYSINFO" ] || { echo "[!] 未找到 $SYSINFO"; exit 1; }

size=$(wc -c < "$SYSINFO")
[ "$size" -eq 400 ] || { echo "[!] $SYSINFO 是 $size 字节，不是 400"; exit 1; }

[ -n "$SN" ] || { echo "[!] 缺 -s SN"; exit 2; }
[ -n "$UUID" ] || { echo "[!] 缺 -u UUID"; exit 2; }

# SN: ME 开头 + 至少 4 位数字
case "$SN" in
    ME[0-9]*) ;;
    *) echo "[!] SN 必须是 ME + 数字开头（现在: $SN）"; exit 2 ;;
esac
[ ${#SN} -ge 6 ] || { echo "[!] SN 太短: $SN"; exit 2; }
[ ${#SN} -le 20 ] || { echo "[!] SN 太长（>20 字节槽）: ${#SN}"; exit 2; }

# UUID: 32 位小写 hex
printf '%s' "$UUID" | grep -qE '^[a-f0-9]{32}$' \
    || { echo "[!] UUID 必须是 32 位小写 hex（现在: $UUID）"; exit 2; }

[ -z "$MODEL" ] || printf '%s' "$MODEL" | grep -qE '^YDP[A-Z0-9_-]+$' \
    || { echo "[!] MODEL 必须是 YDP 开头（现在: $MODEL）"; exit 2; }
[ -z "$SKU" ] || printf '%s' "$SKU" | grep -qE '^OVERHEAD_[A-Z0-9_-]+$' \
    || { echo "[!] SKU 必须是 OVERHEAD_ 开头（现在: $SKU）"; exit 2; }

# ---------- 布局 ----------
# 偏移写成十进制（busybox dd 的 seek= 只接受十进制，不认 0x 前缀）。
# LEN 是**槽位长度**，也就是 set_field 里 dd count= 要清掉的字节数；
# 槽位末尾必须留 1 字节 NUL 分隔（vendor 用 ^ 锚定的正则按 strings 记录抽字段）。
#
# 各字段之间的 NUL 是前一个字段的槽位尾巴，不是当前字段的头——所以
# 清当前槽位时**不能**碰掉前一个字段的 NUL，否则 strings 会把两个字段
# 连成一条记录，`grep -oE '^…'` 就抽不到当前字段。具体到 SN：
# MAC "02:00:00:00:00:00" 占 0x04..0x13，它的 NUL 在 0x14；SN 槽从 0x15
# 起、但 0x15 是 MAC 的 NUL 位，所以 SN 真正能写的起点是 0x16（22），
# 清槽的起点也只能从 0x16 开始，长度相应减 1。
SN_OFF=22;     SN_LEN=41   # 0x16..0x3A，留出 0x15 给 MAC 的 NUL
FW_OFF=59;     FW_LEN=12   # 0x3B..0x46  （"4.13.0" 占 7 字节，余 NUL）
MODEL_OFF=89;  MODEL_LEN=85  # 0x59..0xB5  （MODEL + SKU 共 85 字节）
SKU_OFF=113;                  # 0x71，MODEL 最多 23 字节 + 1 NUL
KERNEL_OFF=182; KERNEL_LEN=50 # 0xB6..0xCB  （"Linux 6.1.0-50-arm64" 占 20 字节）
SCREEN_OFF=246; SCREEN_LEN=14 # 0xF6..0xFD  （"800*254" 占 8 字节）
UUID_OFF=256;  UUID_LEN=32  # 0x100..0x11F  （只清 UUID 那 32 字节，别越界）
MAC_OFF=4;     MAC_LEN=17   # 0x04..0x14，含 NUL 结尾

# ---------- 写入辅助 ----------
# set_field <file> <offset> <length> <value> [no-null]
#
# 默认槽位要留 1 字节 NUL 作为分隔符（vendor 用 `strings | grep -oE '^…'`
# 抽字段，NUL 就是记录边界）。UUID 例外：它是 32 位 hex 恰好占满 32 字节槽，
# 不需要 NUL 结尾（restore_license.sh 用 `grep -aoE` 直接按内容匹配）。
set_field() {
    f=$1 off=$2 len=$3 val=$4 nulls=${5:-yes}
    vlen=${#val}
    if [ "$nulls" = "yes" ]; then
        # 留 1 字节给 NUL
        [ "$vlen" -le $((len - 1)) ] || {
            echo "[!] 字段 @$off 的值太长（$vlen 字节，槽 ${len}-1）" >&2
            exit 3
        }
    else
        [ "$vlen" -le "$len" ] || {
            echo "[!] 字段 @$off 的值太长（$vlen 字节，槽 $len）" >&2
            exit 3
        }
    fi
    # 清槽：先把整段填 NUL
    dd if=/dev/zero bs=1 count="$len" of="$f" seek="$off" conv=notrunc 2>/dev/null
    # 写入值（不含 NUL）
    printf '%s' "$val" | dd of="$f" bs=1 seek="$off" conv=notrunc 2>/dev/null
}

# ---------- 备份 + 复制 ----------
[ -f "$SYSINFO.pre-write.*" ] && rm -f "$SYSINFO.pre-write."*
cp -p "$SYSINFO" "$SYSINFO.pre-write.$$"
cp -p "$SYSINFO" "$TMP"

# ---------- 写各字段 ----------
if [ -n "$MAC" ]; then
    set_field "$TMP" "$MAC_OFF" "$MAC_LEN" "$MAC"
    echo "[i] MAC   @$MAC_OFF (0x$(printf %X $MAC_OFF)) <- $MAC"
else
    echo "[i] MAC   保持原值"
fi

set_field "$TMP" "$SN_OFF" "$SN_LEN" "$SN"
echo "[i] SN    @$SN_OFF (0x$(printf %X $SN_OFF)) <- $SN"

# 固件版本：保留原值（4.13.0），因为 vendor 会用它做机型判定
# 只清一次槽，不重写
dd if=/dev/zero bs=1 count="$FW_LEN" of="$TMP" seek="$FW_OFF" conv=notrunc 2>/dev/null
printf '%s' "4.13.0" | dd of="$TMP" bs=1 seek="$FW_OFF" conv=notrunc 2>/dev/null

# MODEL + SKU 合并在 0x59 起的 85 字节里
MODEL_VAL=${MODEL:-YDPA7-1}
SKU_VAL=${SKU:-OVERHEAD_A71_SKU_CHN_PRO}
set_field "$TMP" "$MODEL_OFF" "$MODEL_LEN" "$MODEL_VAL"
set_field "$TMP" "$SKU_OFF" $((MODEL_LEN - (SKU_OFF - MODEL_OFF))) "$SKU_VAL"
echo "[i] MODEL @$MODEL_OFF (0x$(printf %X $MODEL_OFF)) <- $MODEL_VAL"
echo "[i] SKU   @$SKU_OFF (0x$(printf %X $SKU_OFF)) <- $SKU_VAL"

# 内核版本：从 uname -r 取
KERNEL_VAL=$(uname -r | sed 's/^/Linux /')
set_field "$TMP" "$KERNEL_OFF" "$KERNEL_LEN" "$KERNEL_VAL"
echo "[i] kernel @$KERNEL_OFF (0x$(printf %X $KERNEL_OFF)) <- $KERNEL_VAL"

# 屏幕：从 DRMNativeWindow::setSize 取，兜底 800*254
LASTLOG=$(ls -t /userdata/applog/DictPen_*.log 2>/dev/null | head -1)
SCREEN_VAL=$(grep -ahoE 'DRMNativeWindow::setSize\([0-9]+,[0-9]+\)' "$LASTLOG" 2>/dev/null \
         | tail -1 | sed 's/.*(\([0-9]*\),\([0-9]*\))/\1*\2/')
[ -z "$SCREEN_VAL" ] && SCREEN_VAL="640*172"
set_field "$TMP" "$SCREEN_OFF" "$SCREEN_LEN" "$SCREEN_VAL"
echo "[i] screen @$SCREEN_OFF (0x$(printf %X $SCREEN_OFF)) <- $SCREEN_VAL"

# UUID（32 字节占满槽位，不需要 NUL 结尾）
set_field "$TMP" "$UUID_OFF" "$UUID_LEN" "$UUID" no-null
echo "[i] UUID  @$UUID_OFF (0x$(printf %X $UUID_OFF)) <- $UUID"

# ---------- 校验结果 ----------
echo "=== 校验 ==="
echo "size: $(wc -c < "$TMP")"
strings -n 3 "$TMP" | head -20
echo "SN extract:  $(strings "$TMP" | grep -oE '^ME[0-9]+' | head -1)"
echo "UUID extract: $(grep -aoE '[a-f0-9]{32}' "$TMP" | head -1)"
echo "SKU extract:  $(strings "$TMP" | grep -oE '^OVERHEAD_[A-Z0-9_-]+' | head -1)"
echo "MODEL extract: $(strings "$TMP" | grep -oE '^YDP[A-Z0-9_-]+' | head -1)"

# ---------- 替换 ----------
cp -f "$TMP" "$SYSINFO"
chown root:root "$SYSINFO"
chmod 644 "$SYSINFO"
rm -f "$TMP"
echo "[i] 已写入 $SYSINFO"
