# 开发日志：x6pro（设备 X6pro / RK3562 Melon / Linux ext4 / OS 4.3.5）

`boot_a.img` + `system_a.img` 在本框架里运行的**全部**适配内容。框架只增加了一个 runtime
（`scripts/runtimes/rockchip_ext4.py`）与一条 runtime 注册；`dpctl.py` / `initramfs-init.sh` / `vmgui` 的核心逻辑未改。

## 1. 平台识别（证据链）

| 事实 | 证据 |
|------|------|
| 平台 | boot 是 U-Boot FIT（arm64 kernel + DTB + resource 多镜像，sha256,rsa2048 签名）；DTB `model = Rockchip RK3562 MELON LP4 V10 Board` |
| 系统 | `system_a.img` 是**原始 ext4**（非 erofs/稀疏，superblock magic 0x53ef）；`etc/os-release` 指向 Melon_pro_Package / RK3562-SDK、`rk3562_linux_melon_pro_defconfig` |
| 版本 | `/Version`：`DictPen_APP Version 4.3.5`，Build 2025-11-26 |
| 面板 | DSI `panel@0/1` 时序 `hactive=480 vactive=960`；`etc/xdg/weston/weston.ini` 对 DSI-1 做 rotate-90；`cfg.json` 逻辑屏 `width=960 height=266 direction=270 yoffset=107`（= (480-266)/2） |
| 用户态 | aarch64 + glibc 2.36（`libc.so.6` 导出到 GLIBC_2.36），因此可直接使用 Debian bookworm arm64 二进制 |
| 图形栈 | **ARM Mali（libmali）+ Rockchip RGA**：`libmali-bifrost-g52-g13p0-wayland-gbm.so`、`librga.so.2`；无 Mesa、无 `/usr/lib/dri` |

镜像校验（`[checks]`，本地计算）：

| 文件 | MD5 |
|------|-----|
| `boot_a.img` | `5a1129a5bd70cf3099b793e512aabb57` |
| `system_a.img` | `db313878a4a151e140d9be7cca287e5d` |

## 2. 引导适配

- runtime `rockchip-linux-ext4`：只读挂 ext4 根（`DICTPEN_ROOTFSTYPE=ext4` 由 runtime 追加），其余走框架既有 overlay/状态盘路径；`validate()` 检查 rootfs 是原始 ext4 而不是 erofs。
- `patches/prepare.sh`（initramfs 钩子，跑在 overlay 上，不动源镜像）：停用实机专用的 `S21mountall.sh`、`S40network`（`/dev/block/by-name/*` 与 bes2600 在 QEMU 不存在）、`S50-app-launcher`、`S98usbdevice`、`S99-auto-reboot` 等；安装 `S50vm-ui`；把合成器载荷投送到 `/userdata/weston.tar`。
- `patches/get_pcba_version` 固定返回 `RK3562_Melon_V0`（原实现读 `/proc/device-tree/model`，与 QEMU 的 DTB 不符），使固件自身的板型分支与真机一致。

## 3. 显示：为什么必须换掉厂商 Weston

**症状**：厂商 Weston 11.0.1 能枚举 EDID、能发现 head，但每条输出都停在 `Failed to init output pixman state`，屏幕全黑。

**取证**：

1. ftrace `function_graph` + kretprobe 追 DRM 调用：`drm_mode_create_dumb_ioctl`、`drm_mode_addfb2_ioctl`、`drm_mode_setcrtc`、`drm_mode_mmap_dumb_ioctl`、`drm_gem_mmap` **全部返回 0**（`kprobes/* arg1=0x0`）——内核侧无问题，失败在用户态。
2. `libweston-11/drm-backend.so` 的 `DT_NEEDED` 含 `librga.so.2` + `libmali.so.1`，并含 `c_RkRgaInit` / `rga not supported` 字样；`/etc/profile.d/pixman.sh` 默认 `PIXMAN_USE_RGA=1`；而 QEMU 没有 `/dev/rga`。
3. 该机 `libgbm.so.1` 是 5.7KB 的 Mali 转发壳（`libmali_hook.so.1` → `libmali.so.1`，需 `/dev/mali`），用 `MALI_DEFAULT_WINSYS` 选后端；QEMU 无 Mali 内核态。

**结论**：这台固件的合成路径两条腿都在 Rockchip 专有硬件上——GL 要 Mali、pixman 要 RGA；QEMU 上两者都不存在，厂商 Weston 在虚拟机里没有可用的软件路径。

**对策（全部在包内，不改框架）**：用 Debian bookworm arm64 的 **Weston 10.0.1 + Mesa libgbm + pixman** 做纯软件合成，载荷随包分发（`weston.tar`，26MB；`[adapt].replace` 投送 → `prepare.sh` 落盘 → `S50vm-ui` 解包并启动）。

关键细节（都踩过）：

- 载荷必须含 `weston/libexec_weston.so.0`（`weston` 与 `kiosk-shell.so` 的依赖）——漏掉时合成器静默不启动；
- Debian 的 Weston 10 接 **logind（libsystemd）**而非 seatd；guest 没有 logind，必须 `--tty=1` 走 direct launcher 才能拿到 DRM master；
- socket 用 `--socket=wayland-0` 固定（否则会是 `wayland-1`，应用连不上）；
- **库隔离**：合成器用 `LD_LIBRARY_PATH=/userdata/debweston/...`，应用只用固件自己的 `/oem/YoudaoDictPen/output/libs:/etc/miniapp/jsapis:/etc/miniapp/messageknife`；混用会让应用加载到 Debian 的 `libfreetype`，进而缺 `libbrotlidec.so.1`（固件里没有这个库）而启动失败。

## 4. 已验证（干净状态盘 `up --fresh`）

| 项 | 证据 |
|----|------|
| 包校验 / runtime / MD5 | `dpctl packs x6pro` 全部就位 |
| ext4 根 + 持久化 overlay + switch_root | 串口 `### ext4 root mounted ###`、`### state: ext4 created and mounted ###`、`### overlay: persistent upper ###`、`### switch_root -> /mnt, init /sbin/init ###` |
| 开发凭证 | `S00license: dev license written (174 bytes)`；`/userdata/cfg/license` 174 字节 |
| 合成器 | `S50vm-ui` 后 socket `/run/debweston/wayland-0` 就绪；`vm-weston.log`：`DRM: output Virtual-1 uses shadow framebuffer`、`Output 'Virtual-1' enabled with head(s) Virtual-1` |
| 应用接入 | `vm-miniapp*.log`：`wayland handle_global interface(wl_shm/xdg_wm_base/...)`、`WLNativeWindow::setSurfaceSize(960,480)`、`xdg_surface_configure`、`setSize(960,266)`、`setPosition(0,107)` |
| 界面像素 | `vm/melon-ui5.png`：固件真实 UI 上屏（激活页按钮），UI 条带位于 y=107 的 960×266 区域 |
| 触控（鼠标→触摸桥） | 自研 `patches/touchbridge.pl`：uinput 设备 name/phys=axs_ts → `/dev/input/by-path/axs_ts -> /dev/input/event2`；`dpctl tap 300 150` → 桥日志 `down 300,83`、`tap 600 350` → `down 599,193`（UI 像素坐标）。**注意：本轮取证发现当时设备是废的，应用并无反应；修复见 §5.6(1)** |
| 桌面全渲染 | `vm/melon-home2.png`：查词翻译 / 语法精讲 / 随身听 三张卡片完整（960×266 条带，y=107）；资源来自 `vm/vm/desktop_x`（178 张图 + 页面 bundle），装入 `pkg/<id>/a/`（id 取应用日志的 topAppId：8080222437664451 / 8080252464522508）后图片错误从数百次降到 15 次 |
| 渲染耗时 | 应用启动 → 首帧全渲染 ≈ **77 s**（采样：65.2 s 仍是合成器背景，76.8 s 出现 315 KB 全渲染帧；TCG 纯软件模拟）。合成器不再被跳过后，本轮实测 20 s 内出全帧 |
| 网络（Wi-Fi 关联 + DHCP） | 通过 | `x6pro-wifi: ssid=Youdao-VM wpa_state=COMPLETED` + `wlan0 inet addr:10.201.126.13x`（AP 的 dnsmasq 发租约）；eth0 出网 DNS 与 ICMP 实测通 |
| 触摸设备形态 | 修复后通过 | 当时 `B: EV=1`（一个位都没设上）；按值传参修复后 `B: EV=b`、`B: ABS=3`、`B: PROP=2`，weston 记 `tagged by udev as: Touchscreen` / `device is a touch device`（§5.6(1)） |
| 触摸驱动 UI | **通过**（2026-10-16） | `tap 480 240` → 查词翻译 → 「规范扫描」页；`tap 150 240` → 有道小P → 「此功能需要联网使用」页；`vm/_x6pro/boot3-tap.png`、`boot4-tap.png`，pid 不变、dump 数不变 |
| UI 显示网络已连接 | **通过**（2026-10-16 第二轮） | 补上模拟的 `/dev/bes2600` 并让厂商 `wifi-manager` 上线后：首页状态栏出 Wi-Fi 图标、设置→Wi-Fi 显示「已连接 / Youdao-VM 已连接」、有道小P 卡片进云端页而非「此功能需要联网使用」；根因与取证见 §5.7 |
| 滑动 / 翻页 / 列表滚动 | **通过**（2026-10-17） | 干净重启下 `swipe.py 480 350 480 150` 首页纵向翻到第 2 页（`vm/_x6pro/cb2-vert.png`）、`swipe.py 880 210 250 210` 应用行横滚到行尾「设置」（`cb-hrow.png`、`cb2-flick.png`）、设置页 `swipe.py 300 350 300 160` 整页滚动列表项（`cb2-set-a.png` → `cb2-set-b.png`）；根因（桥把坐标当边沿事件 + QEMU 对未变化的轴去重）见 §5.8 |
| 越过激活 | 带开发凭证后日志出现 `Pin=Develop=homeModeIconShow=false` 与 30s 周期 `Message insert` 主循环 → 应用已进入首页逻辑 |
| 交互式 SSH（双击 `ssh-vm.cmd`） | **通过**（2026-10-17 第二轮） | 根因不在 ssh 配置，而在 guest 从来没挂 devpts：dropbear 对任何请求终端的登录回 `PTY allocation request failed on channel 0` 并以 255 退出。框架 initramfs 补挂后 `tty` → `/dev/pts/0`，取证与修法见 §5.9 |

