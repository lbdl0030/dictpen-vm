#!/bin/sh
# a7p 的 runDictPen 替代版（经 [adapt].replace 覆盖 /usr/bin/runDictPen）。
# 沿用 a7pro 的版本（同代 4.13.x 固件，脚本结构一致）：设环境、cd 到 app 目录、起 miniapp。
# 去掉的部分：
#   * 崩溃计数 -> update_engine --misc=clear; reboot（VM 里没有 A/B 升级，crash-loop
#     会把 VM 重启进死循环）；
#   * rm -rf /data/applog/DictPen*（每次启动清日志，妨碍调试；corelog janitor 负责清理）。
APP_PATH=/oem/YoudaoDictPen/output
cd $APP_PATH
# ⚠ 必须与厂商原版一模一样：/etc/miniapp/jsapis 和 /etc/miniapp/messageknife
# 不能少。少了 messageknife 就找不到 libgetdevicename.so（它是
# /etc/miniapp/jsapis/libjsapi_export_core.so 的 DT_NEEDED），于是 uClibc 的
# dlopen 失败 -> global jsapi 加载不了 -> $falcon.launchApp TypeError ->
# 框架退回启动「验证引导」-> 屏幕全黑（a7pro 实测，a7p 同代同坑）。
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH:/etc/miniapp/jsapis:/etc/miniapp/messageknife
export LC_ALL='zh_CN.utf8'

# 机型皮肤：本机是 YDPA7-2，固件的「机型码 -> 皮肤」表把它映射到 theme-y09p，
# 这套 UI 的设计尺寸正是本机的 1020x240。
# VM 里没有 /dev/vendor_storage，get_sku 读不到 SKU，应用会落到常量
# MOCK_THEME = theme-melon（960x266 排版，在 1020x240 上必然错位）。
# 固件后门：YEnvironment::value("MOCK_THEME", "") 的返回值会被
# YSettingManagerPrivate::themeName() 原样采用（a7pro 在
# libyddal_business_core.so @0x10f858 定位；4.13.2 同一机制）。
# 用环境变量把皮肤钉成本机对应的 theme-y09p——等价于"让笔认得出自己是 YDPA7-2"，
# 不改任何二进制。
export MOCK_THEME=theme-y09p

if [ ! -d /userdata/applog ]; then
    mkdir /userdata/applog
fi
if [ ! -d /userdata/DictPenData/CapData ]; then
    mkdir -p /userdata/DictPenData/CapData
fi

LOG_FILE=/userdata/applog/DictPen_`date +%Y%m%d_%H%M%S`.log
/usr/bin/miniapp > $LOG_FILE 2>&1
