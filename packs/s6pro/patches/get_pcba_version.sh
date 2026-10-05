#!/bin/sh
# s6pro 的 get_pcba_version shim（经 [adapt].replace 覆盖 /usr/bin/get_pcba_version）。
#
# 原版读 /proc/device-tree/model 并对照一张板型表；VM 里 model 是 QEMU 的，
# 全部分支不匹配 -> 返回 "unkown_V?"，于是：
#   * /etc/init.d/S50-app-launcher 的机型 if/elif 全部落空，UI 栈永远不会被拉起；
#   * S01panel 落进 else 分支（cvifb width=254），与本机 cfg.json（260 宽）不符。
#
# 这里报告 Plum_V0 —— 本机对应分支。证据：
#   * cfg.json screen.width=260 / tp_xoffset=110，与 S01panel 的 Plum 分支
#     （cvifb start_x=110 width=260）完全吻合（Cherry=254/113，Melon=266/107）；
#   * cfg.json 的触摸是 I2C 事件节点，Plum 是 CV1826 板里唯一 insmod hynitron_ts
#     （I2C 触摸）的分支；
#   * boot FIT 里有 cv1826_zt_0005a_plum 这块板级 DTB（"Cvitek Cv1826 for Youdao
#     Plum V0 version"），四个分支的板型字符串与 get_pcba_version 一一对应。
echo "Plum_V0"
