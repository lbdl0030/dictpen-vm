#!/bin/sh
# Q107: a6pro 32-bit storage shim (uClibc, libc-free).
# fixup.sh installs this over /usr/bin/miniapp; the real launcher is .real.
LD_PRELOAD=/usr/lib/vmstorage/libvmstorage-armhf-uclibc.so exec /usr/bin/miniapp.real "$@"