## 5. 未完成（按证据排序）

1. ~~**首页绘画**~~ 已闭环（2026-10-17 第三轮，见 §5.10）：让固件自己的 PMS / ResourceManager 把 35 个预设包装完（`env.json` 的 `preset_app_installed: true`）后，图片随包就位，首页直接是完整三卡片 —— 手工投送 `desktop.tar` 不再是必需（保留无害）。原先"包不完整"的状态是**首启安装还没跑完**时的中间态。
2. ~~**触控**~~ 已完成（2026-10-17 复验）：`[adapt].touchbridge` + `cfg_overlay` 之外，堵点全在桥脚本自己 —— 先是 `UI_SET_*BIT` 的按值/按指针传参（§5.6(1)），后是"坐标当边沿事件"导致 tap 与滑动都被吞（§5.8）。现在干净重启下点、翻页、横滚、列表内滚动全部可用。
3. 面板归一化（可选）：x7 的先例是把 `direction`/偏移归零、`[display]` 声明成 UI 逻辑尺寸；本包当前保留 `cfg.json` 原值（实测应用在 960×480 输出上把 UI 正确画在 y=107）。
4. **客机熵池长期只剩 2 bits**（2026-10-17 顺手量到的）：`scripts/build-initramfs.py` 已经把 `virtio-rng.ko` 装进 initrd、guest 里 `virtio_rng` 也确实 loaded，但 dpctl 的 QEMU 命令行**没有 `-device virtio-rng-pci`** → 没有设备绑定 → `/sys/class/misc/hwrng/` 下没有 `current_rng`，没人往池子里搬熵。眼下 dropbear 读的是 `/dev/urandom`，所以还没咬人；§7 记过的偶发 `kex_exchange_identification` 很可能就是它。要补得同时做两件事：QEMU 加设备 + 客机跑 feeder（hwrng 核心只在 `/dev/hwrng` 被打开时才搬运）。**注意**：加 PCI 设备会挪动 `/dev/input/eventN` 的编号，7 个包里凡是硬编码节点名的都要先审。**这条归因还没验证过**，而且别再拿它解释"ssh 连不上"——交互式 ssh 那条真因是缺 devpts（§5.9），与熵无关。

### 5.1 增量（同日深夜）

1. **全新状态盘的首启初始化不完成**：新盘第一次开机时固件停在自带 splash「正在初始化配置，请等待…」（>3 分钟不推进，补起 ResourceManager / SoundPlayer 也一样）。崩点前日志停在 `yocr_ncnn` 加载 `/oem/.../x6pro-rec-32x1280-v9.0.1.2.rknn.encrypt` 并落 `YBreakpad` dump → OCR/NPU 初始化链在 QEMU 上不可用，与 s7pro README 同类（s7pro 的解法是把 `libyocr.so` 全导出桩化 + 中和构造器，让应用走 OCR 不可用容忍路径）。
2. 已初始化的状态盘上，应用能正常进桌面并按上表渲染；当前可复现路径是：用已初始化的状态盘（勿加 `--fresh`）启动并拉起应用。
3. 触控已完成（见上表）：桥脚本随包分发，`[adapt].touchbridge` + `cfg_overlay` 把 `tp` 指到 `/dev/input/by-path/axs_ts` 且 `tp_direction/tp_xoffset/tp_yoffset` 归零。


### 5.2 验收结论（本会话最终）

**已达成**：显示链路 + 固件真实 UI 全渲染。`vm/melon-home2.png` 是完整桌面（查词翻译 / 语法精讲 / 随身听 三张卡片，960×266 条带居中在 y=107）。渲染耗时为实测两轮独立数据：

| 轮次 | 应用启动 → 首帧全渲染 | 证据 |
|------|----------------------|------|
| 1 | ≈ 77 s | 65.2 s 仍是合成器背景（2843 B），76.8 s 出现 319 KB 全渲染帧 |
| 2 | ≈ 64 s | 41 s 背景，64 s 出现 218 KB 渲染帧 |

**未闭环**：全新状态盘的首启初始化。表现与证据：应用会进入自带 splash「正在初始化配置，请等待…」，日志显示它在做首启流程（`YLoginModule` 读 `/userdata/DictPenData/userId` 失败、`YHttpCommonParams` 使用 fallback 身份 `deviceSku=OVERHEAD_X62_SKU_CHN_PRO model=YDPX6-2 deviceSn=vendor_storage`、POST `https://hardware-iot.youdao.com/app/store/version/info/batch`，并逐个包校验 `pkg/<id>/a/libs/`），随后或停在 splash，或干净退出/段错误并落 Breakpad dump 到 `/userdisk/corefile/4.3.5/miniapp-*.dmp`（本轮 dump：`miniapp-20261005235623.dmp`）。

原因分类：应用自状态 + 设备身份。`vendor_storage` 在 QEMU 不存在，应用走 fallback SKU；首启那轮云端交互与自身状态写入在 TCG 慢速环境下没有稳定完成（DNS 通，`hardware-iot.youdao.com` 解析到 `luna-gz.alb.ntes53.netease.com`）。

**已内置的闭合手段**：`prepare.sh` 支持 `userdata-seed.tar`——把一台已完成首启初始化的机器（真机最佳）的 `/userdata` 与 `/userdisk/miniapp` 打成一包，声明到 `[adapt.replace]`（源名 `userdata-seed.tar`），包钩子会在 initramfs 阶段解到 $TARGET，让全新状态盘跳过首启初始化。这是把该问题交给真机状态即可闭合的工程解。

> ⚠ **本节"未闭环"的结论已于 2026-10-17 第三轮推翻**（见 §5.10）：`up --fresh` 实测
> 三次全部走到首页，只是需要 ~6.5 分钟（35 个预设包在 TCG 下解包约 3 分钟，期间界面
> 停在 splash）。当时判"不完成"是因为只等了 3 分钟。`userdata-seed.tar` 也因此从
> "必需的闭合手段"降级为"可选加速手段"。

### 5.3 OCR 桩化的关键约束

- 桩化 `libyocr.so` / `libYoudaoStitch.so` 的导出函数是必须的（否则模型加载链在 QEMU 崩）；但**必须保留构造器**（`DT_INIT` / `DT_INIT_ARRAY` / `DT_INIT_ARRAYSZ` 原样）。
- 实测：中和构造器 → 应用启动即 SIGILL（Illegal instruction）；保留构造器 → 应用正常起来并能进桌面。包内当前为保留构造器版本（`DT_INIT=513992`）。
- 桩化规则：函数名含 init/open/create/load/setup/start/connect/begin/prepare/config → 返回 -1，其余返回 0；文件大小不变、仅就地覆写。
### 5.4 网络与输入适配（本轮新增）

