#!/bin/sh
# s6pro 的 runDictPen 替代版（经 [adapt].replace 覆盖 /usr/bin/runDictPen）。
# 与厂商原版（见 s6work 提取的 usr/bin/runDictPen）的差异只有两处，都注明原因：
#
# 1) LD_LIBRARY_PATH 补上 /etc/miniapp/jsapis 与 /etc/miniapp/messageknife：
#    falcon 的 dynamic_load_jsapi 按库名 dlopen 核心 jsapi（libjsapi_export.so
#    就在 /etc/miniapp/jsapis），厂商脚本没把这个目录放进搜索路径；aiot 层还会
#    显式 dlopen /etc/miniapp/messageknife/libgetdevicename.so。缺路径时 global
#    等 jsapi 挂掉 → JS 模块连锁 "could not load module" → launchApp 失败 →
#    框架退回"验证引导"页，全黑（a7pro 包 2026-10-02 修过一模一样的坑）。
# 2) 崩溃计数的 update_engine/reboot 分支整段删除：VM 里硬重启是破坏性的；
#    且本包把 /tmp 改成 tmpfs，计数本来就跨不了重启。
# 3) switchPartition.sh 整行删除：它调 `update_engine --misc=display` 读 A/B 槽
#    状态，VM 里没有 misc/update-engine，这条命令会永远挂住，导致 miniapp
#    根本起不来（S50-app-launcher 等 wait_miniapp_finish 超时后继续拉 hal 服务，
#    表现为" hal 服务都在、UI 不在"）。A/B 槽清理在 VM 里本来就无意义。

APP_PATH=/oem/YoudaoDictPen/output
cd $APP_PATH
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH:/etc/miniapp/jsapis:/etc/miniapp/messageknife
#export QT_QPA_FB_DRM=1
#export APP_ROOT_PATH=/oem/YoudaoDictPen/output

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

log_num=`find /userdata/applog/ -name "DictPen*" | wc -l`
log_num=`expr $log_num - 10`

log_file=`find /userdata/applog/ -name "DictPen*"`
if [ "${log_file}" ];then
	echo ${log_file} | xargs ls -tr | head -1 | xargs rm
fi

LOG_FILE=/userdata/applog/DictPen_`date +%Y%m%d_%H%M%S`.log

# pack: switchPartition.sh 已删（见文件头注释 3），厂商版此处会调用它
/usr/bin/vm-keys-apply   # VM 实体键：板型 + uinput 替身，必须在 miniapp 之前
/usr/bin/miniapp > $LOG_FILE 2>&1

# pack: 厂商版此处的崩溃计数 / update_engine --misc=clear / reboot 分支已删
# （见文件头注释 2）。
