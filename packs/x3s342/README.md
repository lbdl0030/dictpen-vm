# x3s342 —— YDPX3s / RK3566 / OS 3.4.2（miniapp 桌面）

同板子的另一代固件。OS 2.8.65（旧版 native Qt 桌面）那套包已移到
`../vm-archive/x3s-2865`（仓库同级的 `vm-archive/`），不在本仓库里。本包是 3.4.2，
桌面由 miniapp（libfalcon + QuickJS）提供，和 x6pro / x7 / s7pro 同一条产品线。
判定依据：`/usr/bin/runDictPen` 最后执行 `/usr/bin/miniapp`（旧 Qt 那行是注释），
增量的 delete 清单删掉了 737 个旧 Qt 应用文件。

## 来源与切分

`X3s-OTA-OS3.4.2` 的 `YDPX3s-OTA-all-os3.4.2.img`（1 586 915 914 B，RKFW/RKAF）。
分区表取自包内 `Image/parameter.txt`（`mtdparts=rk29xxnand:...`）；RKAF 条目里
`pos` 相对 RKAF，`padded` 才是字节数：

| 分区 | RKAF 相对偏移 | 大小 | 本包落点 |
|---|---|---|---|
| boot_a | `0x7b6800` | 12 563 968 | `images/x3s342-boot_a.img`（FIT：kernel+dtb） |
| system_a | `0x13b2000` | 790 626 304 | `images/x3s342-system_a.img`（**增量载荷**） |
| uresource | `0x305b2000` | 774 897 664 | `images/x3s342-uresource.img` |

## 3.4.2 的 system_a 是增量，不是整包

根下只有 `addition.filelist`（401）、`modify.filelist`（409）、`delete.filelist`（759）、
`do_*.sh`、`manifest.xml`、`all_file_md5.txt`，没有 `/sbin/init`、`/etc/inittab`，
单独起不来。重建方法是拿 2.8.65 的完整 rootfs 当基线，按厂商三个脚本合：

```
do_delete.sh   <base> <delete.filelist>
do_addition.sh <delta> <base> <addition.filelist>
do_modify.sh   <delta> <base> <modify.filelist>
```

产物是 `images/x3s342-merged.img`（1.5 GiB，fsck 过；`/Version` = 3.3.8）。

**两个必须注意的坑**：

1. 基线里 `/bin/mount`、`/bin/umount` 是**指向 busybox 的软链**，3.4.2 把它们换成独立
   二进制。`cp -rf` 会顺着目标软链写，把 `/bin/busybox` 覆盖成 umount 的内容，
   switch_root 后 init 立刻 `bad usage` + panic。复制每个文件前要先 `rm -rf` 目标。
2. 合并完镜像带着未回放的日志，框架把 rootfs 只读挂上时 `recovery required on readonly
   filesystem`，根挂不上、开不了机。合并后要在客机 `e2fsck -fy` 一次。

## UI 栈

固件的 `/etc/init.d/S50-app-launcher` 按 `get_pcba_version` 分支（Cherry-3566_V0 →
weston；Cherry_Cvitek_1826_V0 → fb），VM 上返回 `unkown_V?` 时两个分支都不走，且它
调用的 `/usr/bin/weston` 不在 system 镜像里。`patches/S55vm-ui` 补这些：

1. 把 `/proc/device-tree/model` bind 成 `Rockchip RK3566 Youdao CHERRY Board`（HAL 认板型）。
2. 挂 uresource（pack 的 `[image].extra` → `/dev/vdc`）。
3. 起软件合成器（Debian weston 载荷，pixman）。
4. 起 `/usr/bin/miniapp`。

**合成器必须是 Weston 9，不能是 10**：`libfalcon.so` 的
`greenui::NativeWindow::create()` 走 **wl_shell**，Debian 的 weston 10 把该协议编译掉了，
应用在 `wl_proxy_marshal_constructor` 里对空接口解引用直接 SIGSEGV（屏幕停在白底）。
bullseye 的 `weston 9.0.0` 的 `libweston-desktop-9` 仍带 wl_shell。另外 weston 的模块
目录是编译期写死的 `/usr/lib/aarch64-linux-gnu/libweston-9` 与 `.../weston`，要把
`libweston-9`、`libweston-desktop-9.so.0*`、`weston/` 三个拷进系统。