**网络**

- 框架侧：包内 `ap = true` 后由框架的 `S00wifi-ap` 在 wlan1 上起虚拟 AP `Youdao-VM`（10.201.126.1）并做 NAT；实测 `iptables-legacy` 必须带 `XTABLES_LIBDIR=/root/.vm/ap/xtables`，否则报找不到 MASQUERADE 目标库。
- 笔侧：新增 `patches/S45vm-wifi` —— 写 `/userdata/cfg/wpa_supplicant.conf`（Youdao-VM，开放网络）、写 `/dev/bes2600`（内容 `wifi opend`，厂商 `wifi-manager` 据此判断芯片状态）、拉起 wpa_supplicant。实测 `wpa_state=COMPLETED`，`wlan0` 从 AP 的 dnsmasq 拿到租约（10.201.126.13x）。
- ~~厂商 `wifi-manager` 不由包启动：它驱动 bes2600，对 hwsim 会主动 deauth 并置 `INTERFACE_DISABLED`~~ —— **这条结论是错的**（2026-10-16 第二轮）：那次"拆台"是因为 `/dev/bes2600` 读不到，wifi-manager 走到"芯片 disabled"分支，不是 hwsim 不兼容。补上芯片节点模拟后它工作正常，本包现在**就是由 `S45vm-wifi` 起它**；其接口约定为控制套接字 `/var/run/wpa_supplicant/wlan0` 与配置 `/data/cfg/wpa_supplicant.conf`（`/data` 是指向 `userdata` 的符号链接，且需要 `update_config=1`）。详见 §5.7。
- 出网：guest 走 eth0（默认路由），实测 DNS 与 ICMP 均通；AP 网段的转发/NAT 规则保留给**外部客户端**——同一内核里 station 与 AP 同栈时单播回程不成立（`ping -I wlan0`、ping AP 地址均丢包），属拓扑限制而非配置问题。

**触摸**（`patches/touchbridge.pl` 修了三个真 bug）

1. uinput 未声明 ABS 范围 → 内核侧 `max=0`，应用把所有触点读成 (0,0)，表现为「点了没反应」；改用 `UI_ABS_SETUP` 声明 `0..W-1` / `0..H-1`。
2. `UI_SET_PHYS` ioctl 号写错（应为 nr=108）→ `phys` 为空、`by-path` 链接落空；修正后 `P: Phys=axs_ts`。
3. 带 MT 槽加 `BTN_TOOL_FINGER` 会被 libinput 判成触摸板，触摸 UI 收不到触摸事件；改为标准单点触摸屏（仅 `ABS_X/ABS_Y` 加 `BTN_TOUCH`），并加 udev 规则 `ENV{ID_INPUT_TOUCHSCREEN}=1`。（当时记的「`UI_SET_PROPBIT` 在当前内核返回 EINVAL」是错的，真因见 §5.6(1)：位号要按值传参，按值传后 `PROP=2` 能直接设上。）

> ⚠ 本节三条方向都对，但**设备当时仍是废的**（`B: EV=1`，一个位都没设上），所以「触控通过」当时只取证到桥日志、没取证应用有反应。完整修复与验证见 §5.6(1)。

**未完成**：应用侧 UI 的「网络已连接」显示尚未取证——需要应用稳定运行时用点击进入设置页截图确认；本轮末次启动的实例在初始化阶段崩过一次（dump 留存在 `/userdisk/corefile/4.3.5/`）。
### 5.5 用鼠标直接控制笔（host 侧直控）

- 事实：QEMU 窗口里的点击到达 guest 是**鼠标事件**（virtio tablet → `/dev/input/event1`），而笔的 UI 是**触摸 UI**；框架的 `pointer` overlay 只在 host 侧画一个落点提示环（其 docstring 自述），**不转发输入**。
- 包内已就位两条转换路径：
  1. guest 侧 `patches/touchbridge.pl` 把 tablet 的按下/拖动转成 `axs_ts` 单点触摸事件（`S00touchbridge: touch device ready`），配合 `99-axs-ts.rules` 让 libinput 视其为触摸屏；
  2. host 侧 `tools/mouse2touch.py`：监听 QEMU 窗口内的鼠标按下/拖动/抬起，按面板坐标经 QMP 直接发触摸序列（可选 `--test X Y` 单发一次），只转发落在 UI 条带（默认 y=107..373）内的点。
- 前提是**应用已经画出界面**：应用若停在启动阶段（屏幕全黑、`pidof miniapp` 有值但无窗口），点哪儿都不会有反应——这个停摆的真因已在 §5.6 定位并修掉。

### 5.6 2026-10-16 本轮：两处真因 + 桩库体检（负结果）

本轮把「点了没反应」拆成两个独立缺陷，都已修复并在实机验证；另外把 x5pro README §10 提出的桩库隐患在本台做了取证，结论是**不成立**。

**(1) 触摸桥：`UI_SET_*BIT` 的参数是按值的，不是指针**

§5.4 记的三个 bug 都修对了方向，但设备实际仍是废的：

```
N: Name="axs_ts"
B: PROP=0
B: EV=1            ← 只有 EV_SYN，没有 EV_KEY/EV_ABS
（无 B: ABS 行）
```

`EV=1` 就是 `input_allocate_device()` 的初始状态——**一个位都没设上**。逐条打 ioctl 返回值取证：

```
SET_EVBIT(KEY) = FAIL(Invalid argument)     ← 传 pack('i',$EV_KEY)
SET_EVBIT(KEY) = ok                          ← 直接传 $EV_KEY
SET_KEYBIT / SET_ABSBIT / SET_PROPBIT 同上
```

根因：内核的 `UI_SET_EVBIT/UI_SET_KEYBIT/UI_SET_ABSBIT/UI_SET_PROPBIT` 走 `uinput_set_bit(arg, ...)`，`arg` 是**位号本身**；而 Perl 的 `ioctl` 第三参数给字符串时传的是**缓冲区地址**，于是内核把地址当位号，`arg >= EV_CNT` 一律 EINVAL。`UI_DEV_SETUP`/`UI_ABS_SETUP`/`UI_SET_PHYS` 确实是传指针的，所以它们一直"成功"，掩盖了失败。

修后（`patches/touchbridge.pl` 的 `setbit()` 统一按值传参）：

```
B: PROP=2      ← INPUT_PROP_DIRECT
B: EV=b        ← EV_SYN|EV_KEY|EV_ABS
B: KEY=400 0 0 0 0 0   ← bit 330 = BTN_TOUCH
B: ABS=3       ← ABS_X|ABS_Y
```

weston 侧确认被认成触摸屏：`event2 - axs_ts: is tagged by udev as: Touchscreen` / `device is a touch device` / `associating input device event2 with output Virtual-1`。

需要纠正 §5.4 的两条结论：

- 「`UI_SET_PROPBIT` 在当前内核返回 EINVAL」是**错的**——那是同一个按值/按指针错误；按值传参后 `PROP=2` 正常设上，不再依赖 udev 规则兜底。
- 「触控通过」当时只取证到桥日志的 `down x,y`（桥自己收到鼠标事件），**没有取证应用有反应**。本轮补上：`tap 480 240` → 查词翻译 → 「规范扫描」页（`vm/_x6pro/boot3-tap.png`）；`tap 150 240` → 有道小P → 「此功能需要联网使用」页（`vm/_x6pro/boot4-tap.png`）。

**仍不完整**：这一版只让「点」通了，拖/滑依然不通；两天后定位到同一个 SYN 处理里的 `next unless $seen` 门（坐标被当成边沿事件），见 §5.8。

**(2) 应用启动停摆：`/run` 在 overlay 上持久，stale `wayland-0` 让合成器永不再启动**

症状即 §5.5 末段：屏幕全黑、`pidof miniapp` 有值但 pid 每几秒变一次、`vm-miniapp.log` 刷 `start /usr/bin/miniapp`（本轮一次开机刷出 4525 行）、`/userdisk/corefile/4.3.5/` **不落新 dump**（不是崩溃，是 abort，Breakpad 没接这条路）。

前台跑一次拿到退出码：`EXIT=134`（SIGABRT）。gdb 抓栈（guest 自带 `/usr/bin/gdb`）：

```
#2 abort () from /lib64/libc.so.6
#3 greenui::NativeWindow::create() from /lib64/libfalcon.so
#4 greenui::WindowManager::getNativeWindow()
#5 falcon_entry ()
```

