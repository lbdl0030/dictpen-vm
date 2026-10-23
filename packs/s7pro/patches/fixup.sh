# s7pro 钩子（initramfs /init 里、switch_root 之前 source，POSIX sh）。
# 每一条都是"这台的固件在 VM 里怎么跑"的适配，逐条注明原因。
# 参考同族的 s6pro 包；所有行为都在本机验证过（取证见 README.md）。

# --- 1. /tmp -> tmpfs ----------------------------------------------------------
# runDictPen 的崩溃计数写在 /tmp：真机 /tmp 是 tmpfs（每靴清零），VM 的 / 是 overlay、
# /tmp 落在状态盘上，计数跨重启累积（本包的 runDictPen 已删掉那条 reboot 分支，这里
# 让 /tmp 的语义与真机一致，避免其它组件也依赖 tmpfs 语义）。
if [ -n "$TARGET" ] && [ -d "$TARGET/tmp" ]; then
    mount -t tmpfs -o size=64m tmpfs "$TARGET/tmp" 2>/dev/null \
        && echo "pack hook fixup: /tmp -> tmpfs"
fi

# --- 2. 静默真机诊断服务（runCapFrame / run_uevent_monitor / run_input_event_monitor）
# 它们在 VM 内核上秒退或空转，guardian 无限重启（每秒刷屏 + 反复重写 applog）。
# 关键是这台的 **runCapFrame 启动点在 S01systeminit**（a6pro 语义只覆盖 S50launcher*，
# 漏掉它时 CaptureFrame 常驻写日志 —— 实测 45 分钟写出 3GB 的 Capframe_*.log，直接把
# 4GB 状态盘写满，随后 overlay 只读、一切钩子失效）。所以按通配扫全部 S* 文件，并且
# sed 要同时匹配 `&` 结尾和无 `&` 两种形态（S01systeminit 的那行没有 &）。
# 注意两点（都踩过）：
#   * busybox 的 sed 不认 `\t`，缩进用 [[:space:]] 匹配；
#   * 框架会按 a6pro 语义把 S50launcher 改名成 S50launcher.disabled，rcS 仍会跑它。
for f in "$TARGET"/etc/init.d/S*; do
    [ -f "$f" ] || continue
    sed -i -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/runCapFrame[[:space:]]*$|\1: #pack: runCapFrame off|' \
           -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/run_uevent_monitor[[:space:]]*$|\1: #pack: uevent monitor off|' \
           -e 's|^\([[:space:]]*\)guardian_run_service /usr/bin/run_input_event_monitor[[:space:]]*$|\1: #pack: input monitor off|' \
           -e 's|^\([[:space:]]*\)/usr/bin/guardian_run /usr/bin/runCapFrame.*$|\1: #pack: runCapFrame off|' \
           -e 's|^\([[:space:]]*\)/usr/bin/guardian_run /usr/bin/run_uevent_monitor.*$|\1: #pack: uevent monitor off|' \
           -e 's|^\([[:space:]]*\)/usr/bin/guardian_run /usr/bin/run_input_event_monitor.*$|\1: #pack: input monitor off|' \
           "$f" 2>/dev/null
done
echo "pack hook fixup: 诊断服务静默完成（grep '#pack:' 命中 $(grep -l '#pack:' "$TARGET"/etc/init.d/S* 2>/dev/null | wc -l) 个文件）"

