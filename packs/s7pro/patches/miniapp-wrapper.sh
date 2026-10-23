#!/bin/sh
# Q104: s7pro 32-bit storage shim + NMT patch no-op.
# runDictPen.sh installs this over /usr/bin/miniapp; the real launcher is .real.
# libvmappfix（源码 appfix.c）两个空操作：
#   ① 家长控制服务的 10 s 定时器在 asio 线程里发 WS 登录消息，连不上云端就抛
#      websocketpp 异常且无人接 → abort（app 每 16 s 重启一次）；桩掉 connectServer。
#   ② verifyLicense 恒真：VM 没有 yd_misc_info 分区，开机约 5 分钟后重校验失败，
#      前台被切到「开始激活」且没有返回键。
LD_PRELOAD="/usr/lib/libvmstorage-armhf.so /usr/lib/libvmappfix-armhf.so" \
    exec /usr/bin/miniapp.real "$@"
