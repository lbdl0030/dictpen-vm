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

# Q105: 32-bit storage shim + NMT no-op; install the LD_PRELOAD wrapper before the app.
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
# pack: 应用侧两处修复的幂等版（Q147）。这两件事原先只在状态盘上手工做，换盘或重装就丢：
#  1) 已安装的媒体播放器包 DT_NEEDED 里还是 libyocr.so，而 fixup 已经把 libyocr 藏掉
#     （没有 CVITEK TPU），于是播放页起不来。包内预设 amr 是补丁版，但已经装上的那份
#     不会自己变 —— 在这里就地改成同长度的 libdl.so.2；没写 libyocr.so 的文件不碰。
#  2) 随身听被商店升到 2.1.3 会缺框架模块 ydListen/ydListenManager 而黑屏，钉回包内
#     预设 2.1.1。`miniapp_cli install` 要框架起来才有效，所以放后台等它先起；
#     每个状态盘只装一次（标记文件；装失败不写标记，下次开机再试）。
for _root in /userdata/miniapp /userdisk/miniapp /userdisk/secondary/miniapp; do
    for _var in a b; do
        for _lib in "$_root"/data/mini_app/pkg/8001650599023931/"$_var"/libs/*mediaplayer*.so; do
            [ -f "$_lib" ] || continue
            if grep -qa 'libyocr.so' "$_lib"; then
                /usr/bin/perl -pi -e 's/libyocr\.so/libdl.so.2/g' "$_lib" \
                    && echo "pack: $(basename "$_lib") DT_NEEDED -> libdl.so.2"
            fi
        done
    done
done
VM_LISTEN_PIN=/userdata/cfg/.vm_s6_listen_pin
if [ ! -f "$VM_LISTEN_PIN" ]; then
    (
        for _wait in 60 90 120; do
            sleep $_wait
            if /usr/bin/miniapp_cli install \
                    /etc/miniapp/resources/presetpkgs/8080232418330628.amr \
                    >> /userdata/applog/vm-listen-pin.log 2>&1; then
                : > "$VM_LISTEN_PIN"
                break
            fi
        done
    ) &
fi

/usr/bin/miniapp > $LOG_FILE 2>&1

# pack: 厂商版此处的崩溃计数 / update_engine --misc=clear / reboot 分支已删
# （见文件头注释 2）。