# --- 3. OCR 栈桩化 -------------------------------------------------------------
# libyocr 的初始化链要 CVITEK TPU（libcviruntime 打开 /dev/ion、/dev/cvi-tpu0），
# QEMU 里没有这些硬件。这台的 OCR 接入比 s6pro 深一层：
#   * libyddal_business_core 等库的 DT_NEEDED 拉 libyocr.so；
#   * /etc/miniapp/jsapis、app 包 payload 里还有**按名字 dlopen("libyocr.so")** 的路径；
#   * libYoudaoStitch（OCR 门面）内部还会调 libyocr 的深层符号。
# 所以"把 libyocr 藏掉 + DT_NEEDED 改写"不够（运行期 dlopen 照样失败 → global/verify
# 等 jsapi 起不来 → 框架退回不画东西的「验证引导」→ 全黑）。
# 本包的解法：**提供一个桩化版 libyocr.so**（patches/libyocr.so = 真库的全导出桩：
# init/open/create 类返回 -1，其余返回 0；DT_INIT/INIT_ARRAY 中和，构造器不跑），
# 经 [adapt.replace] 覆盖到 /oem/YoudaoDictPen/output/libs/libyocr.so。原始 DT_NEEDED
# 链、运行期 dlopen、深层符号绑定全部解析到桩上，OCR 整条栈"在但不工作"。
# libYoudaoStitch.so 同法桩化（它的内部 init 链先于任何导出调用就跑）。
# 首次取证：2026-10-05，`ERROR:libyocr.so: cannot open shared object file:dlopen in
# dynamic_load_jsapi` → `could not load module 'global'` → `eval $falcon.launchApp fail`。
# 另：框架的 [adapt.replace] 是 rcS 里的 S00replace，要求目标 `[ -e ]` 才覆盖 —— 若
# 状态盘 upper 里留有以前"藏文件"留下的 whiteout（overlayfs 字符设备 0:0），[ -e ]
# 永远为假、桩库部署不上去。whiteout 是字符设备：只在 [ -c ] 时删（对 lower 层的
# 普通文件跑 rm 反而会制造新的 whiteout，那就把 S00replace 又挡住了）。
if [ -c "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so" ]; then
    rm -f "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so"
    echo "pack hook fixup: removed stale libyocr.so whiteout"
fi
rm -f "$TARGET/oem/YoudaoDictPen/output/libs/libyocr.so.off" 2>/dev/null

# --- 4. applog 上限（兜底）------------------------------------------------------
# rcS 的诊断服务已静默，但 app 自身还会写 applog；状态盘只有 4GB，Capframe 类日志
# 一旦出现（历史实测 3GB/45min）必须立刻清。DictPen_*.log 留最新 10 份。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00applog-cap" <<'ALCAP'
#!/bin/sh
# s7pro: applog 上限（runCapFrame 已在 rcS 静默，这里清历史 + 兜底）
cd /userdata/applog 2>/dev/null || exit 0
rm -f Capframe_*.log 2>/dev/null
ls -t DictPen_*.log 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null
exit 0
ALCAP
    chmod +x "$TARGET/etc/init.d/S00applog-cap"
    echo "pack hook fixup: S00applog-cap installed"
fi

# --- 5. 网络：eth0 up + udhcpc --------------------------------------------------
# vendor 的 S21dhcpcd 跑 dhcpcd 9.4.0，其 seccomp 白名单在 VM 的 Debian 6.1 内核上
# 触发 "Bad system call"（SIGSYS）直接死 —— eth0 连 UP 标志都没有，slirp 的 hostfwd
# （ssh 2222 / adb 15037）全部悬空，dpctl 只能走串口。busybox 自带 udhcpc 配 vendor
# 自带的 /usr/share/udhcpc/default.script 工作正常（slirp DHCP → 10.0.2.15）。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00net-up" <<'NETUP'
#!/bin/sh
# s7pro: eth0 up + udhcpc（vendor dhcpcd 在 VM 内核 seccomp 崩，原因见 README）
[ -d /sys/class/net/eth0 ] || exit 0
ifconfig eth0 up 2>/dev/null
if ! ifconfig eth0 2>/dev/null | grep -q 'inet addr'; then
    udhcpc -i eth0 -s /usr/share/udhcpc/default.script -n -q -t 6 -T 2 >/dev/null 2>&1 \
        && echo "S00net-up: dhcp lease ok" >/dev/kmsg \
        || echo "S00net-up: dhcp failed" >/dev/kmsg
fi
NETUP
    chmod +x "$TARGET/etc/init.d/S00net-up"
    echo "pack hook fixup: S00net-up installed"
fi