## 触控

`libfalcon.so` 只引用 `wl_touch_interface`，miniapp 进程不持有任何 input 设备的 fd，
但它依赖的 vendor 输入链按 `cfg.json` 的 `tp` 路径找设备
（`/dev/input/by-path/platform-4020000.i2c-event`）。VM 里没有这个平台节点，所以：

1. `patches/touchbridge.bin`（源码 `touchbridge.c`，用 ARM GNU Toolchain 13.3.rel1
   的 `aarch64-none-linux-gnu-gcc -static` 交叉编译，597 KB）读 virtio-tablet，
   把 `BTN_LEFT` 翻成 `BTN_TOUCH`、把 `ABS_X/ABS_Y` 从 0..32767 缩到 800×254，
   通过 `/dev/uinput` 建一块 `vm-touch` 触摸屏。
2. `S55vm-ui` 起桥、等 `vm-touch` 出现，再把它的 event 节点软链到 cfg 的 tp 路径。

试过但不通的两条，别再走：

- **QEMU virtio-multitouch**：weston 能认成 Touchscreen，`mtt begin/data` 也能把
  `ABS_MT_TRACKING_ID` 与 `POSITION_X/Y` 写进节点，但这条流里没有 `BTN_TOUCH`，
  libinput 不认 touch down，`weston-simple-touch` 也收不到；而且 MT 设备一出现，
  QEMU 就不再把绝对坐标发给 tablet。
- **evemu 桥**：guest 有 `/dev/uinput`/`libevdev`/`evtest`，`evemu-device` 也能造设备，
  但 evemu 2.7.0 的描述文件只认前 8 字节 key bitmap，`BTN_TOUCH`(330) 设不上，
  `evemu-play` 直接报不支持。

## 实体键

- 这台客机没有 perl，同族那份 `vm-keys.pl` 跑不了，所以按 touchbridge 的路子编了
  `patches/fakekeys.c` → `fakekeys.bin`（静态 aarch64，`prepare.sh` 落地时要 `chmod 755`，
  `dpctl put` 过去的文件没有执行位）。
- 键名与键码取自固件 `/usr/configs/hal_key_config.json` 的 `Cherry-3566_V0` 节：
  `home_key_id=platform-adc-keys-event`、`home_key_code=115`；power 是
  `platform-fdd40000.i2c-platform-rk805-pwrkey-event`(116)，head 是
  `platform-led_control-event`(134)。这一节没有音量键与播放键，控制台那两颗音量键因此是灰的
  （`patches/vm-keys.env` 的 `VOL_*` 留空即可，`TestConsoleKeys` 钉住这个映射）。
- 厂商按键 HAL 按 by-path 名字找设备（`libydhal_devices.so` 里有 `/dev/input/by-path/`），
  所以 fakekeys 建完 uinput 设备（名字 `adc-keys`）要补 `platform-adc-keys-event` 这条符号链接，
  而且要在 miniapp 之前建好：按键模块只在启动时扫一次设备。
  板型串已经 bind 成 `Rockchip RK3566 Youdao CHERRY Board`，所以查得到这一节
  （没查到的话应用报的是 -14，见 x6pro DEVLOG §7.11）。
- 按下与抬起之间要留 ~120 ms。0 间隔的 down+up 只会走到 `onHomeButtonPress`，
  应用不把它当成一次点击，页面不动；留了间隔才有 `homeCb action=0/1` 并真的回桌面。
- 已实测（冷启动）：进「查词翻译」→ `echo 115 > /tmp/fake-keys.fifo` → `homeCb action=0/1`
  → `onHomeButtonPress` → 回桌面（截图 `vm/shots/_x342_k2_in.png` → `vm/shots/_x342_k2_home.png`）。
  控制台那颗 Home 按钮走的是同一条命令（`vm/_qwen_key_console.py` 在真实代码路径上验，
  截图 `vm/shots/_x342_k3_console_home.png`）。power/head 两颗仍报 init failed（68 / 45），
  是这两条 by-path 没造，属预期。

