#!/bin/sh
# Install the generic physical-key helper + its apply script into the target.
# Runs in the initramfs (sourced), before switch_root. runDictPen calls
# /usr/bin/vm-keys-apply just before miniapp (model bind + helper start).
if [ -n "$TARGET" ] && [ -f /patches/vm-keys.pl ] \
   && [ -f /patches/vm-keys-apply ] && [ -f /patches/vm-keys.env ]; then
    mkdir -p "$TARGET/usr/bin" "$TARGET/etc/init.d"
    cp -f /patches/vm-keys.pl "$TARGET/usr/bin/vm-keys.pl"
    rm -f "$TARGET/etc/init.d/S21vm-keys" "$TARGET/etc/init.d/S00vm-keys"
    cp -f /patches/vm-keys-apply "$TARGET/usr/bin/vm-keys-apply"
    cp -f /patches/vm-keys.env "$TARGET/etc/vm-keys.env"
    chmod 755 "$TARGET/usr/bin/vm-keys.pl" "$TARGET/usr/bin/vm-keys-apply"
    echo "pack hook vm-keys: installed (model/codes in /etc/vm-keys.env)"
fi

# 某些固件由 S01systeminit 直接起 runDictPen，而 [adapt.replace] 的 runDictPen
# 要到 app 启动后才落盘 —— 在 S01systeminit 的 guardian 行前插入 apply，确保
# 显示初始化已经完成、model 只影响 app 的 HAL 查找。apply 幂等。
if [ -n "$TARGET" ] && [ -f "$TARGET/etc/init.d/S01systeminit" ] \
   && ! grep -q 'vm-keys-apply' "$TARGET/etc/init.d/S01systeminit"; then
    sed -i 's|^\([[:space:]]*\)/usr/bin/guardian_run /usr/bin/runDictPen|\1/usr/bin/vm-keys-apply; &|' \
        "$TARGET/etc/init.d/S01systeminit"
    echo "pack hook vm-keys: S01systeminit patched"
fi
