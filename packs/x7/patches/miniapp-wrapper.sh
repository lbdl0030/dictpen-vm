#!/bin/sh
# Q109: x7 64-bit storage shim (AArch64, glibc >= 2.17).
# fixup.sh installs this over /usr/bin/miniapp; the real launcher is .real.
LD_PRELOAD=/usr/lib/vmstorage/libvmstorage.so exec /usr/bin/miniapp.real "$@"