## 网络

- `[adapt].ap = true` 让框架的 `S00wifi-ap` 在 wlan1 上广播 `Youdao-VM`。
- `patches/S45vm-wifi` 等 `AP-ENABLED` 之后起 `wpa_supplicant`（`/data/cfg/wpa_supplicant.conf`，
  `update_config=1`）、重试 `udhcpc` 拿到租约，再 `runWifiMgr`。冷启动后 `wlan0` 自动连上，
  guest 出网正常（`ping 223.5.5.5` 通）。
- **虚拟 AP 只能开放**：这个 VM 内核的 `/proc/crypto` 里没有 `ccm`、也没有
  `arc4/michael_mic`，hostapd 改 WPA/WPA2 会报 `key addition failed`；guest 里也没有
  `/lib/modules`，补不了模块。开放网络是唯一可行形态。
- 应用侧已实测可用（冷启动、不加任何干预）：下拉控制中心状态栏有 Wi-Fi 图标、Wi-Fi 磁贴是亮的；
  设置 → 网络 → 「配置网络」里 Wi-Fi 开关为开，列表显示 `Youdao-VM 已连接`
  （截图 `vm/shots/_x342_net_cold2.png`）。关掉开关 → `wpa_state=INTERFACE_DISABLED`、页面变成
  「要查看可用网络，请打开无线局域网」（`vm/shots/_x342_net_off.png`）；再打开 → 自动重连、
  `ssid=Youdao-VM`、租约 `10.201.126.133` 回来（`vm/shots/_x342_net_on.png`）。
- 出网实测：`wlan0` 从虚拟 AP 拿 `10.201.126.0/24`，默认路由走 eth0（QEMU user-net），
  DNS 能解析厂商域名，`wget http://www.baidu.com/` rc=0。
- **别拿 `state` 字段当连通性判据**：应用日志里
  `notifyWifiStatus status:{"method":"open","event":"scan","state":"0"}` 的 `state` 是那条
  scan 事件自己的字段，`wpa_state:"4"`(COMPLETED) 与 `ssid` 才是连接事实。
  这一台的判定链和 x6pro/x5pro 不同：`/usr/bin/wifi-manager` 直接读
  `/var/run/wpa_supplicant/wlan0` 的 `wpa_state=`，没有 `/dev/bes2600` 那层
  （见 `docs/疑难排查.md` §3.1 的是 bes2600 机型），所以不需要造芯片替身。
- `sys_config wifi` 在开机后读到 0，而 `wifi-manager` 是以参数 1 起的；开关的真实动作走
  `handle_wifi_cmd_ctrl_open_wifi` / `close_wifi`（内部执行 `sys_config wifi 1` / `0`）。
  这个键位不影响上面的实测结果，别拿它当判据。

## 设备身份（厂商 HAL store 的替身）

这台 VM 没有 Rockchip vendor storage，`libgetdevicename.so` 通过
`yd_hal_store_module_new()`（`libydhal_devices.so`）读设备名 / device_secret 时
拿不到值，应用就带着字面量 `vendor_storage` 去云端注册，`device_secret` 永远为空。

`patches/libvmsn.so`（源码 `libvmsn.c`，C，`GLIBC_2.17`，用 ARM GNU Toolchain
`-shared -fPIC` 编译）覆盖这个工厂函数，vtable 只实现插件真正用到的两个槽：

- `0x18` → 设备名（`readDeviceName`）
- `0x60` → device_secret（`readDeviceSecret`）

其余槽（`0x08` `readProductAndDeviceInfo`、`0x68` 写 secret）一律返回 `-1`，
让调用者走失败分支 —— 填"成功但空数据"会让上层拿到未初始化结构体，实测应用会退出。
数据来自 `/userdata/cfg/vm_device.conf`，`prepare.sh` 首次启动时写默认值。
**默认不加载**：它会破坏「我的 / 随身听」等页面（Vue `$children` 报错，根在槽 `0x08`
`readProductAndDeviceInfo` 返回 -1）。需要云端设备身份时执行 `touch /userdata/.vm-identity` 再重启应用。

