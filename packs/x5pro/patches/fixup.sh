# x5pro 机型钩子（initramfs /init 里、switch_root 之前 source，POSIX sh）。
# 每条都是"这台的固件在 VM 里怎么跑"的适配，逐条注明原因；取证见本包 README.md。
# 参考同族 s6pro/s7pro 包；2026-10-05 起与那两台同一套打法。

# --- 1. inittab：串口永远可进 --------------------------------------------------
# rootfs 不带 dropbear（SSH 进不去），厂商 inittab 也没在 console 上起 shell
# —— 串口只有回显没有输入。追加一行 respawn shell，让串口永远可进。
if [ -n "$TARGET" ] && [ -f "$TARGET/etc/inittab" ]; then
    if ! grep -q 'respawn:-/bin/sh' "$TARGET/etc/inittab"; then
        echo '::respawn:-/bin/sh' >> "$TARGET/etc/inittab"
        echo "pack hook fixup: console respawn shell added"
    fi
    # 厂商 getty 的 login 需要密码（shadow 未知）且会跟下面的 console-loop 抢
    # console 输入，VM 里用不上，注释掉。
    sed -i 's|^console::respawn:/sbin/getty|#pack: &|' "$TARGET/etc/inittab"
fi

# --- 2. 恢复被早期实验挪走的 OCR 识别模型 --------------------------------------
# 见包 README；真正的修复是桩化 libyocr/libYoudaoStitch（pack.toml [adapt.replace]，
# 2026-10-05），不再需要动模型文件。幂等：文件在就直接返回。
OcrModel="$TARGET/oem/YoudaoDictPen/output/ocr"
if [ -f "$OcrModel/hidden_cvimodel.bak" ]; then
    mv -f "$OcrModel/hidden_cvimodel.bak" \
          "$OcrModel/yd_rec3404_spp3x3_shared.v2.7.1.qs262.int8.cv182x.80w.cvimodel.encrypt" \
        && echo "pack hook fixup: ocr model restored"
fi

# --- 3. TCP adb（rootfs 自带 adbd；注意这族 adbd 是 android-tools 4.2.2，只走
#     USB gadget、不读 ADB_TCP_PORT —— s6pro 包实测 TCP 走不通。此钩子保留仅为
#     兜底，实际远程访问走下面的 arm64 dropbear SSH）。
if [ -x "$TARGET/usr/bin/adbd" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00adbd" <<'ADBEOF'
#!/bin/sh
# x5pro: TCP adb（rootfs 无 dropbear 时的兜底；这族 adbd 可能只走 USB）
mkdir -p /data/misc/adb
[ -f /data/misc/adb/adb_keys ] || : > /data/misc/adb/adb_keys
touch /tmp/.adb_auth_verified
if ! ps | grep -q '[a]dbd'; then
    ADB_TCP_PORT=5555 ADBD_SHELL=/bin/sh setsid /usr/bin/adbd >/tmp/adbd.log 2>&1 </dev/null &
fi
echo "S00adbd: $(ps | grep '[a]dbd' >/dev/null && echo up || echo failed)" >/dev/kmsg
ADBEOF
    chmod +x "$TARGET/etc/init.d/S00adbd"
    echo "pack hook fixup: S00adbd installed"
fi

# --- 4. console shell 走 rcS --------------------------------------------------
# 这台机器上 busybox init 在 rcS 结束后没有启动 inittab 的 respawn 条目（与
# s6pro/s7pro 同款），串口因此永远没有 shell。改由一个 rcS 钩子自拉自收。
if [ -d "$TARGET/etc/init.d" ] && [ -d "$TARGET/root" ]; then
    mkdir -p "$TARGET/root/.vm"
    cat > "$TARGET/root/.vm/console-loop" <<'CLOOP'
#!/bin/sh
# x5pro: console respawn shell（见 pack 钩子 fixup.sh 的说明）
echo "pack: console shell ready"
while :; do
    setsid sh </dev/console >/dev/console 2>&1
    sleep 1
done
CLOOP
    chmod +x "$TARGET/root/.vm/console-loop"
    cat > "$TARGET/etc/init.d/S51console-shell" <<'CSHEOF'
#!/bin/sh
# x5pro: start the console shell loop (once)
case "$1" in
  start)
    if ! ps | grep -q '[c]onsole-loop'; then
        setsid /root/.vm/console-loop >/dev/console 2>&1 </dev/console &
        echo "S51console-shell: loop started"
    fi
    ;;
