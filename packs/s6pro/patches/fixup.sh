# s6pro 机型钩子（initramfs /init 里、switch_root 之前 source，POSIX sh）。
# 与 a6pro 的框架钩子语义差异都在这里补齐：
#
# 1. 框架会把 S50launcher 改名禁用 —— 那是 a6pro 的语义（它启动应用）。
#    这台机器的 S50launcher 只拉起 hal 服务（wpa_supplicant / wifi-manager /
#    bluetoothd / bt-manager ...），应用由 S50-app-launcher 启动，必须改回来，
#    否则无线/蓝牙服务全都不起。
# 2. rootfs 无 dropbear：框架注入的 inittab dropbear 行只会每靴报 not found，
#    去掉；SSH 没有了，adb 改由 rcS 钩子用 rootfs 自带的 adbd 起 TCP
#    （guest 5555 <- 宿主 15037 的 hostfwd，与 dpctl 的约定一致）。
# 3. 这台机器的 vendor busybox init 和 x5pro 一样：rcS 结束后 inittab 的
#    respawn 条目不出现，串口没有 shell —— 用自拉自收的 console-loop 兜底。
# 4. 触摸：cfg.json 的 tp 指向 /dev/input/by-path/vm-tp（见 cfg.json.overrides），
#    这里把 QEMU Virtio Tablet 的 event 节点链过去（udev 的 by-path 要到 S10
#    才有，而 S00 钩子先跑，所以自己链）。

# --- 1. 恢复 hal 服务启动器 ---------------------------------------------------
if [ -n "$TARGET" ] && [ -f "$TARGET/etc/init.d/S50launcher.disabled" ] \
   && [ ! -f "$TARGET/etc/init.d/S50launcher" ]; then
    mv "$TARGET/etc/init.d/S50launcher.disabled" "$TARGET/etc/init.d/S50launcher" \
        && echo "pack hook fixup: S50launcher restored (hal services launcher)"
fi

# --- 2a. 静默三个真机诊断服务（VM 里只剩刷屏 + 白烧 TCG CPU）------------------
# run_uevent_monitor / run_input_event_monitor / runCapFrame 在 VM 内核上秒退，
# guardian_run 的 mode_waitrun 会无限重启它们（每秒各刷一屏 console，还会不停
# 重写 /data/applog 的日志）。它们是给真机抓 uevent/输入/崩溃帧用的，VM 里无意义。
if [ -n "$TARGET" ] && [ -f "$TARGET/etc/init.d/S50launcher" ]; then
    sed -i -e 's|^\t\{1,\}guardian_run_service /usr/bin/runCapFrame$|\t: #pack: runCapFrame off|' \
           -e 's|^\t\{1,\}guardian_run_service /usr/bin/run_uevent_monitor$|\t: #pack: uevent monitor off|' \
           -e 's|^\t\{1,\}guardian_run_service /usr/bin/run_input_event_monitor$|\t: #pack: input monitor off|' \
           "$TARGET/etc/init.d/S50launcher" \
        && echo "pack hook fixup: diagnostic services silenced in S50launcher"
fi

# --- 2b. 去掉无 dropbear 的 inittab 行 ----------------------------------------
if [ -n "$TARGET" ] && [ -f "$TARGET/etc/inittab" ]; then
    sed -i '/dropbear/d' "$TARGET/etc/inittab"
fi