即**建窗口失败**，与 OCR/NPU 无关。往下查合成器：`pidof weston` 空，而 `/run/debweston/wayland-0` 存在且时间戳是**上一次开机**的。原因：`/run` 不在 tmpfs 而在 overlay 的 upper（状态盘）上，`dpctl down` 若走到「guest did not stop; killing qemu」就是硬断电，weston 来不及清掉 socket；而 `S50vm-ui` 的守卫是 `[ ! -S "$XDG_RUNTIME_DIR/wayland-0" ]` —— stale socket 文件照样通过 `-S`，于是**合成器被跳过**，应用连不上 wayland，`NativeWindow::create()` 每次 abort，guardian 无限重启。

这条也解释了 §5.5 记录的"时好时坏"：**取决于上一次关机是否干净**。

修法（`patches/S50vm-ui`）：守卫改成看进程而不是看 socket 文件，并在启动前清掉残留：

```sh
if ! pidof weston >/dev/null 2>&1; then
    rm -f "$XDG_RUNTIME_DIR/wayland-0" "$XDG_RUNTIME_DIR/wayland-0.lock"
    ...
```

验证（刻意复现故障条件）：`taskkill /F` 直接杀 QEMU → 重启后 `/run/debweston/wayland-0` 仍是上次的 `21:30:48`，随后被清掉并重建为 `21:34:32`，weston pid 794、miniapp pid 832，桌面 20 s 内全渲染，点卡片可进三级页，dump 数不变（11）。

**(3) 桩库体检：x5pro §10 的隐患在本台不成立**

x6pro 的桩库同样是「全导出就地覆写」（`tools/a64stub.py`），所以 x5pro README §10.5 怀疑的问题在这里**确实存在**——被抹掉的 C++ 运行期符号比 x5pro 还多：

| 库 | 被抹的运行期符号 | 字节 | 其中 rootfs `libstdc++.so.6.0.29` 不提供 |
|----|------------------|------|------------------------------------------|
| `libyocr.so` | 517（全部 WEAK） | 203,000 | 508 |
| `libYoudaoStitch.so` | 348（全部 WEAK） | 139,212 | 341 |

但决定性的第二层不成立：把 823 个去重后的符号做成清单，扫描 `/mnt/oem`、`/mnt/etc/miniapp`、`/mnt/usr/lib`、`/mnt/usr/bin` 下**全部 1159 个 ELF/可执行文件**的 `.dynsym` UND 表，命中 **0**。也就是说没有别的对象依赖这些弱定义，桩化不会像 x5pro 那样把全进程的 `unordered_map::operator[]` 换成 `return 0`。对照组（同一把扫描器查 `_Znwm`/`_ZdlPv`/`__cxa_finalize`）在 libcapt/libyocr/libyddal_base 里都能命中，说明 0 是真 0。

差异的来由：x5pro 的 libstdc++ 是 6.0.22（GCC 6），业务库与它同代，模板实例只由业务库的弱定义提供；本台是 6.0.29，且实测这些实例各库自带或不需要。**结论：x6pro 不需要跑 `fix-stub-cxxrt.py` 回填**，别照着 x5pro 顺手改。

工具（本轮新增，留在 `vm/_x6pro/`）：`stubaudit64.py`（ELF64 桩化审计，`load/body/funcs_of/stubbed_runtime` 可复用）、`cxxrt-gap.py`（与 libstdc++ 求差集）、`undscan.pl`（guest 侧 ELF64 UND 扫描，注意这台 guest 的 perl 只支持 `sSiIlLqQjJfFdDpP` 后面的 `<` 后缀，`H<`/`B<` 会报错）、`gdb-abort.sh`（SIGABRT 抓栈）、`fg-run.sh`（前台跑应用取退出码）。

**当时仍未闭环**：应用侧的网络判定 —— 已在同日第二轮闭环，见 §5.7。

### 5.7 2026-10-16 第二轮：应用侧「联网」判定闭环（根因是缺 `/dev/bes2600`）

现象：`wlan0` 已 `wpa_state=COMPLETED`、有 IP、guest 出网实测通（`wget http://connectivitycheck.gstatic.com/generate_204` 成功），应用的 HTTP 也真能发（日志里 `pullSceneListRequest` 拿到正常 JSON 响应），但 UI 仍卡在「此功能需要联网使用，请联网」，控制台日志 `wifiConnectedOperateResult====> global.isOnline(): false`；设置页的 Wi-Fi 开关打开后停在「正在开启无线局域网络…」不推进。

**取证链（谁在决定 isOnline）**

1. `/etc/miniapp/jsapis/libjsapi_export.so` 里有 `_ZN13JSGlobalProxy8isOnlineE` 和 `JSWifiProxy::onWifiStatusChanged(bool)` / `onInternetConnectChanged(string,bool)` —— 状态是**别人推给 JS 的**，不是应用自己探测可达性；所以"网络通但 UI 说不通"完全可能同时成立。
2. 推状态的是 YDAL：`libyddal_business.so` 里有 `connect YWifiModulePrivate ssid:`；DAL 的 IPC 是 D-Bus（`libyddal_base_ipc.so` 的 `YDbusCenter/YDbusEnv`、`/org/youdao/`，会话总线配置 `/oem/YoudaoDictPen/output/configs/yd_dbus_session.conf`，实测 `dbus-daemon --config-file=... --fork` 在跑，socket `/tmp/dbus-XXXXXXXX`）。
3. 总线上那个 wifi 服务的正主是厂商 `wifi-manager`（本包当时故意没起）。它的判据在 `strings /usr/bin/wifi-manager` 里一目了然：`get_bes2600_state` / `cmd[cat /dev/bes2600] wifi opend` / `wifi state is disabled` / `set_bes2600_state: set bes2600 wifi on` / `/var/run/wpa_supplicant/wlan0` / `cp /data/cfg/wpa_supplicant.conf ...`。
4. guest 里 `ls /dev/bes2600` → **No such file**（devtmpfs，QEMU 没有 bes2600 驱动）。于是 `cat /dev/bes2600` 失败 → wifi-manager 认定芯片 disabled → 永远不发布 connected → `isOnline()` 恒 false。

**根因**：应用的「联网」不是可达性判断，而是厂商 wifi 状态机的芯片上报。§5.4 记的「厂商 wifi-manager 对 hwsim 会主动 deauth 并置 `INTERFACE_DISABLED`，所以不能启动」是**同一个根因的症状**，不是 hwsim 的限制——它不是因为驱动不兼容而拆台，是因为查不到芯片状态才走到"关闭"分支。

**修法**（全在 `patches/S45vm-wifi`）：

1. 用普通文件模拟 `/dev/bes2600` + 一个 0.1 s 循环把「命令」翻译成「状态」回写（真驱动的读输出与写输入无关）：写 `ifname:none cmd:WIFI_ON` → 回 `wifi opend`，`WIFI_OFF` → `wifi closed`，其余一律维持/补 `wifi opend`。循环用内建 `read` 不用 `cat`，免 fork。
2. wpa_supplicant 配置搬到厂商期望的 `/data/cfg/wpa_supplicant.conf`（`/data`→`userdata` 符号链接），并把 `update_config` 从 0 改成 1 —— 否则 wifi-manager 的 `wpa_cli_ctrl_save_config()` 报 `save config failed`。
3. 关联 + 拿到租约之后 `setsid /usr/bin/runWifiMgr &`（它自己等 wpa_supplicant 出现，再按 `sys_config wifi` 传 0/1）。

**验证（干净重启，包内脚本自动生效）**：`read -r s < /dev/bes2600` → `wifi opend`；`/usr/bin/wifi-manager 1` 在跑；`wpa_state=COMPLETED`、`inet addr:10.201.126.134`；首页状态栏出现 Wi-Fi 图标（`vm/_x6pro/final-on-c.png`）；`tap 150 240` 进有道小P **不再**是「此功能需要联网使用」，而是云端页「Hi~ 我是你的专属陪伴 有道小P / 新用户登录享新人福利 / 请扫描二维码登录」（`vm/_x6pro/final-xiaop.png`）；设置 → Wi-Fi 页显示「已连接」+ 历史网络 `Youdao-VM 已连接`；开关 off→on 往返都能走完（`final-off-c.png` → `final-on-c.png`）；应用日志出现 `wss://hardware-push-server.youdao.com/device_socket/...` 与 `dict.youdao.com/login/acc/loginstat` 周期轮询——这两件事只在应用认为已联网时才做。