esac
CSHEOF
    chmod +x "$TARGET/etc/init.d/S51console-shell"
    echo "pack hook fixup: S51console-shell installed"
fi

# --- 5. 静默真机诊断服务（run_uevent_monitor / run_input_event_monitor / runCapFrame）
# 这台它们的启动点在 S50launcher（框架按 a6pro 语义把 S50launcher 改名成
# S50launcher.disabled，rcS 仍会跑它）；不静默时 guardian 无限重启它们：串口被
# 刷屏到没法用，Capframe_*.log 高速增长（s7pro 实测 45 分钟 3GB）。行格式与 a6pro
# 相同（guardian_run_service，tab 缩进），按 S* 通配 + 幂等 sed。
for f in "$TARGET"/etc/init.d/S*; do
    [ -f "$f" ] || continue
    sed -i -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/runCapFrame[[:space:]]*$|\1: #pack: runCapFrame off|' \
           -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/run_uevent_monitor[[:space:]]*$|\1: #pack: uevent monitor off|' \
           -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/run_input_event_monitor[[:space:]]*$|\1: #pack: input monitor off|' \
           "$f" 2>/dev/null
done
echo "pack hook fixup: 诊断服务静默完成（grep '#pack:' 命中 $(grep -l '#pack:' "$TARGET"/etc/init.d/S* 2>/dev/null | wc -l) 个文件）"

# --- 6. applog 上限（兜底）----------------------------------------------------
# 诊断服务已静默；Capframe 类日志一旦再出现直接清，DictPen_*.log 留最新 10 份。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00applog-cap" <<'ALCAP'
#!/bin/sh
# x5pro: applog 上限（runCapFrame 已在 rcS 静默，这里清历史 + 兜底）
cd /userdata/applog 2>/dev/null || exit 0
rm -f Capframe_*.log 2>/dev/null
ls -t DictPen_*.log 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null
exit 0
ALCAP
    chmod +x "$TARGET/etc/init.d/S00applog-cap"
    echo "pack hook fixup: S00applog-cap installed"
fi

# --- 6b. mousedev 缩放 = 面板尺寸 ----------------------------------------------
# 这台 app 的 UI 光标 = /dev/input/mouse0 的增量（mousedev 字节流）。mousedev 把
# ABS 增量换算成像素用的 xres/yres 默认是 Debian 内核的 1024x768，与本机面板
# 800x254 不符 —— 光标每动一次偏 41 倍。参数运行时可写（实测），必须在 miniapp
# 起来之前写好。PANEL=800x254 来自 pack.env（框架注入）。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ] && [ -n "$PANEL" ]; then
    cat > "$TARGET/etc/init.d/S00mouse-scale" <<MSCALE