# --- 2b. 藏掉 libyocr（OCR 栈会硬 assert）-------------------------------------
# miniapp 启动时初始化 OCR：libyocr → libcviruntime 的 CviDeviceMem::device_init
# 要打开 /dev/ion 与 /dev/cvi-tpu0（CVITEK TPU/ION 分配器）；QEMU 里没有这些
# 设备，device_init 直接 assert(0) abort（日志：
# "cvi_device_mem.cpp:528 ... Assertion `0' failed"），整个应用秒崩。
#
# 但 libyocr 不能简单改名了事：libjsapi_export.so / libyddal_business.so 的
# DT_NEEDED 拉着 libyocr.so，直接藏掉会让 dlopen 失败 → global 等 jsapi 起不来
# → UI 全黑。所以配合包里的两份补丁库（把 DT_NEEDED 原位改成 libdl.so.2，见
# pack.toml），这里把 libyocr 藏掉，让运行时的 OCR dlopen 走"模块不可用"路径
# （app 对模块 init 失败有成熟的容忍路径，实测稳定）。幂等：改名后 [-f] 不再命中。
if [ -n "$TARGET" ] && [ -f "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so" ]; then
    mv "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so" \
       "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so.off" \
        && echo "pack hook fixup: libyocr hidden (no CVITEK TPU in VM)"
fi


# --- 2e. perl（触摸桥运行时；rootfs 无 perl）---------------------------------
if [ -d /patches/perl-tree/usr ] && [ -d "$TARGET/usr" ]; then
    cp -r /patches/perl-tree/usr/bin/. $TARGET/usr/bin/
    cp -r /patches/perl-tree/usr/lib/arm-linux-gnueabihf/. $TARGET/usr/lib/arm-linux-gnueabihf/
    # vendor ld-2.23 不搜 multiarch 目录，libperl 再放一份到默认路径
    cp -f $TARGET/usr/lib/arm-linux-gnueabihf/libperl.so.5.20 $TARGET/usr/lib/
    chmod 755 $TARGET/usr/bin/perl
    echo "pack hook fixup: perl (armhf) installed"
else
    echo "pack hook fixup: perl skipped, /patches has: $(ls /patches/ 2>&1 | tr '
' ' ')"
fi

# --- 2c. SSH：Debian arm64 dropbear -------------------------------------------
# rootfs 无 dropbear、自带 adbd（android-tools 4.2.2）不支持 TCP（不读
# ADB_TCP_PORT，TCP adb 走不通），所以装一份 arm64 dropbear —— guest 内核是
# aarch64，Debian arm64 二进制直接能跑（与框架 hostapd 同一思路）。
# 文件来自 patches/dropbear-tree/（fetch-arm64.py 从 Debian bookworm 抓取）。
# 库放 /lib、/usr/lib 的 aarch64 多目录（Debian ld 内建搜索路径），loader 放
# /lib/ld-linux-aarch64.so.1（ELF INTERP 所指，与 armhf 的 ld-2.23 不同名不冲突）。
if [ -n "$TARGET" ] && [ -d /patches/dropbear-tree/lib ] && [ -d "$TARGET/lib" ]; then
    mkdir -p $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu \
             $TARGET/usr/sbin $TARGET/usr/bin $TARGET/root/.ssh
    cp -r /patches/dropbear-tree/lib/aarch64-linux-gnu/. $TARGET/lib/aarch64-linux-gnu/ \
        && cp -f /patches/dropbear-tree/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 \
                 $TARGET/lib/ld-linux-aarch64.so.1 \
        && cp -f /patches/dropbear-tree/usr/lib/aarch64-linux-gnu/libtomcrypt.so.1 \
                 /patches/dropbear-tree/usr/lib/aarch64-linux-gnu/libtommath.so.1 \
                 /patches/dropbear-tree/usr/lib/aarch64-linux-gnu/libgmp.so.10 \
                 $TARGET/usr/lib/aarch64-linux-gnu/ \
        && cp -f /patches/dropbear-tree/usr/sbin/dropbear $TARGET/usr/sbin/dropbear \
        && cp -f /patches/dropbear-tree/usr/bin/dropbearkey $TARGET/usr/bin/dropbearkey \
        && cp -f /patches/dropbear-tree/usr/bin/strace $TARGET/usr/bin/strace \
        && chmod -R 755 $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu \
             $TARGET/lib/ld-linux-aarch64.so.1 \
        && chmod 755 $TARGET/usr/sbin/dropbear $TARGET/usr/bin/dropbearkey \
                     $TARGET/usr/bin/strace \
        && chmod 755 $TARGET/usr/sbin/dropbear $TARGET/usr/bin/dropbearkey \
                     $TARGET/usr/bin/strace \
        && echo "pack hook fixup: dropbear (arm64) installed"
    cat > "$TARGET/etc/init.d/S00dropbear" <<'DBEOF'