# --- 6. 串口 shell（console-loop）----------------------------------------------
# 这台 vendor busybox init 在 rcS 结束后不启 inittab 的 respawn 条目（x5pro/s6pro
# 同款问题），而 `dpctl sh` 在没有 ssh/adb 时就是走串口回落的 —— 得有人守着 console。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ] && [ -d "$TARGET/root" ]; then
    sed -i 's|^::respawn:-/bin/sh$|#pack: &|' "$TARGET/etc/inittab" 2>/dev/null
    mkdir -p "$TARGET/root/.vm"
    cat > "$TARGET/root/.vm/console-loop" <<'CLOOP'
#!/bin/sh
# s7pro: console respawn shell（vendor busybox init 不启 respawn 条目）
echo "pack: console shell ready"
while :; do
    setsid sh </dev/console >/dev/console 2>&1
    sleep 1
done
CLOOP
    chmod +x "$TARGET/root/.vm/console-loop"
    cat > "$TARGET/etc/init.d/S51console-shell" <<'CSHEOF'
#!/bin/sh
# s7pro: 启动 console shell 循环（只起一次）
if ! ps | grep -q '[c]onsole-loop'; then
    setsid /root/.vm/console-loop >/dev/null 2>&1 </dev/null &
    echo "S51console-shell: console-loop started" >/dev/kmsg
fi
CSHEOF
    chmod +x "$TARGET/etc/init.d/S51console-shell"
    echo "pack hook fixup: console shell installed"
fi

# --- 7. 触摸节点别名（桥本体由框架 [adapt].touchbridge 钩子安装）----------------
# 这台 app 走 **tslib**（miniapp 的 maps 里有 /usr/lib/ts/input.so），在 tp 的 event
# 节点上等 **BTN_TOUCH + ABS_X/ABS_Y**（厂商写死 platform-4010000.i2c-event）。而
# QEMU 的 virtio-tablet 只发 BTN_LEFT——QMP 的 abs/key 事件到不了它的 evdev（实测
# 抓包只有 BTN_LEFT，见 README）——tslib 对没有 BTN_TOUCH 的流一概不认。
# 框架的 S00touchbridge（[adapt].touchbridge 声明后安装）把 tablet 经 uinput 重发布成
# "axs_ts" 触摸屏（BTN_LEFT→BTN_TOUCH、坐标按 cfg.json 的 1020x240 缩放）。本钩子把
# 本机要的两个名字都链到桥设备上；桥没起来时退化到裸 tablet（至少 tp 能打开）。
# 注意 S00 排序：S00tp-link 按字典序先于 S00touchbridge，所以要等桥的链接出现。
if [ -n "$TARGET" ] && [ -d "$TARGET/etc/init.d" ]; then
    cat > "$TARGET/etc/init.d/S00tp-link" <<'TPEOF'
#!/bin/sh
# s7pro: 把面板触摸节点链到 uinput 桥设备（详见 README「触摸」）
mkdir -p /dev/input/by-path
i=0
while [ $i -lt 120 ]; do
    [ -e /dev/input/by-path/axs_ts ] && break
    sleep 0.1; i=$((i+1))
done
if [ -e /dev/input/by-path/axs_ts ]; then
    ln -sf /dev/input/by-path/axs_ts /dev/input/by-path/vm-tp
    ln -sf /dev/input/by-path/axs_ts /dev/input/by-path/platform-4010000.i2c-event
    echo "S00tp-link: vm-tp / platform-4010000.i2c-event -> bridge (axs_ts)" >/dev/kmsg
else
    dev=""
    for d in /sys/class/input/input*/name; do
        [ "$(cat "$d" 2>/dev/null)" = "QEMU Virtio Tablet" ] || continue
        for e in "${d%/name}"/event*; do
            [ -e "$e" ] || continue
            ev=$(basename "$e")
            [ -e "/dev/input/$ev" ] && dev="/dev/input/$ev"
        done
    done
    if [ -n "$dev" ]; then
        ln -sf "$dev" /dev/input/by-path/vm-tp
        ln -sf "$dev" /dev/input/by-path/platform-4010000.i2c-event
        echo "S00tp-link: bridge missing, using raw tablet ($dev)" >/dev/kmsg
    else
        echo "S00tp-link: virtio tablet not found" >/dev/kmsg
    fi
