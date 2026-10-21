#!/bin/sh
# Q106: x5pro 32-bit storage shim + NMT no-op (glibc 2.23).
# runDictPen.sh installs this over /usr/bin/miniapp; the real launcher is .real.
LD_PRELOAD=/usr/lib/vmstorage/libvmstorage-armhf.so exec /usr/bin/miniapp.real "$@"
