#!/bin/sh
# x5pro 的 get_pcba_version shim（经 [adapt].replace 覆盖 /usr/bin/get_pcba_version）。
# 原版从真机硬件（vendor_storage / PCBA 传感器）读板型，VM 里返回 "unkown_V?"，
# 导致 /etc/init.d/S50-app-launcher 的机型分支全部不匹配、UI 栈永远不会被拉起。
# 这里直接报告本机的板型（与厂商 boot DTB 名 cv1826_zt_0005a 对应的 Cherry V1 5G）。
echo "Cherry_Cvitek_1826_V1_5G"
