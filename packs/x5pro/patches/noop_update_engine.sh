#!/bin/sh
# x5pro 的 update_engine shim（经 [adapt].replace 覆盖 /usr/bin/update_engine）。
# 原版做 A/B 升级槽管理；VM 里既没有 misc 分区也没有升级流程，而且它在
# "recovery 模式"（cmdline 判定）下会主动 reboot —— 这是救援 shell 反复死亡的
# 直接原因。整个替换为空操作。
exit 0
