# x7 钩子（initramfs /init 里、switch_root 之前 source，POSIX sh）。

# --- 存储 shim / miniapp 包装器（Q109）-------------------------------------------
# x7 的 app 是 AArch64 + glibc 2.36（与 x6pro 同 ABI），直接复用 x6 的 shim
# （只要求 GLIBC_2.17）。x7 没有本包的 runDictPen 替换，故由 fixup 首靴把
# /usr/bin/miniapp 换成 wrapper、原启动器留成 .real；shim 放 /usr/lib/vmstorage/。
if [ -f /patches/libvmstorage.so ] && [ -f /patches/miniapp-wrapper.sh ]; then
    mkdir -p "$TARGET/usr/lib/vmstorage"
    cp -f /patches/libvmstorage.so "$TARGET/usr/lib/vmstorage/libvmstorage.so"
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/lib/miniapp-wrapper.sh"
    if [ ! -e "$TARGET/usr/bin/miniapp.real" ]; then
        cp -f "$TARGET/usr/bin/miniapp" "$TARGET/usr/bin/miniapp.real"
    fi
    cp -f /patches/miniapp-wrapper.sh "$TARGET/usr/bin/miniapp"
    chmod 755 "$TARGET/usr/lib/vmstorage/libvmstorage.so" \
              "$TARGET/usr/lib/miniapp-wrapper.sh" "$TARGET/usr/bin/miniapp.real" \
              "$TARGET/usr/bin/miniapp"
    echo "pack hook fixup: AArch64 storage shim installed"
fi