**遗留**：二维码登录页里左侧二维码位显示「请重试」——那是云端接口/宿主 DNS 的问题（应用偶发 `Resolving timed out after 7002 ms` 后走 `Use custom DNS and retry`），不影响联网判定；`[adapt].license` 之外的账号云链路仍未验证。

### 5.8 2026-10-17 本轮：滑动不通（桥把「坐标」当成了边沿事件）

**现象**：用户报「x6pro 滑动不行」。本轮干净重启实测：翻页、列表滚动、轮播全部无反应；更糟的是 `dpctl tap` 在桥日志里**一条都不增**（§5.6(1) 那轮"点已通"跑的是热部署进 guest 的一版桥，包内文件不是它 —— 干净重启才暴露出来）。而 `swipe.py` 手势桥侧事件齐全，界面却不动。

**取证**

1. host 侧新写 `vm/_x6pro/swipe.py`（QMP `input-send-event` 连发 ABS，步间 sleep）。桥日志能打出完整的 `down 699,133` → 14×`move` → `up`，说明**桥已经发出去了**，发射侧不是瓶颈。
2. 同一个桥上 `swipe.py` 的 `down/move/up` 齐全、`dpctl tap` 却一条都不增 —— 两者送进 QEMU 的事件帧形状不同。于是用 `evtest /dev/input/event1`（源设备，不 grab，可与桥并行读同一份事件）抓真实事件流：

```
Event: ... type 3 (EV_ABS), code 0 (ABS_X), value 10240
Event: ... type 3 (EV_ABS), code 1 (ABS_Y), value 13653
Event: ... SYN_REPORT
Event: ... type 1 (EV_KEY), code 272 (BTN_LEFT), value 1     ← 按下这一帧里没有坐标
Event: ... SYN_REPORT
Event: ... type 1 (EV_KEY), code 272 (BTN_LEFT), value 0
```

两条事实：

- **QEMU 对没变化的轴去重**。`input-send-event` 重复发同一个 ABS 值只有第一次上报；`dpctl tap` 的四批事件用的是同一组坐标，所以带 `BTN_LEFT` 的那一批已经没有 ABS 了。
- **QEMU 的 virtio-tablet 不透传 BTN_TOUCH(330)**。`dpctl tap`/`swipe.py` 发的 `key number 330` 在源设备上根本不存在，触摸语义完全靠桥自己合成 —— 这是设计如此，但也意味着桥不能指望源设备给出「触摸开始」这个信号。

**根因**：桥的 SYN 处理里有一道 `next unless $seen`（本帧收到过 ABS 才处理）。按下帧没有坐标 → 按下被吞；抬起帧同样没有坐标 → 抬起被吞（§5.6(1) 收尾时我只修了抬起，按下还挂着这道门）。「点了没反应」和「滑了没反应」是同一个门的两面：**坐标是状态，不是边沿**。

**修法**（`patches/touchbridge.pl`）：位置改为常驻状态，每个 SYN 帧都算一次 `px,py`，只按 `BTN_LEFT`/`down` 的组合决策：

- `!btn && down` → 无条件发 `BTN_TOUCH 0`（抬起）；
- `btn && !down` → 用**最近一次已知位置**发 `BTN_TOUCH 1 + ABS`（按下），不再要求本帧有 ABS；
- `btn && down && seen && 位置变了` → 只发 `ABS`（移动）。

`$seen` 降级为「本帧是否有新轴值」，只用于移动判定；`rx/ry` 初值给面板中心（16383），避免开机后第一次按下落在 (0,0)。

**验证（干净重启，包内脚本自动生效：`md5sum /root/.vm/touchbridge.pl` == 包内 `c0f09c6d367024e20f5cd1315afeb60c`）**

- `tap 480 240` → 查词翻译页（`vm/_x6pro/cb-tap2.png`）；`tap 40 137` → 回首页；
- `swipe.py 480 350 480 150 --steps 10 --ms 20` → 首页**纵向翻到第 2 页**（应用商店 + 应用行，左侧圆点第二颗亮起）：`cb-vert.png`、`cb2-vert.png`；
- `swipe.py 880 210 250 210 --steps 12 --ms 20` → 应用行**横向滚动**（背单词/AI语法分析/小语种翻译/英语同步学 → …/写作指导/单词本/学习营/AI口语教练/语法精讲）：`cb-hrow.png`；
- 快滑（`--steps 6 --ms 10`）同样生效，`cb2-flick.png` 已滚到行尾的「设置」；
- **列表内滚动**（最硬的一条）：设置页 `swipe.py 300 350 300 160 --steps 10 --ms 20` 把两列设置项整页滚过 —— `网络/蓝牙/音量/亮度/词典/自动发音` → `词典/自动发音/多行/自定义息屏/空间清理/系统更新`（`cb2-set-a.png` → `cb2-set-b.png`）；
- 桥日志计数与手势严格一致：一次 14 步滑动 = `down` 1 + `move` 14 + `up` 1。

**需要纠正 §5.6(1) 的一条验证记录**：那轮写的「`swipe.py` 让首页轮播翻到第 2 页」（`sw3-page2.png`）**不成立** —— 复核该图，它是 有道小P 的扫码登录页，不是轮播第 2 页。首页本身是**纵向**分页（左侧两颗圆点是竖排的），第 1 页那三张主卡片横拖不换页（`cb-page2b.png`），可横滑的目标在第 2 页的应用行上。当时的"通过"是把别的操作结果当成了滑动的结果。

### 5.9 2026-10-17 第二轮：交互式 SSH 连不上（guest 从来没挂 devpts）

**现象**：上一轮把 `UserKnownHostsFile=NUL` 那个坑修完，我这边四条路径（`dpctl sh`、裸 `ssh`、`~/.ssh/config`、`ssh-vm.cmd`）**全部静默通过**，用户仍然报"没连上"，且明确入口是**双击 `ssh-vm.cmd`**。

**取证**（先把"我全绿"这件事证到无可辩驳，再找分叉点）

- 用真正的干净 PATH（`C:\Windows\system32;C:\Windows;...OpenSSH`，写进一个临时 `.cmd` 里 `set PATH=`，因为 `cmd //c 'set "PATH=..."'` 从 Git Bash 传过去会被引号截断、上一轮的"干净环境"其实还留着 `D:\Git\usr\bin\ssh.exe`）：`where ssh` 只剩原生客户端，原生 `ssh.exe` 带 `ssh-vm.cmd` 的全套参数 → `RC=0`；`ssh-vm.cmd` 本身 → `RC=0`；`ssh-keyscan` 出来的服务器密钥与 `vm/known_hosts_devvm` 逐字节相同；`attrib tools\ssh\id_devvm` 只有 `A`（不是只读），`icacls` 只有 `lbdl:(R)`（不会触发 UNPROTECTED PRIVATE KEY FILE）。
- 于是只剩一个从没测过的形状：**不带远程命令**——而双击恰好就是这一种。补测 `printf '...\n' | cmd //c ssh-vm.cmd -tt` →

  ```
  PTY allocation request failed on channel 0
  ```

  退出码 255。
- guest 侧：`grep pts /proc/mounts` **空**，`/dev/pts` 是个空目录，`/dev/ptmx` 是 devtmpfs 上的 `crw-rw-rw- 5,2`；`dropbear -V` = v2022.82，命令行 `/usr/sbin/dropbear -p 22 -r /root/.ssh/dropbear_rsa_host_key`（没给 `-B`，不是主动禁 PTY）。手工 `mount -t devpts devpts /dev/pts` 之后 `tty` 立刻给出 `/dev/pts/0`。

**根因**：这台固件自己的 userspace 从不分配伪终端，所以它的启动序列里没有 devpts 这一步；框架的 initramfs 也只挂了 proc / sysfs / devtmpfs。dropbear 收到 PTY 请求后 `openpty()` 失败 → 拒绝 → 会话以 255 结束。**带命令的 ssh 不要 PTY**，所以框架内所有通道（`dpctl sh`、`guest_run`、initramfs 钩子）一直好使，故障只在交互式客户端上出现 —— 这就是"我全绿、用户全红"的分叉点，和 known_hosts 那件事无关（那一条也确实是真坑，只是不是这一条）。

**修法**（框架级，不进固件）：`scripts/initramfs-init.sh` 在 `mount -t devtmpfs none /dev` 之后补

```sh
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true
```

