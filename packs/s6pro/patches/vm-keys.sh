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