#!/bin/sh
# x5pro: mousedev 的 ABS->像素缩放对齐面板（app 光标走 mouse0 增量）
echo ${PANEL%x*} > /sys/module/mousedev/parameters/xres 2>/dev/null
echo ${PANEL#*x} > /sys/module/mousedev/parameters/yres 2>/dev/null
MSCALE
    chmod +x "$TARGET/etc/init.d/S00mouse-scale"
    echo "pack hook fixup: S00mouse-scale installed (PANEL=$PANEL)"
fi

# --- 7. perl（触摸桥运行时；rootfs 无 perl）------------------------------------
# 框架的 touchbridge 钩子硬编码 /usr/bin/perl。素材来自 s6pro/s7pro 包的 perl-tree
# （Debian armhf perl-base 5.20，guest 内核的 COMPAT 层直接跑）。
if [ -d /patches/perl-tree/usr ] && [ -d "$TARGET/usr" ]; then
    cp -r /patches/perl-tree/usr/bin/. $TARGET/usr/bin/
    cp -r /patches/perl-tree/usr/lib/arm-linux-gnueabihf/. $TARGET/usr/lib/arm-linux-gnueabihf/
    cp -r /patches/perl-tree/usr/share/. $TARGET/usr/share/ 2>/dev/null
    # vendor ld-2.23 不搜 multiarch 目录，libperl 再放一份到默认路径
    cp -f $TARGET/usr/lib/arm-linux-gnueabihf/libperl.so.5.20 $TARGET/usr/lib/
    chmod 755 $TARGET/usr/bin/perl
    # 树内模块目录是 5.20.2，perl 二进制的 @INC 找 5.20：链接过去。先删同名目录
    # 再 ln —— 目标若是空目录，`ln -sfn` 会把链接建进目录里而不是替换（s7pro 踩过）。
    rm -rf $TARGET/usr/lib/arm-linux-gnueabihf/perl/5.20 \
           $TARGET/usr/share/perl/5.20
    ln -sfn 5.20.2 $TARGET/usr/lib/arm-linux-gnueabihf/perl/5.20
    ln -sfn 5.20.2 $TARGET/usr/share/perl/5.20
    echo "pack hook fixup: perl (armhf) installed"
fi

# --- 8. arm64 dropbear（SSH，:22）--------------------------------------------
# rootfs 无 dropbear；旧的 nc 裸 shell（busybox64 nc -ll -e sh，占 :22）不是 SSH，
# dpctl 的 sh/put/get 用不了。装 Debian arm64 dropbear（guest 内核是 aarch64，直接
# 跑；authorized_keys 由框架 init 注入 /root/.ssh），dpctl 全链路走 SSH。
# 文件来自 patches/arm64-tree/（与 s7pro 包同一套，libc/loader + tomcrypt/tommath/gmp）。
if [ -d /patches/arm64-tree/lib ] && [ -d "$TARGET/lib" ]; then
    # 旧版 fixup 装过 S52telnetd（busybox64 nc 裸 shell，占 :22）——与 dropbear
    # 端口冲突，删掉（state 盘 upper 会把它留到下一次开机）。
    rm -f "$TARGET/etc/init.d/S52telnetd"
    mkdir -p $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu \
             $TARGET/usr/sbin $TARGET/usr/bin $TARGET/root/.ssh
    cp -rf /patches/arm64-tree/lib/aarch64-linux-gnu/. $TARGET/lib/aarch64-linux-gnu/
    cp -f /patches/arm64-tree/lib/ld-linux-aarch64.so.1 $TARGET/lib/ld-linux-aarch64.so.1
    # 注意必须是 -rf：busybox 的 cp 没有 -r 时对 "dir/." 报 "omitting directory"，
    # 库拷不进去、dropbear 缺 libtomcrypt 起不来（s7pro 实测踩过）。
    cp -rf /patches/arm64-tree/usr/lib/aarch64-linux-gnu/. $TARGET/usr/lib/aarch64-linux-gnu/
    cp -f /patches/arm64-tree/usr/bin/strace        $TARGET/usr/bin/strace
    cp -f /patches/arm64-tree/usr/sbin/dropbear     $TARGET/usr/sbin/dropbear
    cp -f /patches/arm64-tree/usr/bin/dropbearkey   $TARGET/usr/bin/dropbearkey
    cp -f /patches/arm64-tree/usr/bin/dbclient      $TARGET/usr/bin/dbclient
    chmod -R 755 $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu
    chmod 755 $TARGET/lib/ld-linux-aarch64.so.1 $TARGET/usr/bin/strace \
              $TARGET/usr/sbin/dropbear $TARGET/usr/bin/dropbearkey $TARGET/usr/bin/dbclient
    cat > "$TARGET/etc/init.d/S00dropbear" <<'DBEOF'
#!/bin/sh
# x5pro: arm64 dropbear（首靴生成 host key；authorized_keys 由框架 init 注入）
[ -x /usr/sbin/dropbear ] || exit 0
mkdir -p /root/.ssh
if [ ! -f /root/.ssh/dropbear_rsa_host_key ]; then
    /usr/bin/dropbearkey -t rsa -s 2048 -f /root/.ssh/dropbear_rsa_host_key \
        >/tmp/dropbearkey.log 2>&1
    echo "S00dropbear: host key generated" >/dev/kmsg
fi
if ! ps | grep -q 'usr/[s]bin/dropbear'; then
    LD_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu:/lib/aarch64-linux-gnu \
        setsid /usr/sbin/dropbear -p 22 -r /root/.ssh/dropbear_rsa_host_key \
        -s -g >/tmp/dropbear.log 2>&1 </dev/null &
    sleep 1
    ps | grep -q '[d]ropbear' \
        && echo "S00dropbear: up" >/dev/kmsg \
        || echo "S00dropbear: failed: $(tail -n1 /tmp/dropbear.log 2>/dev/null)" >/dev/kmsg
fi
DBEOF
    chmod +x "$TARGET/etc/init.d/S00dropbear"
    # inittab 里如果框架注入过 dropbear 行，这台用不上（rootfs 原本没有 dropbear）；
    # 由上面的 S00dropbear 统一负责。
    sed -i '/dropbear/d' "$TARGET/etc/inittab" 2>/dev/null
    echo "pack hook fixup: strace + dropbear (arm64) installed"
fi

# --- 9. 触摸参数：在 guest 阶段再覆盖一次 cfg.json（overlay 的 upper 会遮蔽）---
# 框架的 cfg.json.overrides 是在 initramfs 阶段改的，那时 $TARGET 是**只读根**
# （overlay 的 lower）。/etc/miniapp/resources/cfg.json 一旦被写进 upper
# （app 或系统组件保存过一次），overlay 合并时 **upper 优先**，框架的修改就被
# 遮蔽了 —— 实测：改 lower 后 app 读到的仍是旧值（tp_direction 三个取值
# 0/90/270 行为完全一样，就是这个原因）。
# So：这里生成一个 S00 钩子（在 app 由 S50 启动之前跑），对**合并视图**再应用
# 一次触摸参数。幂等（每次开机都写同样的值）。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00cfg-tp" <<'CFGTP'
#!/bin/sh
# x5pro: 触摸参数写进 overlay 的合并视图（upper 会遮蔽 initramfs 阶段改的 lower）
# tp_direction=0 是恒等变换；device.mouse 置空以去掉 mouse 链注入的假 DOWN(0,0)
# （见 packs/x5pro/patches/cfg.json.overrides 的注释与 README §6.7）
CFG=/etc/miniapp/resources/cfg.json
[ -f "$CFG" ] || exit 0
sed -i -e 's/"tp_direction": *[0-9]*/"tp_direction": 0/' \
       -e 's/"tp_xoffset": *[0-9]*/"tp_xoffset": 0/' \
       -e 's/"tp_yoffset": *[0-9]*/"tp_yoffset": 0/' \
       -e 's#"mouse":[[:space:]]*"[^"]*"#"mouse": ""#' "$CFG" 2>/dev/null
exit 0
CFGTP
    chmod +x "$TARGET/etc/init.d/S00cfg-tp"
    echo "pack hook fixup: S00cfg-tp installed"
fi

# --- 存储 shim / miniapp 包装器（Q106）-------------------------------------------
# 同 s6pro：新增文件经 [adapt.replace] 的 S00replace 装不上（只覆盖已存在目标），
# 这里直接落盘。shim 放 /usr/lib/vmstorage/ 子目录，避免客机加载器把顶层 .so
# 当缺失库候选。NMT no-op 一起带上：覆盖 getStorageInfo 后 loadNmtPatch 在
# localdict 缺失时会抛异常 abort。
if [ -f /patches/libvmstorage-armhf.so ] && [ -f /patches/miniapp-wrapper.sh ]; then
    mkdir -p "$TARGET/usr/lib/vmstorage"
    cp -f /patches/libvmstorage-armhf.so "$TARGET/usr/lib/vmstorage/libvmstorage-armhf.so"
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/lib/miniapp-wrapper.sh"
    chmod 755 "$TARGET/usr/lib/vmstorage/libvmstorage-armhf.so" \
              "$TARGET/usr/lib/miniapp-wrapper.sh"
    rm -f "$TARGET/usr/lib/libvmstorage-armhf.so"
    echo "pack hook fixup: armhf storage shim installed"
fi
