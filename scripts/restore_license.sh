#!/bin/sh
# restore_license.sh v5 — 有道词典笔激活凭证恢复(服务器直取版)
# v5: cfg/applog 目录自动探测(/data|/userdata|/userdisk 三种批次);
#     分辨率从最新 DictPen 日志的 DRMNativeWindow::setSize 提取。
# v4: client 参数动态判定(不再硬编码 orange),逻辑复刻自
#     libyddal_business.so upload_crash_api_monitor @0xe1d18 的机型映射链
#
# 流程:自动探测 cfg/applog 挂载点,采集 SN/UUID/SKU/model/版本/分辨率,
#       直接请求有道 activation/v2/verify 接口获取最新 license,
#       校验后写回 license 并重启 miniapp。
# 前提:设备已联网且在服务器端注册过(首次激活过的机器)。
# 用法:root 执行  sh restore_license.sh

# ---------- 0. 目录探测(不同批次固件挂载点不同:/data|/userdata|/userdisk)----------
for D in /data /userdata /userdisk; do
    if [ -f "$D/cfg/system_info.bin" ]; then CFG_DIR=$D/cfg; LOGDIR=$D/applog; break; fi
done
CFG_DIR=${CFG_DIR:-/data/cfg}
LOGDIR=${LOGDIR:-/data/applog}
LIC_FILE=$CFG_DIR/license
SYSINFO=$CFG_DIR/system_info.bin
EXPECT_LEN=174                      # 凭证标准长度,0=不校验长度
VERIFY_URL=https://fanyidan.youdao.com/fanyidanapi/activation/v2/verify

[ "$(id -u)" = "0" ] || { echo "[!] 请用 root 执行"; exit 1; }
[ -f "$SYSINFO" ] || { echo "[!] 未找到 $SYSINFO"; exit 1; }
echo "[i] cfg=$CFG_DIR applog=$LOGDIR"