fi
TPEOF
    chmod +x "$TARGET/etc/init.d/S00tp-link"
    echo "pack hook fixup: S00tp-link installed"
fi

# --- 8. 系统配置种子 -----------------------------------------------------------
# 固件自带出厂默认（/usr/configs/defconfig.conf）；VM 首次开机 sysconfig-manager
# 拿到空文件会一直刷 "sys cfg uninitialized or unexpected data"。只在缺失/为空时写。
if [ -n "$TARGET" ] && [ -f "$TARGET/usr/configs/defconfig.conf" ]; then
    mkdir -p "$TARGET/data/cfg"
    if [ ! -s "$TARGET/data/cfg/sys_config.conf" ]; then
        cp -f "$TARGET/usr/configs/defconfig.conf" "$TARGET/data/cfg/sys_config.conf" \
            && echo "pack hook fixup: sys_config.conf seeded from defconfig"
    fi
fi

# --- 9. perl（触摸桥运行时；rootfs 无 perl）------------------------------------
# 框架的 touchbridge 钩子硬编码 /usr/bin/perl。素材来自 s6pro 包的同名 perl-tree
# （Debian armhf perl-base 5.20，guest 内核的 COMPAT 层直接跑）。
# 注意：树内模块目录是 5.20.2，而这个 perl 二进制的 @INC 找 5.20（ maj.min）——
# s6pro 当年没真跑过桥所以没暴露；不补链接 strict.pm 都找不到。
if [ -d /patches/perl-tree/usr ] && [ -d "$TARGET/usr" ]; then
    cp -r /patches/perl-tree/usr/bin/. $TARGET/usr/bin/
    cp -r /patches/perl-tree/usr/lib/arm-linux-gnueabihf/. $TARGET/usr/lib/arm-linux-gnueabihf/
    cp -r /patches/perl-tree/usr/share/. $TARGET/usr/share/ 2>/dev/null
    # vendor ld-2.23 不搜 multiarch 目录，libperl 再放一份到默认路径
    cp -f $TARGET/usr/lib/arm-linux-gnueabihf/libperl.so.5.20 $TARGET/usr/lib/
    chmod 755 $TARGET/usr/bin/perl
    # 树内模块目录是 5.20.2，perl 二进制的 @INC 找 5.20：把版本目录链接过去。
    # 注意先删同名目录再 ln —— 目标若是（vendor 留下的）空目录，`ln -sfn` 会把
    # 链接建到它里面而不是替换它，strict.pm 照样找不到（实测踩过）。
    rm -rf $TARGET/usr/lib/arm-linux-gnueabihf/perl/5.20 \
           $TARGET/usr/share/perl/5.20
    ln -sfn 5.20.2 $TARGET/usr/lib/arm-linux-gnueabihf/perl/5.20
    ln -sfn 5.20.2 $TARGET/usr/share/perl/5.20
    echo "pack hook fixup: perl (armhf) installed"
fi

