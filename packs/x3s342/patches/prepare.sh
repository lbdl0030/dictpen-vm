#!/bin/sh
# x3s342（YDPX3s / RK3566 / OS 3.4.2 / miniapp 桌面）的宿主侧准备。
# 跑在可写 overlay 上，源镜像只读。
#
# 与 @2.8.65 那包的区别：3.4.2 的桌面是 miniapp（libfalcon + QuickJS），
# 不用 BaseQml / wayland-shell 插件 / 激活绕过那套；合成器必须带 wl_shell，
# 所以换的是 weston 9（不是 10）。

mkdir -p "$TARGET/etc/vm-disabled" "$TARGET/userdata/applog" "$TARGET/userdisk" 2>/dev/null

# 物理分区挂载脚本在 VM 里会去挂不存在的分区，和 initramfs 的挂载打架；
# S50launcher.disabled 这个名字看着是关掉的，但 rcS 是按 `S??*` 匹配，
# 它照样执行，会拉起 runCapFrame（无摄像头，无限重启）等一串空转服务。
for script in S21mountall.sh S50launcher.disabled S99input-event-daemon \
              S99_run_test_scripts S99_auto_reboot; do
    if [ -f "$TARGET/etc/init.d/$script" ]; then
        mv "$TARGET/etc/init.d/$script" "$TARGET/etc/vm-disabled/$script"
    fi
done

for s in S45vm-wifi S55vm-ui; do
    if [ -f "/patches/$s" ]; then
        cp "/patches/$s" "$TARGET/etc/init.d/$s"
        chmod 755 "$TARGET/etc/init.d/$s"
    fi
done

# 合成器载荷：guest 侧第一次启动时解开（/userdata 在状态盘上，跨启动保留）
if [ -f /patches/weston.tar ]; then
    cp -f /patches/weston.tar "$TARGET/userdata/weston.tar"
fi
if [ -f /patches/weston9.tar ]; then
    cp -f /patches/weston9.tar "$TARGET/userdata/weston9.tar"
fi
# 触控桥：静态 aarch64 二进制（patches/touchbridge.c 交叉编译），
# 把 virtio-tablet 的 BTN_LEFT/ABS 翻成单点触摸屏。
if [ -f /patches/touchbridge.bin ]; then
    cp -f /patches/touchbridge.bin "$TARGET/userdata/touchbridge.bin"
    chmod 755 "$TARGET/userdata/touchbridge.bin"
fi
# 设备身份替身：VM 没有 Rockchip vendor storage，厂商 HAL 的 store 模块拿不到
# SN/secret；这个 LD_PRELOAD 库用纯文本配置顶上（S55vm-ui 起应用时加载）。
if [ -f /patches/libvmsn.so ]; then
    cp -f /patches/libvmsn.so "$TARGET/userdata/libvmsn.so"
    chmod 644 "$TARGET/userdata/libvmsn.so"
fi
if [ ! -f "$TARGET/userdata/cfg/vm_device.conf" ]; then
    mkdir -p "$TARGET/userdata/cfg"
    cat > "$TARGET/userdata/cfg/vm_device.conf" <<'DEVCONF'
device_name=YDPX3s-vm
device_secret=vmdevsecret0000000000000000000000
screen=800*480
DEVCONF
fi

# 实体键替身：这台客机没有 perl，同族的 vm-keys.pl 跑不了，用交叉编译的静态二进制。
# dpctl put 落地的文件没有执行位，这里必须 chmod 755。
if [ -f /patches/fakekeys.bin ]; then
    cp -f /patches/fakekeys.bin "$TARGET/userdata/fakekeys.bin"
    chmod 755 "$TARGET/userdata/fakekeys.bin"
fi
if [ -f /patches/vm-keys.env ]; then
    cp -f /patches/vm-keys.env "$TARGET/userdata/cfg/vm-keys.env"
fi

# 存储 shim：厂商 getStorageInfo 返回固件写死的 4GB，设置 → 空间清理 永远显示满。
# 这个 preload 库把它换成 statvfs("/userdata")，也就是 VM 状态盘。
if [ -f /patches/libvmstorage.so ]; then
    cp -f /patches/libvmstorage.so "$TARGET/userdata/libvmstorage.so"
    chmod 644 "$TARGET/userdata/libvmstorage.so"
fi

# 声音配置：固件那份 /etc/asound.conf.VCherry_3566 在 VM 里打不开（见 patches/asound.conf.vm）
if [ -f /patches/asound.conf.vm ]; then
    mkdir -p "$TARGET/userdata/cfg"
    cp -f /patches/asound.conf.vm "$TARGET/userdata/cfg/asound.conf.vm"
fi

# 出厂预置应用的安装脚本（首次启动由 S55vm-ui 后台执行一次）
if [ -f /patches/install-presets.sh ]; then
    cp -f /patches/install-presets.sh "$TARGET/userdata/install-presets.sh"
    chmod 755 "$TARGET/userdata/install-presets.sh"
fi

echo "x3s342: compositor + touch + identity payloads staged"

# 原来的 fstab 指向物理 A/B 分区；VM 的挂载由 initramfs 负责
[ -e "$TARGET/etc/vm-disabled/fstab.original" ] || cp "$TARGET/etc/fstab" "$TARGET/etc/vm-disabled/fstab.original"
printf '# VM mounts are established by initramfs.\n' > "$TARGET/etc/fstab"

printf 'x3s342: overlay prepared; source image remains read-only\n'