选这里而不是包内 `/etc/init.d/Sxx`：7 个包同一个洞，而 switch_root 用的是 `mount --move /dev`，**会连子挂载一起搬进真根**，所以一处改完全部机型生效、且不需要往固件里塞文件。改完必须 `python scripts\build-initramfs.py` —— 每包 initrd 的缓存指纹里记的是 base initramfs 的 md5（`packlib.ensure_initrd`），不重打 base 就永远还是旧的那份。

**验证**（重打 initramfs → `down` → `up --pack x6pro`，不带 `--fresh`）

- 开机 41 s 时 `grep pts /proc/mounts` 已有 `devpts /dev/pts devpts rw,relatime,mode=600,ptmxmode=000`，`/dev/pts/ptmx` 就位；
- `printf 'tty; echo INTERACTIVE-OK; exit\n' | cmd //c "ssh-vm.cmd" -tt` → 真提示符 `[root@YoudaoDictionaryPen-000:~]#`、`/dev/pts/0`、`INTERACTIVE-OK`；`dpctl shell` 同步可用；
- 回归面：`dpctl status` 四通道 open；`touchbridge.pl` / `weston` / `miniapp` / `guardian_run` 进程都在；桥 md5 包内外一致（`c0f09c6d367024e20f5cd1315afeb60c`）；`vm/_x6pro/post-devpts-home.png` 首页三卡片完整渲染。
- **其余 6 个包还没有开机验证**：改的是框架 initramfs，推理由 `mount --move /dev` 带着子挂载进真根，一处修完全部机型；但实测只有 x6pro。若某个包自己的 init 也挂 devpts，只是叠一层（后挂的那次生效），不会失败 —— 下次在别的包上跑 `dpctl sh -c "grep pts /proc/mounts"` 顺手确认即可。

**本轮没有重跑手势注入**：`dpctl tap` 报 `WinError 10061`，`netstat` 显示 vmgui（pid 25252）正独占 QMP 4446 —— QEMU 一个 QMP chardev 只服务一个客户端，这条限制 `docs/使用手册.md` 里早就写着（"vmgui 在前台时 `dpctl tap` 会因为抢不到 QMP 而失败，先关窗口"）。没有去动用户那个窗口，所以 §5.8 的滑动结论沿用上一轮干净重启的验证（桥字节未变）。顺手把 `qmp_send` 里那句裸 `WinError` 换成点名 vmgui、给出 `--display none` 出路的中文提示。

**需要纠正上一轮的记录**：§7 里 known_hosts 那条修法**仍然成立**，但那轮"四条客户端路径全部静默通过 ⇒ ssh 已修好"**证明范围不够** —— 四条全是"带命令"的调用，一条都没请求过 PTY。以后判断 ssh 通道是否可用，必须包含一次**不带命令的交互登录**。

### 5.10 2026-10-17 第三轮：首启初始化已闭环 —— 原判"不完成"是**等待时间不足**

**结论**：`up --fresh` 自己能走完首启。三次连续实测（每次都是全新状态盘，`--headless`）
**全部**出首页，桌面图片完整、点/返回可用：

| run | 全新盘启动 → 首帧全渲染 | miniapp 启动次数 | Breakpad dump |
|-----|------------------------|-----------------|---------------|
| 1 | ~6–7 min | 3 | **1**（第 1 个实例崩，自愈） |
| 2 | **382 s** | 2 | 0 |
| 3 | **373 s** | 2 | 0 |

§5 第 1 项（首页绘画）与 §5.2 的"未闭环"因此都不成立。卡的不是初始化流程，而是
**把一段 6 分多钟的安装当成了挂起** —— 原记录写的是"`>3 分钟`不推进"，而 3 分钟时
安装只到一半。

**首启阶段时间线**（run 3，`up --fresh --headless`，t=0 为 `up` 返回）

| 阶段 | 时刻 | 证据 |
|------|------|------|
| 应用第 1 次启动 | +43 s | 自带 splash「正在初始化配置，请等待…」，截屏约 12 KB |
| **预设包安装** | +43 s → +330 s | 从 `/etc/miniapp/resources/presetpkgs/*.amr`（35 个、165 MB）逐个解到 `/userdisk/miniapp/data/mini_app/pkg/<id>/a/`（装完 223 MB）；期间界面**一直是 splash** |
| 应用**主动**重启 | +330 s | 日志 `home=systemEnvCustomPresetAppInstalled===restartMiniapp` → `~~~_exit`（设计如此，"装完重启生效"） |
| 应用第 2 次启动 → 进首页逻辑 | +330 s → | `Pin=Develop=homeModeIconShow=false`（§4 用的"已进首页"判据） |
| 首帧全渲染 | +373 s | 截屏 221 251 B；`env.json` 的 `preset_app_installed: true`、`packages.json` 14 946 B |

**要纠正的三条记录**

1. §5.1「`>3 分钟`不推进」——**测量口径错了**。判据应是"等 `homeModeIconShow=false`"
   或"等截屏 >100 KB"，不是固定等 3 分钟。装包在 TCG 下本就要 ~3 分钟。
2. §5.1 把崩点归因于 `yocr_ncnn` 加载 `*.rknn.encrypt` —— 本轮三次启动里精确 grep
   `yocr_*|ncnn|rknn|cvi_device_mem|/dev/cvi-tpu*` 命中 **0**（早先看到的"命中"其实是
   `/oem/YoudaoDictPen/`**`outtpu`**`t/...` 这个子串；`npu` 也只有
   `i`**`npu`**`t_timestamps` 两处误命中）。run 1 那次崩溃点在 `YButtonEventAdapter`
   初始化之后（日志停在 `m_volumeDownModule.init failed res== -14`），与 OCR/NPU 无关。
3. §5 第 1 项「首页绘画要手工装 `desktop.tar`」—— 首启走完时固件自己的 PMS 已经把
   35 个预设包全部装好（`packages.json` / `env.json` / 223 MB pkg 目录都在
   `/userdisk` 上），图片随包就位，首页直接完整。`desktop.tar` 从"必需"降级为"可选"。

**首启期间的偶发崩溃（4 次全新启动里 2 次，各 1 个 dump）**：均已定位到根因，见下面的
「附带查清」一节 —— 自愈，不影响结果。

**状态盘余量（本轮已处理）**：首启装完后 512 MiB 的默认盘占 **318/488 MB（67%）**，
只剩 160 MB —— overlay 的 upper 就住在这块盘上，撞满会让整套 guest 适配退化成只读。
已在本包 `pack.toml` 加 `[state] size_mb = 1024` 并重建验证（guest 侧 `df` 为
974 M / 可用 640 M，即 34%）。该设置**只在创建时生效**（`up --fresh` 或删除
`vm/state-x6pro.img` 后 `up`）。

**验收命令**（首启要盯的是"进首页"而不是 pid —— pid 在中间一定会变一次）

```powershell
python scripts\dpctl.py up --pack x6pro --fresh --headless
# 等到下面两条都成立，再截图；期间界面停在 splash 是正常的
python scripts\dpctl.py sh -c "grep -c homeModeIconShow=false /userdata/applog/vm-miniapp.log"  :: >=1
python scripts\dpctl.py sh -c "grep -c systemEnvCustomPresetAppInstalled /userdata/applog/vm-miniapp.log" :: >=1
python scripts\dpctl.py screenshot --out vm\x.png                                             :: >100 KB = 首页
```

截图对照：`vm/_x6pro/fb3-home.png`（全新盘首启后的首页，三卡片图片完整）、
`fb3-tap.png`（tap 480 240 → 规范扫描）、`fb3-back.png`（返回首页）。

#### 附带查清：首启期间那个偶发崩溃（2026-10-17）

上一轮记为"偶发、只看日志无法定位"。本轮用 Breakpad **minidump** 把它定位到了。

取样：4 次全新启动里 **2 次**出现（每次 1 个 dump，随后自愈）；温盘启动（`up`，不带
`--fresh`）1 次未出现。dump 在 `/userdisk/corefile/4.3.5/miniapp-*.dmp`（约 370 KB），
宿主侧用两个纯标准库脚本解析（无需 gdb / 无需 guest）：

```powershell
python vm\_x6pro\dmpinfo.py vm\_x6pro\dumps\miniapp-*.dmp    # 流 / 模块 / 异常码 / 崩溃地址
python vm\_x6pro\dmpctx.py  vm\_x6pro\dumps\miniapp-*.dmp    # 崩溃线程寄存器
python vm\_x6pro\elfsym.py  vm\_x6pro\libydhal_devices.so 'fdelt|select'   # 符号与导入
python vm\_x6pro\dis_a64.py vm\_x6pro\libydhal_devices.so 0xa394           # 定位并反汇编
```