效果（冷启动实测）：`POST https://hardware-push-server.youdao.com/device/getWSToken`
返回成功，应用打开 `wss://.../device_socket/<id>`；`device_secret is not obtained`
不再出现。登录态本身仍是 `not login`（要用账号登录，属另一层）。

## 声音

- QEMU 只有 **card0**（USB Audio，音量控件 `Audio Output Volume Control Playback Volume`
  = numid 3）。`S55vm-ui` 照固件 `S50-app-launcher` 的 Cherry-3566 分支补：绑 asound →
  `alsactl restore` → `hal-audio -D 0 -s $spk` → 起 `runSoundPlayer` / `runSoundRecord`。
- 固件那份 asound 在 VM 里打不开声卡。`pcm.hook_speaker_ctrl`、
  `pcm.rk_eqdrc_hook_speaker_ctrl` 等四段是 `type hooks`，打开时要写 Rockchip 功放的路径
  开关 `Playback Path`；QEMU 的卡没这个控件，`snd_pcm_open` 直接 ENOENT：
  `ALSA lib setup.c:547:(add_elem) Cannot obtain info for CTL elem (MIXER,'Playback Path',0,0,0)`。
  应用侧的症状是 `SoundPlayer.log` 里 `openOutputDevice ... =m_audioDevice.pcm_handle=0`
  → `open=======error=====m_bIsCloseOutput=1`，`pcm0p/sub0/status` 一直是 `closed`，
  宿主峰值 0.0%。`/usr/lib/ladspa` 在固件里没装，`pcm.rk_eqdrc` 的 eq_drc 链同样走不通。
  包里因此带一份 `patches/asound.conf.vm`（prepare.sh 每次开机覆盖到 `/userdata/cfg`，
  S55vm-ui 再 bind 到 `/etc/asound.conf`）：pcm 名字一个不改，只把 hooks 层换成 `type plug`，
  `hw:1,0` / `hw:0,1` / `hw:7,1,0` 收到 `hw:0,0`，`speaker` 按底本注释走 `spk_softvol`。
- 音量：`sys_config spk_volume` 在应用从没设置过它时读到 0，照抄就是
  `amixer cset hw:0 numid=3 value=0`（-128 dB，整机静音）。S55vm-ui 现在把 0 和读不到都按
  80 处理，并写回 sys_config。写回会被 `runSysconfigMgr` 的缓存盖回去（下次开机
  `sys_config spk_volume` 仍是 0），声卡值不受影响，应用播放时也不会把增益拉回 0。
- 已实测（2026-10-24 冷启动）：随身听 → USB上传 → 《I Wanna Know.mp3》，播放页歌词滚动，
  `pcm0p/sub0/status` = `RUNNING`，`scripts/audio-endpoint.ps1 -PeakSeconds 12` 宿主峰值
  73.7%（截图 `vm/shots/_x342_cold_ok.png`）。
- **假采集**：QEMU 的 usb-audio 只暴露 playback（`/dev/snd` 只有 `pcmC0D0p`），
  `asound.conf.vm` 里 `capture.pcm "vm_mic"` + `pcm.vm_mic { type null }`，
  `arecord -D default` 能录到静音 WAV。
- 假采集喂不到厂商录音链：`youdao_hal_audio_config.json` 里 `mic_card_id=0` /
  `mic_subdevice_id=1`，HAL 直接开 `hw:0,1`，绕开 asound 别名。症状是
  `SoundRecord.log` 的 `youdao_hal_audio_read: ... pcm_handle ... is NULL OR device is not MIC!`，
  `/userdisk/record/audioResultOutData.pcm` 停在 0 字节。语音助手页与录音页本身正常
  （进入、波形动画、结束录音返回都不崩），只是没有 ASR 结果。
  补 `snd-aloop` 走不通：客机 `/lib/modules` 没有 `6.1.0-50-arm64` 这棵树（内核是框架带的
  Debian 内核，rootfs 是厂商的）。要让录音链拿到帧得 interpose `snd_pcm_open`，
  把 CAPTURE 方向的 `hw:0,1` 重定向到 null。
