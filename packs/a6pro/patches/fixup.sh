# a6pro 钩子（initramfs /init 里、switch_root 之前 source，POSIX sh）。

# --- 存储 shim / miniapp 包装器（Q107）-------------------------------------------
# a6 没有本包自己的 runDictPen，所以由 fixup 直接把 /usr/bin/miniapp 换成 wrapper：
# 首靴把固件原启动器留成 .real，之后每靴只覆盖 wrapper。
# shim 是 libc-free 的（uClibc 用户态）：statfs 走 raw syscall，字符串用
# libstdc++ 的 operator new 分配；放 /usr/lib/vmstorage/ 子目录，避免加载器扫
# /usr/lib 顶层找缺失库时误开。
if [ -f /patches/libvmstorage-armhf-uclibc.so ] && [ -f /patches/miniapp-wrapper.sh ]; then
    mkdir -p "$TARGET/usr/lib/vmstorage"
    cp -f /patches/libvmstorage-armhf-uclibc.so \
          "$TARGET/usr/lib/vmstorage/libvmstorage-armhf-uclibc.so"
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/lib/miniapp-wrapper.sh"
    if [ ! -e "$TARGET/usr/bin/miniapp.real" ]; then
        cp -f "$TARGET/usr/bin/miniapp" "$TARGET/usr/bin/miniapp.real"
    fi
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/bin/miniapp"
    chmod 755 "$TARGET/usr/lib/vmstorage/libvmstorage-armhf-uclibc.so" \
              "$TARGET/usr/lib/miniapp-wrapper.sh" "$TARGET/usr/bin/miniapp.real" \
              "$TARGET/usr/bin/miniapp"
    echo "pack hook fixup: uClibc storage shim installed"
fi
