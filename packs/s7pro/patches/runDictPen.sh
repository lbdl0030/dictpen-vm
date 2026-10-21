#!/bin/sh
# s7pro 的 runDictPen 替代版（经 [adapt].replace 覆盖 /usr/bin/runDictPen）。
# 除下面两处，与固件原版逐字一致（原版内容见 README 的取证）：
#
# 1) 去掉 `rm -rf /data/applog/DictPen*`：每次启动清日志，妨碍调试；清理交给
#    包的 S00applog-cap 钩子（按数量封顶）。
# 2) 去掉"崩溃计数 > 5 -> update_engine --misc=clear; reboot"分支：VM 里没有 A/B
#    升级引擎、硬重启是破坏性的（$SLOT 在 VM 里是空串，原版这个分支必进）。
# 3) 去掉"藏厂商开机 splash"块（2026-10-05 删）：这块代码从第一天起就是死代码——
#    这台固件里根本不存在 /etc/miniapp/resources/youdao_startup_logo.bmp（find 全盘
#    无此文件），`[ -f ]` 永不命中。当年的黑屏真因是 libyocr 缺失导致 jsapi 加载链
#    断掉（见 fixup.sh 第 3 节与 README），与 splash 无关。
#
# 保留原样、不要动的部分：
#   * LD_LIBRARY_PATH 里的 /etc/miniapp/jsapis 与 /etc/miniapp/messageknife ——
#     固件原版就有（这台没有 a7pro 那个"漏路径 -> dlopen 失败 -> 全黑"的坑）；
#   * cd $APP_PATH、core dump 开关、DictPenData/CapData 目录、before miniapp 打点。
# Q104: 32-bit storage shim + NMT no-op; install the LD_PRELOAD wrapper before the app.
if [ -f /usr/lib/miniapp-wrapper.sh ]; then
    if [ ! -e /usr/bin/miniapp.real ]; then
        mv /usr/bin/miniapp /usr/bin/miniapp.real
    fi
    cp -f /usr/lib/miniapp-wrapper.sh /usr/bin/miniapp
    chmod 755 /usr/bin/miniapp
fi

APP_PATH=/oem/YoudaoDictPen/output
cd $APP_PATH
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH:/etc/miniapp/jsapis:/etc/miniapp/messageknife

if [ ! -d /userdata/applog ] ;then
	mkdir /userdata/applog
fi

#enable core dump
if [ ! -d /userdisk/corefile ] ;then
	mkdir /userdisk/corefile
fi

save_core=`cat /userdata/cfg/debug.cfg | grep "save_core=" | awk -F= '{print $2}'`
if [ "1" == "$save_core" ] ;then
	ulimit -c 10485760
	echo 1 > /proc/sys/kernel/core_uses_pid
	echo "/userdisk/corefile/core-%e-%p-%t" > /proc/sys/kernel/core_pattern
else
	ulimit -c 0
fi

if [ ! -d /userdata/DictPenData/CapData ] ;then
	mkdir -p /userdata/DictPenData/CapData
fi

# pack: 原版此处的 `rm -rf /data/applog/DictPen*` 已删（见文件头注释 1）
# pack: 原版此处的崩溃计数 / update_engine --misc=clear / reboot 分支已删
# （见文件头注释 2）。

LOG_FILE=/userdata/applog/DictPen_`date +%Y%m%d_%H%M%S`.log

echo "before miniapp" >/dev/kmsg
/usr/bin/vm-keys-apply   # VM 实体键：板型 + uinput 替身，必须在 miniapp 之前
/usr/bin/miniapp > $LOG_FILE 2>&1