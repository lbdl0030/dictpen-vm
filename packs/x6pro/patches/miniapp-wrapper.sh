#!/bin/sh
# Q99: make the pen's Settings storage follow the VM state disk.
# prepare.sh installs libvmstorage.so at /usr/lib and moves the real launcher
# to /usr/bin/miniapp.real; this wrapper only adds the preload.
LD_PRELOAD=/usr/lib/libvmstorage.so exec /usr/bin/miniapp.real "$@"
