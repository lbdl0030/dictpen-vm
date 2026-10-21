#!/bin/sh
# Q104: s7pro 32-bit storage shim + NMT patch no-op.
# runDictPen.sh installs this over /usr/bin/miniapp; the real launcher is .real.
LD_PRELOAD=/usr/lib/libvmstorage-armhf.so exec /usr/bin/miniapp.real "$@"