# --- 10. arm64 调试 / 访问工具（strace + dropbear）-----------------------------
# guest 用户态是 armhf，但 guest 内核是框架的 Debian arm64 —— 直接跑 Debian arm64
# 二进制（与框架 hostapd 同一思路）。两类用途：
#   * strace：调试 guest 里 armhf 应用的崩溃（miniapp 段错误时唯一的现场手段）；
#   * dropbear：rootfs 无 SSH，装上后宿主的 `dpctl put/get/sh` 走 scp/ssh（比串口
#     可靠、能传文件）。authorized_keys 由框架 init 注入到 /root/.ssh。
# 文件来自 patches/arm64-tree/（从 s6pro 包的同名素材裁剪：libc/loader +
# tomcrypt/tommath/gmp）。库必须放 /lib、/usr/lib 的 aarch64 多目录（Debian ld
# 内建搜索路径）。
if [ -d /patches/arm64-tree/lib ] && [ -d "$TARGET/lib" ]; then
    mkdir -p $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu \
             $TARGET/usr/sbin $TARGET/usr/bin $TARGET/root/.ssh
    cp -rf /patches/arm64-tree/lib/aarch64-linux-gnu/. $TARGET/lib/aarch64-linux-gnu/
    cp -f /patches/arm64-tree/lib/ld-linux-aarch64.so.1 $TARGET/lib/ld-linux-aarch64.so.1
    # 注意必须是 -rf：busybox 的 cp 没有 -r 时对 "dir/." 报 "omitting directory"，
    # 最初写成 cp -f 导致 libtomcrypt/libtommath/libgmp 没拷进来、dropbear 起不来
    # （串口现场：`cp: omitting directory '/patches/arm64-tree/usr/lib/aarch64-linux-gnu/.'`）。
    cp -rf /patches/arm64-tree/usr/lib/aarch64-linux-gnu/. $TARGET/usr/lib/aarch64-linux-gnu/
    cp -f /patches/arm64-tree/usr/bin/strace        $TARGET/usr/bin/strace
    cp -f /patches/arm64-tree/usr/sbin/dropbear     $TARGET/usr/sbin/dropbear
    cp -f /patches/arm64-tree/usr/bin/dropbearkey   $TARGET/usr/bin/dropbearkey
    cp -f /patches/arm64-tree/usr/bin/dbclient      $TARGET/usr/bin/dbclient
    chmod -R 755 $TARGET/lib/aarch64-linux-gnu $TARGET/usr/lib/aarch64-linux-gnu
    chmod 755 $TARGET/lib/ld-linux-aarch64.so.1 $TARGET/usr/bin/strace \
              $TARGET/usr/sbin/dropbear $TARGET/usr/bin/dropbearkey \
              $TARGET/usr/bin/dbclient
    cat > "$TARGET/etc/init.d/S00dropbear" <<'DBEOF'
#!/bin/sh
# s7pro: arm64 dropbear（首靴生成 host key；authorized_keys 由框架 init 注入）
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
    # 框架注入的 inittab dropbear 行在这台上没有意义（rootfs 原本没有 dropbear），去掉
    sed -i '/dropbear/d' "$TARGET/etc/inittab" 2>/dev/null
    echo "pack hook fixup: strace + dropbear (arm64) installed"
fi

# --- 11. 存储 shim / miniapp 包装器（新增 /usr/lib 文件）------------------------
# [adapt.replace] 走 rcS 的 S00replace，而它只覆盖**目标已存在**的文件
# （initramfs-init.sh: `[ -e "$dst" ] || continue`）。这两个文件是新增的，靠
# replace.list 永远装不上（实测：initrd 里文件在、replace.list 有这两行，boot 后
# /usr/lib 仍没有）。所以在这里直接拷进 TARGET；runDictPen.sh 负责首靴把
# /usr/bin/miniapp 换成包装器（仅当 /usr/lib/miniapp-wrapper.sh 存在）。
if [ -f /patches/libvmstorage-armhf.so ] && [ -f /patches/miniapp-wrapper.sh ]; then
    mkdir -p "$TARGET/usr/lib"
    cp -f /patches/libvmstorage-armhf.so "$TARGET/usr/lib/libvmstorage-armhf.so"
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/lib/miniapp-wrapper.sh"
    chmod 755 "$TARGET/usr/lib/libvmstorage-armhf.so" "$TARGET/usr/lib/miniapp-wrapper.sh"
    echo "pack hook fixup: armhf storage shim installed"
fi
# 应用侧两个空操作：家长控制 WS 定时器桩 + verifyLicense 恒真（安装路径与上面同一套，
# 原因见 appfix.c 顶部注释、miniapp-wrapper.sh 与 §12.16）。
if [ -f /patches/libvmappfix-armhf.so ]; then
    cp -f /patches/libvmappfix-armhf.so "$TARGET/usr/lib/libvmappfix-armhf.so"
    chmod 755 "$TARGET/usr/lib/libvmappfix-armhf.so"
    echo "pack hook fixup: armhf app-side stubs installed"
fi
