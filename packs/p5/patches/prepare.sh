#!/bin/sh
# p5（词典笔 P5 / Almond / RK3566 / OS 3.5.0）的宿主侧准备，跑在可写 overlay 上。
# 结构照 x6pro（同代同构：RK356x + ext4 + weston + miniapp），去掉 x6pro 特有的
# desktop 载荷 / 存储 shim / fake-keys（P5 的按键是 rk805-pwrkey + adc-keys 115，
# 与 Melon 的 adc 键表不同，二期再做）。
mkdir -p "$TARGET/etc/vm-disabled" "$TARGET/userdisk" "$TARGET/userdata/bin" "$TARGET/userdata/applog"

# 物理机专用脚本：挂物理分区 / 起厂商 weston(需真 DSI) / 摄像头 3A / 升级与自检等。
# 与 VM 的 initramfs 挂载和 S50vm-ui 打架的都在这里停用；不存在的条目自动跳过。
for script in S01DisableDebugUart S10async_commit.sh S21mountall.sh S40network S40rkaiq_3A \
              S50-app-launcher S50launcher S50launcher.disabled S98usbdevice \
              S99-auto-reboot S99_run_test_scripts S99input-event-daemon; do
    if [ -f "$TARGET/etc/init.d/$script" ]; then
        mv "$TARGET/etc/init.d/$script" "$TARGET/etc/vm-disabled/$script"
    fi
done

for s in S45vm-wifi S50vm-ui; do
    if [ -f "/patches/$s" ]; then
        cp "/patches/$s" "$TARGET/etc/init.d/$s"
        chmod 755 "$TARGET/etc/init.d/$s"
    fi
done

# 合成器双载荷（guest 侧 S50vm-ui 首启解开）：
#   weston.tar  = bookworm 运行库 + loader（P5 系统 glibc 2.29 太老）
#   weston9.tar = weston 9 本体（libfalcon 走 wl_shell，weston 10 编译掉了该协议）
if [ -f /patches/weston.tar ]; then
    cp -f /patches/weston.tar "$TARGET/userdata/weston.tar"
    echo "p5: weston payload staged"
fi
if [ -f /patches/weston9.tar ]; then
    cp -f /patches/weston9.tar "$TARGET/userdata/weston9.tar"
    echo "p5: weston9 payload staged"
fi

# 触摸桥：C 版（x3s342 同款，无 perl 依赖），S50vm-ui 起桥并软链到 axs_ts。
if [ -f /patches/touchbridge.bin ]; then
    cp -f /patches/touchbridge.bin "$TARGET/usr/bin/touchbridge.bin"
    chmod 755 "$TARGET/usr/bin/touchbridge.bin"
fi
# 触摸节点标记（vm-touch 出现后由 udev 认成触摸屏；名字对不上也无害）。
mkdir -p "$TARGET/etc/udev/rules.d"
if [ -f /patches/99-axs-ts.rules ]; then
    cp -f /patches/99-axs-ts.rules "$TARGET/etc/udev/rules.d/99-axs-ts.rules"
fi

# 通用虚拟网络路径保留；只删真机专用的 dropbear inittab 项（若无）。
if [ ! -x "$TARGET/usr/sbin/dropbear" ]; then
    sed -i '\|::sysinit:/usr/sbin/dropbear |d' "$TARGET/etc/inittab"
fi

# 原 fstab 指向物理 A/B 分区；VM 挂载由 initramfs 负责。
[ -e "$TARGET/etc/vm-disabled/fstab.original" ] || cp "$TARGET/etc/fstab" "$TARGET/etc/vm-disabled/fstab.original"
printf '# VM mounts are established by initramfs.\n' > "$TARGET/etc/fstab"

# 音量控件号：Almond_V0 节原值 speaker_vol_ctl_numid=6（RK805 codec 布局），
# QEMU usb-audio 每张卡的音量控件是 numid 3。只改 Almond_V0 这一节，
# 别的产品节保持原样；重复执行结果不变（x6pro 同款 awk）。
AUDIO_CFG="$TARGET/usr/configs/youdao_hal_audio_config.json"
if [ -f "$AUDIO_CFG" ]; then
    awk '
    /^[[:space:]]*"Almond_V0"[[:space:]]*:/ { insec = 1 }
    insec && /"speaker_vol_ctl_numid"/ {
        sub(/"speaker_vol_ctl_numid"[[:space:]]*:[[:space:]]*"[0-9]+"/, "\"speaker_vol_ctl_numid\":\"3\"")
    }
    insec && /^[[:space:]]*}/ { insec = 0 }
    { print }
    ' "$AUDIO_CFG" > "$AUDIO_CFG.vm-tmp" && mv -f "$AUDIO_CFG.vm-tmp" "$AUDIO_CFG"
fi

# 存储 shim：厂商 YSystemInfoAdapter::getStorageInfo 返回固件写死的容量，
# 应用里凡是显示「已用/总量」的地方都跟着这个数。preload 库把它换成 statvfs(/userdata)。
if [ -f /patches/libvmstorage.so ]; then
    cp -f /patches/libvmstorage.so "$TARGET/userdata/libvmstorage.so"
    chmod 644 "$TARGET/userdata/libvmstorage.so"
fi

printf 'p5: overlay prepared; source image remains read-only\n'