**结论**（dump `miniapp-20261017133024.dmp`）

| 项 | 值 |
|----|-----|
| 信号 | `SIGSEGV`（code 11） |
| 崩溃线程 PC | `libydhal_devices.so + 0xa394` |
| LR | 同库 `+0xa334` |
| SP | `0x0000ffff5fffe2a0` |
| 故障地址 | `0x0000ffff60322790` |
| 崩溃模块 | `/usr/lib/libydhal_devices.so`（md5 `025376bef776e12aef522fd77aabecff`，223 904 B） |

反汇编出的是**内联的 `FD_SET`**：

```
0xa36c  ldr x0, [sp,#0x18]      ; 取一个设备描述符
0xa370  ldr w0, [x0,#8]         ; fd 字段
0xa374  add w1, w0, #0x3f
0xa37c  csel w0, w1, w0, lt     ; 有符号除法的取整修正
0xa380  asr w0, w0, #6          ; fd >> 6        <- __FD_ELT
0xa38c  lsl x0, x0, #3
0xa390  add x1, sp, #0x60       ; 栈上的 fd_set
0xa394  ldr x0, [x1, x0]        ; <<< 崩溃：越界读
0xa39c..0xa3bc                  ; |= 1 << (fd & 0x3f); 回写
```

数值自洽：`sp+0x60 = 0xffff5fffe300`，`0x64892 × 8 = 0x324490`，相加**正好**是故障地址
`0xffff60322790` —— 即索引 = `0x64892`，等价 fd ≈ 2 600 万。该库 `.dynsym` 里有
`UND select`，且这段前面是 `cmp w0,#0xf` / `b.ls` 的 **16 次循环**，所以它就是
"对 16 个设备 fd 逐个 `FD_SET` 再 `select()`" 那段。**那个 fd 是没被填过的垃圾值**
（设备在 QEMU 里打不开，字段留着未初始化内存），`FD_SET` 直接把越界写落在栈上。

**为什么间歇**：它读的是**未初始化的栈内存**，每次开机的垃圾值不同 —— 于是有的轮次
越界到已映射的页（无事）、有的落到未映射页（SIGSEGV）。

**为什么不修**：这是固件自己的 bug（`libydhal_devices.so` 里一个已 strip 的本地函数，
偏移 +0xa394），只在设备枚举不全的环境里可达；VM 侧没有合理修法。它**自愈**：guardian
重启后的下一个实例照常装包、进首页，代价只是那一次重启与一个 370 KB 的 dump。
崩溃时刻的应用日志停在 `mediaPlayerService.js === MediaPlayerService onCreate` 之后，
紧接 `YButtonEventAdapter` 那批 `config home_key_id/power_key_id/... read failed`。

**可能的降噪方向（未做、未验证）**：若把该 HAL 要 open 的那 16 个节点在 VM 里补到
"打开成功"，fd 字段就是合法值，越界写自然消失。前提是先弄清它 open 的是哪些节点
（strace 一次 `miniapp` 的 `openat` 即可），未展开。

## 6. 复现

```powershell
python scripts\dpctl.py packs x6pro
python scripts\dpctl.py up --pack x6pro --headless
python scripts\dpctl.py wait --timeout 240
python scripts\dpctl.py screenshot --out vm\melon.png
python scripts\dpctl.py sh --serial -c \"tail -3 /userdata/applog/vm-weston.log\"
```

联网判定这一路的体检命令（§5.7）：

```sh
read -r s < /dev/bes2600; echo $s          # 期望 wifi opend
ps w | grep -E 'wifi-manag|bes2600' | grep -v grep
wpa_cli -iwlan0 status | grep -E 'ssid|wpa_state'
tail -20 /data/applog/wifimgr.log           # runWifiMgr 在 save_core=1 时才落这个文件
```

截图对照：`vm/_x6pro/final-xiaop.png`（云端页，非「需联网」）、`final-wifipage.png` / `final-on-c.png`（Wi-Fi 页「已连接」）、`final-off-c.png`（开关关断后列表收起）。

触摸/滑动这一路的体检命令（§5.8）。`swipe.py` 是 host 侧手势注入器，坐标用**面板像素**，`--steps`/`--ms` 控制步数与步间隔（每步一次 QMP 往返）：

```powershell
python vm\_x6pro\swipe.py 480 350 480 150 --steps 10 --ms 20   # 首页向上翻页
python vm\_x6pro\swipe.py 880 210 250 210 --steps 12 --ms 20   # 应用行横滚
python scripts\dpctl.py sh -c 'tail -20 /tmp/touchbridge.log'  # 期望 down / N×move / up
```

怀疑"事件到底有没有到内核"时，用 evtest 非抢占地和常驻读者并行读同一个节点（桥照常工作，两边都收得到）：

```sh
nohup timeout 8 evtest /dev/input/event1 >/tmp/ev1.log 2>&1 </dev/null &
# 另开一次调用发手势/dpctl tap
grep -E '^Event' /tmp/ev1.log
```

本轮用的离线工具：`vm/work/depclosure.py`（依赖闭包）、`vm/work/debfetch.py`（Debian arm64 取包）、`vm/work/serial_ctrlc.py`（解卡串口）。
2026-10-16 本轮新增：`vm/_x6pro/stubaudit64.py`（ELF64 桩化审计）、`cxxrt-gap.py`（与 libstdc++ 求差集）、`undscan.pl`（guest 侧 ELF64 UND 扫描）、`gdb-abort.sh`（抓 SIGABRT 栈）、`fg-run.sh`（前台跑应用取退出码）、`probe-uinput*.pl`（uinput 传参探针）。
2026-10-17 本轮新增：`vm/_x6pro/swipe.py`（面板像素 → QMP 连续 ABS 的手势注入器，`swipe.py X0 Y0 X1 Y1 [--steps N] [--ms DT]`）。

## 7. 坑