# ---------- 1. 采集设备参数 ----------
SN=$(strings "$SYSINFO"   | grep -oE '^ME[0-9]+'            | head -1)
UUID=$(grep -aoE '[a-f0-9]{32}' "$SYSINFO"                  | head -1)
SKU=$(strings "$SYSINFO"  | grep -oE '^OVERHEAD_[A-Z0-9_-]+'| head -1)
MODEL=$(strings "$SYSINFO" | grep -oE '^YDP[A-Z0-9_-]+'     | head -1)
VER=$(strings "$SYSINFO"   | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' | head -1)

# MAC:取第一个非全零网卡地址并 URL 编码冒号
for IF in /sys/class/net/*/address; do
    M=$(cat "$IF" 2>/dev/null | tr -d ':'); [ -n "$M" ] || continue
    [ "$M" = "000000000000" ] || { MAC_RAW=$(cat "$IF" | tr 'a-f' 'A-F'); break; }
done
[ -n "$SN" ] && [ -n "$UUID" ] && [ -n "$MAC_RAW" ] || {
    echo "[!] 参数采集失败 sn=$SN uuid=$UUID mac=$MAC_RAW"; exit 1; }

KERNEL=$(uname -r)

# 分辨率:从最新 DictPen 日志的 DRMNativeWindow::setSize(w,h) 提取
LASTLOG=$(ls -t "$LOGDIR"/DictPen_*.log 2>/dev/null | head -1)
[ -n "$LASTLOG" ] || LASTLOG=$(ls -t "$LOGDIR"/*.log 2>/dev/null | head -1)
SCREEN=$(grep -ahoE 'DRMNativeWindow::setSize\([0-9]+,[0-9]+\)' "$LASTLOG" 2>/dev/null \
         | tail -1 | sed 's/.*(\([0-9]*\),\([0-9]*\))/\1x\2/')
SCREEN=${SCREEN:-960x266}
echo "[i] screen=$SCREEN ${LASTLOG:+(来自 $(basename "$LASTLOG"))}"

echo "[i] SN=$SN SKU=$SKU MODEL=${MODEL:-unknown} MAC=$MAC_RAW"

# ---------- 1.5 判定 client ----------
# 原生判定链:HAL get_sku → 平台枚举 → 硬编码平台名(adapter_core @0x505d4);
# 此处用机型字符串直接映射(business.so @0xe1d18 同源实现,34 条配对逐条反汇编解出)。
get_client() {
    # ① 日志校准:真实 verify 请求会打印完整 URL;白名单+行尾锚定防截断脏数据
    c=$(grep -ahoE 'client=[a-z0-9_-]+' $LOGDIR/*.log* 2>/dev/null \
        | grep -E '=(orange|popcorn|coco|exam|cherry|kiwi|mango|y24|almond|plum|melon|olive|peach|pear|y02-1|y03|y05|y06|y07|y08|y09|y09p|y11|apollo|hermes|hermes-plus|x3s|d2|d3|p6)$' \
        | tail -1 | cut -d= -f2)
    case "$c" in [a-z][a-z0-9_-]*) echo "$c"; return;; esac
    # ② 机型映射表
    case "$MODEL" in
        YDPX5-1|YDPX5-2|YDPX5-3)            c=coco;;
        YDPP5-1)                             c=almond;;
        YDL011|YDL012|YDL013)                c=apollo;;
        YDP032|YDP038)                       c=x3s;;
        YDPS6|YDPS6-1)                       c=plum;;
        YDPX6-1|YDPX6-2)                     c=melon;;
        YDP031|YDP037)                       c=d3;;
        YDP021|YDP022)                       c=d2;;
        YDPP6-1)                             c=p6;;
        YDPX7-1)                             c=orange;;
        YDPX7-2)                             c=popcorn;;
        YDPX7-6)                             c=y02-1;;
        YDLE6P)                              c=hermes-plus;;
        YDPX7-3|YDPX7-5)                     c=y03;;
        YDPA7-1)                             c=y09;;
        YDPA7-2)                             c=y09p;;
        YDPR3-1)                             c=y11;;
        YDPS7-2)                             c=y08;;
        YDLE7P)                              c=y06;;
        YDLM7)                               c=y05;;
        YDPX7-7|YDPX7-8|YDPX7-9)             c=y07;;
        *)                                   c=orange;;   # 兜底:同平台默认
    esac
    echo "$c"
}
CLIENT=$(get_client)
echo "[i] client=$CLIENT"

# ---------- 2. 请求服务器 ----------
RESP=$(curl -sk --max-time 20 --get "$VERIFY_URL" \
    --data-urlencode "code=$SN" \
    --data-urlencode "mac=$MAC_RAW" \
    --data-urlencode "uuid=$UUID" \
    --data-urlencode "product=dictpen" \
    --data-urlencode "appVersion=${VER:-4.8.7}" \
    --data-urlencode "client=$CLIENT" \
    --data-urlencode "deviceSn=$SN" \
    --data-urlencode "mid=Linux$KERNEL" \
    --data-urlencode "screen=$SCREEN" \
    --data-urlencode "model=${MODEL:-YDPX7-1}" \
    --data-urlencode "imei=$SN" \
    --data-urlencode "deviceSku=$SKU" 2>/dev/null)

echo "$RESP" | grep -q '"code":0' || { echo "[x] 服务器校验失败: $RESP"; exit 1; }

LIC=$(printf '%s' "$RESP" | sed 's/.*"license":"//; s/".*//')
case "$LIC" in 1:*) ;; *) echo "[x] 应答中无有效 license: $RESP"; exit 1;; esac
if [ "$EXPECT_LEN" != "0" ] && [ ${#LIC} != "$EXPECT_LEN" ]; then
    echo "[x] license 长度(${#LIC})异常,期望 $EXPECT_LEN"; exit 1
fi
echo "[i] 获取 license 成功(${#LIC}B): $(printf '%s' "$LIC" | head -c 50)..."

# ---------- 3. 备份并写入 ----------
[ -f "$LIC_FILE" ] && cp "$LIC_FILE" "$LIC_FILE.bak.$$"
printf '%s' "$LIC" > "$LIC_FILE" || { echo "[!] 写入失败"; exit 1; }
chown root:root "$LIC_FILE"; chmod 640 "$LIC_FILE"
echo "[i] 已写入 $(wc -c < "$LIC_FILE") 字节 -> $LIC_FILE"

# ---------- 4. 重启并验证 ----------
kill -9 $(pidof miniapp) 2>/dev/null
echo "[i] 等待看门狗拉起 miniapp..."
sleep 12
if [ ! -f "$LIC_FILE" ]; then
    echo "[x] 失败:license 被应用判定无效删除,请改走扫码激活"; exit 1
fi
NEWLOG=$(ls -t $LOGDIR/*.log 2>/dev/null | head -1)
BAD=$(grep -ac 'license verify failed' "$NEWLOG" 2>/dev/null)
if [ "$BAD" = "0" ]; then
    echo "[✓] 成功:本地校验通过,激活正常 ($NEWLOG)"
else
    echo "[x] 失败:新日志仍有 $BAD 条 verify failed"
    exit 1
fi