- `hal-audio -D 2`（模拟耳机）返回 -22 是预期（没有 card2），不用给 QEMU 加卡。
- 语音助手页正常（2026-10-24 复测：助手页、录音页、结束录音返回都对，截图
  `vm/shots/_x342_va4.png`、`vm/shots/_x342_va5.png`、`vm/shots/_x342_va6.png`）。之前那次黑屏出现在加载 store 替身时，
  是应用自身的 Vue 报错（`cannot read property '$children' of undefined`），根在槽 0x08，
  见「设备身份」；替身默认关闭后不复现。

## 存储与设置

- 厂商 `YSystemInfoAdapter::getStorageInfo`（在 `libyddal_adapter.so`）返回固件写死的
  `{"totalSpace":4,...,"user":0,"available":0}`，设置 → 空间清理 因此恒显示
  「已使用4.0GB/4GB」。这台与 x6pro 是同一份适配层：两个 mangled 符号名一致，
  都是 AArch64（`std::string` 走 x8 返回），所以直接套 x6pro 的 preload shim。
- `patches/vmstorage.c` → `libvmstorage.so`：用 `statvfs("/userdata")`（就是状态盘）生成同一份 JSON。
  S55vm-ui 把它**常开**进 `LD_PRELOAD`；要云端设备身份时再往后追加 `libvmsn.so`，两者可共存。
- 实测（冷启动、2G 状态盘）：装 shim 前「已使用4.0GB/4GB」（截图 `vm/shots/_x342_store_base.png`），
  装后「已使用0.3GB/2GB」，与 `df` 的 259M/2.0G 对上（`vm/shots/_x342_store_shim.png`）。
- 设置项在这台是「空间清理」。整列可见：网络 / 蓝牙 / 音量 / 亮度 / 词典 / 自动发音 / 多行 /
  自定义息屏 / 空间清理 / 系统更新 / 关于。设置 app 冷启动有一二十秒的纯灰加载页，
  别按灰屏判失败（同 Q179 ② 那条时序坑）。

## 息屏

- 设置 → 自定义息屏 有 1/3/5/10/30 分钟五档；往大改会先弹「延长息屏时间会导致电量消耗加快，
  是否确认延长？」，点「延长」才生效（选中态变绿）。截图 `vm/shots/_x342_off1.png`、`vm/shots/_x342_off4.png`。
- 熄屏动作在这台 VM 里没落地：改成 3 分钟、静置三分半，`/tmp/.screen_state` 仍是
  `screen_on=1`（文件 mtime 没动），画面也没黑。方向是 VM 没有背光节点
  （`/sys/class/backlight` 是空的，只有 `/dev/fb0`），厂商那条链没有可驱动的东西。
  这条按「设置里能改熄屏时间」验收通过；真熄屏要另想背光的办法。

## 现状

- 主屏已能画：查词翻译 / 语音助手 / 有道随身听 三张卡片（截图 `vm/shots/_x342_final_home.png`）。
- 触控可用：点第一张卡进「规范扫描」，点第二张进「触发方式」（截图
  `vm/shots/_x342_final_tap.png`、`vm/shots/_x342_native1.png`）。
- 画面：主屏内容按 **800×254** 设计（厂商 cfg 与 preset 桌面包一致），cfg 只决定
  画布/表面。把 cfg 改成 480 只能把画布撑满，主屏仍只占上方 254、下方黑；下拉面板
  按 480 高画，在 254 视口里会被裁 —— 两件事都来自桌面包的设计高度，改 cfg 解决不了。
- 已做：预置应用首启自动装（22/22）、应用内本地播放出声（见「声音」）、实体键 Home 回桌面
  （见「实体键」）、存储页跟随状态盘（见「存储与设置」）、设置里能改熄屏时间（见「息屏」）。
- 未做：power 与 head 两颗键的 by-path、真熄屏（缺背光节点）。
- 按人定的范围不做：录音链路（假采集只到 `default` 消费者）、云端与设备身份。