- **Perl 的 `ioctl` 第三参数：给字符串=传地址，给数字=传值。**`UI_SET_EVBIT/KEYBIT/ABSBIT/PROPBIT` 要的是位号本身，写成 `pack('i',$x)` 会让内核把缓冲区地址当位号，一律 EINVAL；而 `UI_DEV_SETUP/UI_ABS_SETUP/UI_SET_PHYS` 确实要指针——两类混在同一个脚本里，失败的那几个不检查返回值就完全看不出来。
- **`/run` 在 overlay 上（状态盘），不随重启清空。**硬断电留下的 `wayland-0` 是 socket 文件，`[ -S ... ]` 判定照样通过，用它当"服务在跑"的守卫会永久跳过启动方；守卫要用 `pidof`。
- **guest 的 perl 只接受 `sSiIlLqQjJfFdDpP(` 后面的 `<` 后缀**，`H<`/`B<` 直接编译报错；写 ELF 解析时 u16 用 `S<`、u8 用 `B`。
- 应用「停在启动阶段、屏幕全黑」不一定是崩溃：本轮那台是 `EXIT=134`（SIGABRT）且 **Breakpad 不落 dump**，`/userdisk/corefile/` 数量不变正是线索；`guardian_run` 会把它伪装成无限重启。先 `kill` 掉 guardian 再前台跑一次拿退出码，比猜快。
- 串口上不要让后台进程继承 stdout（`cmd | tail` 永远等不到 EOF，控制台被卡死）；后台一律 `>/file 2>&1 &`。
- `&` 后面不能紧跟 `;`（busybox ash 报 syntax error）。
- guest 的 busybox `tar` 不支持 `-z`，载荷用未压缩 tar。
- guest 的 lib32（armhf）库会让静态依赖检查误判已满足，算闭包前要先排除 `lib32` 与 `armhf`。
- `adb` 通道可用（`tools/platform-tools/adb.exe`）；SSH 在个别状态下 banner exchange 失败，串口兜底。
- **Perl 长驻进程必须 `$|=1`**：stdout 缓冲会让 print 永不落盘（桥看起来没日志，其实是没 flush）。
- Perl 的 `$1` 会被后续正则覆盖：`if ($line =~ /(event\d+)/ && $name =~ /Tablet/i)` 中第二个匹配清空 `$1` → 先存局部变量再判断。
- `pkill -f 关键字` 会匹配到自己的命令行 → 自杀；用精确 pid 或 `killall`。
- 串口控制台会打乱带变量的长命令；关键步骤拆短，或写成脚本投送后执行。
- 包内多份载荷必须在 initramfs 钩子里逐个投送（只加 `[adapt].replace` 声明不够）——`desktop.tar` 就漏投过一次。
- **「网络通但 UI 说不通」不是矛盾**：这类固件的联网判定是**服务推给 JS 的状态**（`wifi-manager` → D-Bus → `JSWifiProxy::onWifiStatusChanged` → `global.isOnline()`），不是应用自己探测可达性。排查顺序应是先找 `isOnline` / `onXxxChanged` 这些 push 接口的**提供方**，而不是继续调路由和 DNS。
- **模拟字符设备只有 devtmpfs 上的普通文件可用**，读写共用一个文件就得靠回写循环把"命令"翻译成"状态"；循环间隔要短——1 s 会晚吞掉 `wifi-manager` 刚写进去的 `cmd:WIFI_OFF`，UI 永久卡在「正在关闭无线局域网络…」，0.1 s 才走得完往返。循环里用内建 `read -r s < 文件`，别用 `$(cat 文件)`（每 0.1 s fork 两个进程）。
- **`dpctl sh -c "..."` 的外层参数用单引号**：双引号里再套 `$( )` 和 `&` 会被本地 Git Bash 抢走执行——本轮有一条命令在 guest 里没跑，反而在 host 上报 `pidof: command not found`（它去 `D:\Git\dev\bes2600` 建了文件）。要在 guest 里后台跑的东西，先落成 guest 脚本文件再 `sh /path/file`。
- 这台 guest **没有 `nm`/`objdump`/`readelf`/`c++filt`**，也没有 `/usr/local/bin`（往不存在的目录重定向只会静默失败）；符号级取证要么 `strings` 凑，要么把文件拉到 host 用 python 解 ELF。递归 `grep -r /oem`、`/userdisk` 全树会撞 ssh 的 60 s 超时，先缩小到具体 pkg 目录。
- **QEMU 的 input 核心对"没变化的绝对轴"去重**：`input-send-event` 连续几批发同一个坐标，只有第一帧会上报。于是 `dpctl tap` 的按下帧里**没有 ABS**（坐标在上一帧），任何"本帧收到坐标才处理"的翻译层都会把按下吞掉 —— 坐标要当状态存，不能当边沿等。
- **QEMU 的 virtio-tablet 不透传 BTN_TOUCH(330)**：`dpctl tap`/`swipe.py` 都发了 `key number 330`，但源设备 `evtest` 里只有 BTN_LEFT。触摸语义完全由桥合成（这正确），取证时别以为 330 会到 guest。
- **evtest 不加 `--grab` 能和常驻读者并行读同一个 event 节点** —— 判断"到底是哪一段没收到事件"最省事的办法，不用停桥、不用改代码。
- 验证手势必须盯**页面上确实被那个方向驱动的东西**：这台首页是纵向分页（左侧圆点竖排），横拖三张主卡片本来就不动；把误点进扫码页当成"滑动生效"，就会记下一条假通过（§5.8 末段的纠正）。
- **`dpctl put` 热部署过的脚本，结论不算数**：guest 里跑的和包里的文件会悄悄分叉（本轮 §5.6(1) 的"点已通"来自热部署版，包内那版连 tap 都不通）。收尾必须 `down` + `up` 再 `md5sum` 对一次包内外，命令是 `md5sum packs/x6pro/patches/touchbridge.pl` 与 `dpctl sh -c 'md5sum /root/.vm/touchbridge.pl'`。
- **「x6pro 连不上 ssh」不是 x6pro 的问题，是 `UserKnownHostsFile=NUL`**：Git-Bash/MSYS 的 `/usr/bin/ssh` 不认 Windows 的空设备名 `NUL`，会**在当前工作目录建一个真文件叫 `NUL`** 当 known_hosts（本仓库根目录就躺了一个 10 月 7 日的，里面是旧型号的 `[127.0.0.1]:2222 ssh-rsa`）。所有包共用 2222 而每个型号一把 dropbear 密钥，于是换包必刷

  ```
  WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!
  Offending RSA key in NUL:1
  Password authentication is disabled to avoid man-in-the-middle attacks.
  ```

  **`StrictHostKeyChecking=no` 挡不住这段**（它只是"照样放行"），而且顺手禁掉密码/键盘交互认证 —— 依赖口令的客户端就此连不上。修法是让 known_hosts 成为一个真实的、每次 `dpctl up` 清空的专用文件 `vm/known_hosts_devvm`（dpctl.py 的 `SSH_KNOWN_HOSTS`、`ssh-vm.cmd`、`~/.ssh/config` 三处统一指它），写法见 `docs/使用手册.md` §7.1。
- **`dpctl sh` 只回传 stdout，stderr 全被吞**（`guest_run` 只在 rc==255 且命中 `SSH_TRANSPORT_ERRORS` 时才看 stderr）—— 上面那一大段警告在 `dpctl sh` 里永远看不见，所以"dpctl 正常"完全不能当作"ssh 通道健康"的证据。排查这类问题要裸跑一次 `ssh -p 2222 root@127.0.0.1`，或用 `ssh -G` 看 effective options。
- 顺带：客户端还得给 `-i tools/ssh/id_devvm`（`~/.ssh` 下没有默认私钥，客机 `/root/.ssh/authorized_keys` 只认这把）；`~/.ssh/config` 里给 `Host 127.0.0.1` 配段时**不要写 `Port`**，否则 `ssh 127.0.0.1` 的 22 端口也被带到 2222；OpenSSH 10.5 不支持 `Match port`，只能按地址匹配。
- **`.cmd`/`.bat` 里只能写 ASCII**：cmd.exe 按 OEM 代码页读脚本，UTF-8 中文不仅显示成乱码，还会把 `echo` 行拆坏（本轮就报出 `'t' 不是内部或外部命令`）。仓库里那几个 `.cmd` 全英文不是巧合。
- **双击 `.cmd` 用的是系统 PATH，不是 Git Bash 的 PATH**：`ssh` 会解析成 `C:\Windows\System32\OpenSSH\ssh.exe`，或者干脆没有（没装 OpenSSH 可选功能时）。加上 `@echo off` 之后不 `pause`，任何报错都随窗口关闭消失 —— 用户的观感就是"双击没反应/闪一下就没了"，而不是某条具体错误。`ssh-vm.cmd` 现在先 `where ssh` 预检，失败时打印退出码 + 排查顺序再 `pause`。
- **「带命令的 ssh 全绿」不能推出「ssh 能用」**：只有交互式登录才请求 PTY。这台 guest 的固件 userspace 从不分配伪终端，devpts 因此从没被挂上，dropbear 见 PTY 就拒 —— 而 `dpctl sh` / `guest_run` / initramfs 钩子全是带命令的，永远碰不到这条路径。测 ssh 通道要**至少一次不带远程命令**的登录（详见 §5.9）。
- **改了 `scripts/initramfs-init.sh` 必须重打 base**：`python scripts\build-initramfs.py`。每包 initrd 的缓存指纹（`packlib.ensure_initrd` 的 `sig.base`）记的是 `images/initramfs.cpio.gz` 的 md5，不重打 base，`up` 会照旧复用旧 initrd，改动看起来"没生效"。
- `build-initramfs.py` 那三条 `warn: module virtio_ring.ko / virtio.ko / crc32c_arm64.ko not found` **不是回归**：前两个内建进 kernel（`virtio_blk`/`virtio_net` 能 insmod 成功就是证据，它们依赖这俩），crc32c 有 `libcrc32c` + `crc32c_generic` 兜底。init 脚本本来就是"找到 .ko 才 insmod，找不到跳过"。
- 从 Git Bash 模拟"干净系统 PATH"别用 `cmd //c 'set "PATH=..." && ...'`：引号会被截断，`set` 实际没生效，`D:\Git\usr\bin\ssh.exe` 照样抢在前 —— 要写成一个临时 `.cmd`（放 `vm/` 下，反正不进仓库）再跑。
- **QEMU 的一个 QMP chardev 只服务一个客户端**：默认 `up` 会拉 vmgui，它长期占着 127.0.0.1:4446，于是 `dpctl tap` / `swipe.py` 拿到的是 `WinError 10061`（或挂到超时），而 `dpctl screenshot` 因为走 HMP 4445 一切正常 —— 极易误判成"VM 挂了/桥坏了"。注入手势前先关 vmgui（`up --display none`/`gtk`）。

## 8. 版权

镜像版权归**网易有道**，仅供个人学习研究；包内合成器为 Debian 项目发行物（各自许可）。`weston.tar` 与 `images/` 均不进可传播代码包。
