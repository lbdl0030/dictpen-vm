#!/bin/sh
# Q105: s6pro 32-bit storage shim + NMT no-op (glibc 2.23).
# Kept outside /usr/lib: the vendor loader scans that dir for missing libs.
LD_PRELOAD=/usr/lib/vmstorage/libvmstorage-armhf.so exec /usr/bin/miniapp.real "$@"