#!/bin/sh
# s6pro: start the arm64 dropbear (key gen on first boot; authorized_keys is
# injected by the framework init before rcS)
[ -x /usr/sbin/dropbear ] || exit 0
mkdir -p /root/.ssh
if [ ! -f /root/.ssh/dropbear_rsa_host_key ]; then
    /usr/bin/dropbearkey -t rsa -s 2048 -f /root/.ssh/dropbear_rsa_host_key \
        >/tmp/dropbearkey.log 2>&1
    echo "S00dropbear: host key generated" >/dev/kmsg
fi
if ! ps | grep -q 'usr/[s]bin/dropbear'; then
    setsid /usr/sbin/dropbear -p 22 -r /root/.ssh/dropbear_rsa_host_key \
        -s -g >/tmp/dropbear.log 2>&1 </dev/null &
    sleep 1
    ps | grep -q '[d]ropbear' \
        && echo "S00dropbear: up" >/dev/kmsg \
        || echo "S00dropbear: failed: $(tail -n1 /tmp/dropbear.log 2>/dev/null)" >/dev/kmsg
fi
DBEOF
    chmod +x "$TARGET/etc/init.d/S00dropbear"
fi

# --- 2d. （TCP adb 不装：这台 adbd 是 android-tools 4.2.2，只走 USB gadget，
#          不读 ADB_TCP_PORT；远程访问统一走上面的 dropbear SSH）-----------------

# --- 3a. /tmp -> tmpfs（真实设备语义）------------------------------------------
# runDictPen 的崩溃计数写在 /tmp：真机 /tmp 是 tmpfs（每靴清零），而 VM 的 /
# 是 overlay、/tmp 落在状态盘上 —— 计数跨重启累积会引发循环 reboot。
# （runDictPen 已由本包整体替换，重启分支在替换版里删除；这里的 tmpfs 仍保留，
# 让 /tmp 的语义与真机一致。）
if [ -n "$TARGET" ] && [ -d "$TARGET/tmp" ]; then
    mount -t tmpfs -o size=64m tmpfs "$TARGET/tmp" 2>/dev/null \
        && echo "pack hook fixup: /tmp -> tmpfs"
fi

# --- 3b. 串口 shell（console-loop，同 x5pro 的做法）-----------------------------
# 只留一个读 console 的 shell：框架把 inittab 的 login 改成了 respawn sh，但这台
# vendor busybox init 在 rcS 后不启 respawn 条目（x5pro 实测同款问题）；而万一某次
# 起了，又会和 console-loop 两个进程抢串口输入（表现为命令回显被打碎）。所以这里
# 注释掉 respawn sh，console 提供方固定为 console-loop。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ] && [ -d "$TARGET/root" ]; then
    sed -i 's|^::respawn:-/bin/sh$|#pack: &|' "$TARGET/etc/inittab"
    mkdir -p "$TARGET/root/.vm"
    cat > "$TARGET/root/.vm/console-loop" <<'CLOOP'
#!/bin/sh
# s6pro: console respawn shell（vendor busybox init 在 rcS 后不启 respawn 条目）
echo "pack: console shell ready"
while :; do
    setsid sh </dev/console >/dev/console 2>&1
    sleep 1
done
CLOOP
    chmod +x "$TARGET/root/.vm/console-loop"
    cat > "$TARGET/etc/init.d/S51console-shell" <<'CSHEOF'
