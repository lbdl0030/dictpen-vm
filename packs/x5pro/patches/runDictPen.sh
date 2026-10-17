#!/bin/sh
# x5pro 的 runDictPen 替代版（经 [adapt].replace 覆盖 /usr/bin/runDictPen）。
# 与厂商原版行为一致的部分：设 LD_LIBRARY_PATH、cd 到 app 目录、起 miniapp。
# 去掉的部分：
#   * 崩溃计数 -> update_engine --misc=clear; reboot（VM 里 update_engine 已是
#     空操作，但 reboot 还在——crash-loop 会把 VM 重启进死循环）；
#   * 日志轮转（保留；guest 的 corelog janitor 与框架 S00corelog 负责清理）。
APP_PATH=/oem/YoudaoDictPen/output
cd $APP_PATH
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH:/etc/miniapp/jsapis:/etc/miniapp/messageknife
export LC_ALL='zh_CN.utf8'

if [ ! -d /userdata/applog ]; then
    mkdir /userdata/applog
fi
if [ ! -d /userdata/DictPenData/CapData ]; then
    mkdir -p /userdata/DictPenData/CapData
fi

LOG_FILE=/userdata/applog/DictPen_`date +%Y%m%d_%H%M%S`.log
/usr/bin/miniapp > $LOG_FILE 2>&1
