#!/bin/sh
# s7pro 的 get_pcba_version shim（经 [adapt].replace 覆盖 /usr/bin/get_pcba_version）。
# 固件原版读 /proc/device-tree/model 分支；VM 里是 QEMU 的 "linux,dummy-virt"，
# 所有分支都不匹配 -> 返回 unkown_V?，S01panel / S50launcher 的机型分支
# （insmod 参数、input-event-daemon.conf 选择、服务开关）全部走 else。
# 板型证据：boot FIT 里 Y08 板 DTB 的 model = "Cvitek Cv1826 for Youdao Y08 V0
# version"，与 rootfs 里 S50 脚本的 "${pcba}" == "Y08_CV1813_V0" 分支一一对应，
# 也和板型表（get_pcba_version 的 DTS 表）一致。
echo "Y08_CV1813_V0"