#!/bin/sh
# s6pro: start the console shell loop (once)
if ! ps | grep -q '[c]onsole-loop'; then
    setsid /root/.vm/console-loop >/dev/null 2>&1 </dev/null &
    echo "S51console-shell: console-loop started" >/dev/kmsg
fi
CSHEOF
    chmod +x "$TARGET/etc/init.d/S51console-shell"
    echo "pack hook fixup: console shell installed"
fi

# --- 3c. 系统配置种子 ----------------------------------------------------------
# sysconfig-manager 的存储是 /data/cfg/sys_config.conf；VM 里首次开机它拿到空
# 文件，之后每 20s 刷一遍 "sys cfg uninitialized or unexpected data" 且永远
# 不就绪（app 侧 sys_config 全是空值）。用固件自带的出厂默认值（defconfig.conf）
# 做种子 —— 只在文件缺失/为空时写，不覆盖用户后续改出来的状态。
if [ -n "$TARGET" ] && [ -f "$TARGET/usr/configs/defconfig.conf" ]; then
    mkdir -p "$TARGET/data/cfg"
    if [ ! -s "$TARGET/data/cfg/sys_config.conf" ]; then
        cp -f "$TARGET/usr/configs/defconfig.conf" "$TARGET/data/cfg/sys_config.conf" \
            && echo "pack hook fixup: sys_config.conf seeded from defconfig"
    fi
fi

# --- 4. 触摸设备链接 ------------------------------------------------------------
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00tp-link" <<'TPEOF'
#!/bin/sh
# s6pro: 把 cfg.json 的 tp（/dev/input/by-path/vm-tp）链到 virtio tablet。
# VM 里没有 hynitron I2C 触摸；rcS 的 S00 时 udev（S10）还没起，by-path 不存在，
# devtmpfs 的事件节点已经在，按名字从 sysfs 找到它再链。
mkdir -p /dev/input/by-path
dev=""
for d in /sys/class/input/input*/name; do
    [ "$(cat "$d" 2>/dev/null)" = "QEMU Virtio Tablet" ] || continue
    inp=${d%/name}
    for e in "$inp"/event*; do
        [ -e "$e" ] || continue
        ev=$(basename "$e")
        [ -e "/dev/input/$ev" ] && dev="/dev/input/$ev"
    done
done
if [ -n "$dev" ]; then
    ln -sf "$dev" /dev/input/by-path/vm-tp
    echo "S00tp-link: vm-tp -> $dev" >/dev/kmsg
else
    echo "S00tp-link: virtio tablet not found" >/dev/kmsg
fi
TPEOF
    chmod +x "$TARGET/etc/init.d/S00tp-link"
    echo "pack hook fixup: S00tp-link installed"
fi

# --- 9. mousedev 缩放 = 面板尺寸（触摸光标对齐，2026-10-06）---------------------
# 这台 app 的 UI 光标 = /dev/input/mouse0 的增量（mousedev 字节流），点击发生在
# 光标当前位置。mousedev 把 ABS 增量换算成像素用的 xres/yres 默认是 Debian 内核的
# 1024x768，与本机面板 640x260 不符 —— 光标每动一次偏 41 倍：命中卡片靠运气，
# 小目标（返回箭头）永远点不中。参数运行时可写，必须在 miniapp 起来之前写好。
# PANEL=640x260 来自 pack.env（框架注入）。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ] && [ -n "$PANEL" ]; then
    cat > "$TARGET/etc/init.d/S00mouse-scale" <<MSCALE
#!/bin/sh
# s6pro: mousedev 的 ABS->像素缩放对齐面板（app 光标走 mouse0 增量）
echo ${PANEL%x*} > /sys/module/mousedev/parameters/xres 2>/dev/null
echo ${PANEL#*x} > /sys/module/mousedev/parameters/yres 2>/dev/null
MSCALE
    chmod +x "$TARGET/etc/init.d/S00mouse-scale"
    echo "pack hook fixup: S00mouse-scale installed (PANEL=$PANEL)"
fi
