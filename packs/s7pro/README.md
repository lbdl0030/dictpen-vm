# 固件包：s7pro

S7pro（设备树代号 `Y08_CV1813`，**Cvitek CV1826** 平台）的固件包。rootfs 为只读
**erofs**，用户态 32 位 armhf（跑在框架 arm64 内核的 COMPAT 层上）。

**现状（2026-10-05）：可跑。** 引导、桌面 UI 上屏（1020×240 四卡片）、触摸导航、
SSH/串口、假电量、持久化、网络全部就绪（验收截图见 `vm/s7-clean-home.png` /
`vm/s7-clean-tap.png`）。本文记录每个"为什么"，按黑屏 → 触摸 → 网络 → 磁盘的
取证顺序展开。

- [1. 文件来源](#1-文件来源)
- [2. 能力现状](#2-能力现状)
- [3. 与 s6pro/a6pro 的差异与对策](#3-与-s6proa6pro-的差异与对策)
- [4. 黑屏取证：jsapi 断链（已修复）](#4-黑屏取证jsapi-断链已修复)
- [5. 黑屏取证：fb0 有内容但扫描黑（二分记录）](#5-黑屏取证fb0-有内容但扫描黑二分记录)
- [6. 触摸：tslib 等 BTN_TOUCH，桥必须（已修复）](#6-触摸tslib-等-btn_touch桥必须已修复)
- [7. 网络：vendor dhcpcd 在 VM 内核上 SIGSYS（已修复）](#7-网络vendor-dhcpcd-在-vm-内核上-sigsyssigsys已修复)
- [8. 磁盘：runCapFrame 45 分钟写满 4GB（已修复）](#8-磁盘runcapframe-45-分钟写满-4gb已修复)
- [9. dropbear：busybox cp 缺 -r（已修复）](#9-dropbearbusybox-cp-缺--r已修复)
- [10. 已知问题与未验证项](#10-已知问题与未验证项)

## 1. 文件来源

| 文件 | 说明 | MD5 |
|------|------|-----|
| `images/S7-1-pro-ro-system.img` | 只读根（erofs，1.5 GiB） | `234e5d5f6a0be3bd36f8b75f44f9419a` |
| `images/S7-1-pro-ro-boot.img` | boot（16 MiB，仅作参数参考） | `cd005817e4e6271e9ec74a605f01f06e` |

MD5 为本地计算的自校验值。镜像版权归网易有道，仅供个人学习研究。

## 2. 能力现状

| 能力 | 状态 |
|------|------|
| 引导（erofs root + overlay + 4GB 状态盘） | ✅ |
| UI 上屏 | ✅ 1020×240 桌面四卡片完整正立（`vm/s7-clean-home.png`） |
| 触摸 | ✅ uinput 桥（axs_ts），`dpctl tap` 位置正确、UI 正常换页（`vm/s7-clean-tap.png`） |
| SSH（dropbear，arm64） | ✅ 开机自启，宿主 `ssh-vm.cmd` / `dpctl sh` 直达 |
| 网络（eth0 → slirp 10.0.2.15） | ✅ S00net-up（udhcpc），hostfwd 可用 |
| 电池 | ✅ S00battery 假电量 |
| 激活 | ✅ S00license 开发凭证直接进桌面 |
| 串口 shell | ✅ S51console-shell（vendor init 不起 respawn 的兜底） |
| OCR | ⛔ 桩化（无 CVITEK TPU，OCR 功能不可用，其余正常） |
| 设置存储 | ✅ 跟随 VM 状态盘（shim；空间清理与关于→存储容量都显示 1.8GB/6GB，见 §14） |
| 本地翻译（NMT） | ⛔ 补丁缺失，shim 跳过加载；词典在线功能正常（见 §14） |
| adb | 未配（rootfs 的 adbd 情况未查；SSH 已覆盖传输需求） |

## 3. 与 s6pro/a6pro 的差异与对策

| 差异 | 对策 |
|------|------|
| rootfs 是 erofs（s6pro 是 ext4） | `[platform].runtime = "cv18xx-linux-erofs"`（框架现成 runtime） |
| 板型 Y08_CV1813 | `[adapt.replace]` 的 `get_pcba_version.sh` 报 `Y08_CV1813_V0` |
| **OCR 依赖链深**：DT_NEEDED、运行期 dlopen、深层符号三层都要 libyocr | 桩化版 `libyocr.so`/`libYoudaoStitch.so` 整体替换（见 §4，这是 s6pro 方案不够用的地方） |
| **app 走 tslib**（等 BTN_TOUCH），不是 s6pro 的"直接吃 BTN_LEFT" | 框架 touchbridge + 本包 perl-tree（见 §6） |
| vendor dhcpcd 在 VM 内核 SIGSYS 崩，eth0 不 UP | fixup 装 S00net-up（busybox udhcpc）（见 §7） |
| runCapFrame 启动点在 **S01systeminit** 且无 `&` 结尾 | fixup 按 S* 通配 + 两种形态的 sed（见 §8） |
| rootfs 无 dropbear、无 perl | arm64-tree + perl-tree（同 s6pro 思路） |

面板结论与 s6pro 一致：**虚拟屏 = app 的 UI 逻辑尺寸**（横屏 1020×240），cfg_overlay
把厂商的 240/1020 + direction=270 归一化成 1020/240 + direction=0。实测 vinfo 协商
成功：`vinfo xres=1020 yres=240 bits_per_pixel=32` + `hasDBuffer=0`（cfg 的
`dbuffer=false` 生效，fb 只有一页，app 直接画可见缓冲）。

## 4. 黑屏取证：jsapi 断链（已修复）

**症状**：miniapp 常驻不崩（falcon 初始化、解包 app、注册 jsapi 都在日志里），
fb0 里有 ~244736 个非零字节，但 QEMU 截图全黑。

**证据链**（`/userdata/applog/DictPen_*.log`）：

```
dynamic_load_jsapi /etc/miniapp/jsapis/libjsapi_export_core.so
ERROR:libyocr.so: cannot open shared object file: No such file or directory:dlopen in dynamic_load_jsapi
loadQJSModule(global) not found
ReferenceError: could not load module filename 'global'
eval $falcon.launchApp fail / TypeError: not a function
→ 框架退回「验证引导」(8080282888534774)，而它不画东西 → 全黑
```

这正是 [`docs/疑难排查.md`](../../docs/疑难排查.md) §0 的"全黑判据"（按截屏字节数分类，a7pro 同型）。根因：**libyocr 的初始化链
要 CVITEK TPU**（libcviruntime 的 `CviDeviceMem::device_init` 打开 /dev/ion 与
/dev/cvi-tpu0，QEMU 没有），而这台的 OCR 接入比 s6pro 深三层：

1. `libyddal_business.so` / `libyddal_business_core.so` / `libyddal_business_cvplayer.so`
   的 **DT_NEEDED** 拉 `libyocr.so`；
2. jsapis 与 app 包 payload 里有**运行期按名字 `dlopen("libyocr.so")`** 的路径
   （s6pro 只需要处理第 1 层，所以"藏掉 + DT_NEEDED 改写"在它那儿够用）；
3. `libYoudaoStitch.so` 引用 libyocr 的**深层符号**（如
   `_Z23yocr_init_dictpen_model...`），靠全局作用域从 libyocr 解析——它在自己的
   构造器/内部 init 链里就调用（不经过任何导出函数，**桩化导出拦不住**）。

**最终方案**（全部包层，不动框架）：提供一个"在但不工作"的 OCR 栈——

- `patches/libyocr.so` = 真库（从 rootfs 提取）的**全导出桩化版**：903 个导出函数
  就地改写成 Thumb 桩（名字含 init/open/create/load/setup 类返回 -1，其余返回 0；
  `movs r0,#0; [subs r0,#1;] bx lr`），`DT_INIT` 改成惰性标签、`DT_INIT_ARRAYSZ`
  清零（构造器不跑，TPU 路径永远不会被执行）。经 `[adapt.replace]` 覆盖到
  `/oem/YoudaoDictPen/output/libs/libyocr.so`。
- `patches/libYoudaoStitch.so` 同法（304→实为 903 个定义的 FUNC 全部桩化 + 构造器
  中和）。它的 `ydstitch_init` 是**导出符号**（business_core 调它），桩化后 OCR
  初始化立即返回 -1，app 走自带的"OCR 不可用"容忍路径。
- **不改 DT_NEEDED**：桩 libyocr.so 在，原始依赖链、运行期 dlopen、深层符号绑定
  全部自然解析。presetpkgs 的 .amr 也**不需要**动（这正是选这条路的收益——s6pro
  当时改写了 19 个 .amr，本包零改动）。

三个实现层的坑（都踩过，写下来防复发）：

- **ELF32 的 `Elf32_Sym` 是 16 字节**（ELF64 才是 24）。第一次写桩化器按 24 步进，
  "302 个导出"大半是错位读出的垃圾、真入口漏桩，现场表现为 `ydstitch_init` 照跑
  （日志还有 `ydstitch_init time: 1.22803 ms`）然后 `symbol lookup error`。按 16
  步进后是 903 个、一次通过。
- **状态盘 upper 的 whiteout 会挡住 `[adapt.replace]`**：框架的 replace 是 rcS 里的
  S00replace，要求目标 `[ -e ]` 才覆盖；overlayfs 的 whiteout 是字符设备 0:0，会让
  `[ -e ]` 永远为假。fixup 里用 `[ -c ]` 判断后 `rm`（**不能**对 lower 层的普通文件
  跑 `rm -f`——那会制造新的 whiteout，把 S00replace 又挡住）。
- initramfs 阶段的钩子把换掉/删掉的文件清干净（`libyocr.so.off` 历史残留 3MB 也一并
  清），避免每代状态盘带病传递。

## 5. 黑屏取证：fb0 有内容但扫描黑（二分记录）

定位 §4 的过程中做过一组二分实验（之后排查同类问题可直接复用）：

| 实验 | 结果 | 结论 |
|------|------|------|
| `dd if=/dev/urandom of=/dev/fb0` → 截图 | 噪声上屏 | fb0 → QEMU 扫描的整条链路是通的，偏移/bpp 全对 |
| 截图 PPM 尺寸 | 734416 B = 1020×240×3+16 | QEMU 扫描面与虚拟屏一致 |
| `od` fb0 的像素字 | `ff000000` 整屏 | app 在画**不透明黑**（splash 背景），不是"画了看不见" |
| `ps` 里 miniapp 的 fd | fd3=/dev/fb0（整块 mmap）、**无 /dev/dri** | 这台是纯 fbdev 路径（无 falcon-DRM 问题，a6pro 那套 libfalcon 补丁不适用） |

即：**显示侧从头到尾没有问题**，"fb0 有内容但截图黑"= app 只画了黑底（jsapi 断链
后退回的「验证引导」不画内容）。修好 §4 桌面即出。

## 6. 触摸：tslib 等 BTN_TOUCH，桥必须（已修复）

**现象**：S00tp-link 把 `platform-4010000.i2c-event` 链到 Virtio Tablet 后，app 日志
`tsdevice:... open success!` + `winInputLoop open tp success!`，但 `dpctl tap` 之后
UI 毫无反应。

**证据链**：

1. miniapp 的 maps 里有 `/usr/lib/ts/input.so` —— app 走 **tslib**，而 tslib 的默认
   协议是 `EV_ABS/ABS_X,ABS_Y` + `EV_KEY/BTN_TOUCH`；
2. 在 guest 上后台 `cat /dev/input/event1`，宿主 tap 时抓包：**只有
   `EV_KEY/BTN_LEFT` 按下/抬起 + SYN，没有 BTN_TOUCH、没有 ABS**；
3. QMP 侧 `input-send-event` 对 `abs`/`key` 事件都返回成功（无 error），但事件到不了
   tablet 的 evdev：QEMU 的输入路由里 `key` 类型（BTN_TOUCH 是这么发的）落在
   **keyboard**（event0），`abs` 事件同样被丢；只有 `btn` 类型（BTN_LEFT）真的到达
   tablet。`info mice` 确认 tablet 注册为 absolute handler，HMP 的
   `mouse_move/mouse_button` 则是另一条 legacy 路径，在本配置下完全不到达。

**结论**：这台必须有一个**真触摸屏语义**的设备。采用框架现成的 touchbridge 方案
（`[adapt].touchbridge = "patches/touchbridge.pl"`）：桥读 tablet（BTN_LEFT+ABS），
经 uinput 重发布成 `axs_ts` 触摸屏（BTN_LEFT→BTN_TOUCH、坐标按 cfg.json 的 1020×240
缩放——脚本本身是通用的，面板尺寸从 cfg 读，一行不用改）。fixup 负责：

- 装 **perl**（树来自 s6pro 包的 perl-tree，Debian armhf 5.20）：**树内模块目录是
  `5.20.2`，perl 二进制的 @INC 找 `5.20`**，必须补一条版本符号链接；且 vendor 若留
  了同名空目录，`ln -sfn` 会把链接建到目录里而不是替换它——先 `rm -rf` 再 `ln`
  （实测踩过：`Can't locate strict.pm in @INC` → 桥起不来）；
- **S00tp-link 改为"等桥、链桥"**：`vm-tp` 和厂商硬编码的
  `platform-4010000.i2c-event` 都链到 `/dev/input/by-path/axs_ts`（S00 字典序在
  S00touchbridge 之前，所以要等桥的链接出现，最多 12s；桥没起来退化到裸 tablet）。

验证：干净启动后 `dpctl tap 130 120`（第一张卡片）→ 进入「规范扫描」页
（`vm/s7-clean-tap.png`）；此前另一轮 `tap 380 120`（第二张卡片）→ 语音助手的
「开始激活」页——**位置正确**。

> 遗留说明：QMP `abs`/`key` 事件到不了 virtio-tablet 这件事本身（QEMU 11.1.1）没有
> 深挖到根因——a6pro 时代 dpctl tap 是位置正确的，当时与现在的差异未查。对本包无
> 影响（桥从 guest 内部读 evdev，不依赖 QMP abs），但如果以后要在别的包上复用
> "裸 tablet + BTN_LEFT"方案，记得 s6pro 的 tap 也许从来都是"点击落在光标当前位置"。

## 7. 网络：vendor dhcpcd 在 VM 内核上 SIGSYS（已修复）

**现象**：`dpctl sh` 能通（走串口），但 `dpctl status` 的 ssh/adb 全是"open 却连不
上"——hostfwd 的目标根本不存在。

**证据链**：`ip addr` 无 eth0（连 UP 标志都没有）→ vendor 的 S21dhcpcd 跑
dhcpcd 9.4.0，其 seccomp 白名单在 VM 的 Debian 6.1 内核上触发
`Bad system call`（SIGSYS）直接死（串口可见）；slirp 的 hostfwd 没有投递目标，
宿主侧表现为 banner 超时。另有一个干扰项：dropbear 起来后 ssh 仍不通，guest 内
`dbclient` 报 `Network is unreachable`——eth0 无 IP。

**修复**：fixup 装 `S00net-up`：`ifconfig eth0 up` + busybox `udhcpc` 配 vendor 自带的
`/usr/share/udhcpc/default.script`（slirp DHCP → 10.0.2.15，DNS 10.0.2.3）。
验证：串口日志 `S00net-up: dhcp lease ok`；`ifconfig eth0` 有
`inet addr:10.0.2.15`；宿主 `ssh -p 2222` 直达。vendor 的 S21dhcpcd 照跑（它的
dhcpcd 崩就崩，不影响 udhcpc 拿到的租约）。

## 8. 磁盘：runCapFrame 45 分钟写满 4GB（已修复）

**现象**：状态盘 100% 满（`No space left on device`），df 显示 `/userdata/applog`
2.8GB，其中单个 `Capframe_*.log` = **2,981,621,760 字节**，还在涨。

**根因**：runCapFrame/CaptureFrame 的启动点在 **S01systeminit**（`/usr/bin/guardian_run
/usr/bin/runCapFrame`，无 `&` 结尾）——早期按 a6pro 语义只静默 S50launcher* 的
`guardian_run_service ...` 形态，漏了这台。CaptureFrame 在 VM 里 42 分钟 CPU 的
日志写作。

**修复**：fixup 的静默 sed 按 `S*` 通配 + 匹配带不带 `&` 的两种形态；另装
`S00applog-cap` 兜底（`Capframe_*.log` 直接清、`DictPen_*.log` 留最新 10 份）。
验证：干净启动 df 29%，applog 20 个文件。

## 9. dropbear：busybox cp 缺 -r（已修复）

fixup 的 arm64 段把 `usr/lib/aarch64-linux-gnu` 的库拷进 guest 时写的是 `cp -f`：
busybox 对 `dir/.` 源报 `cp: omitting directory ...`，libtomcrypt/libtommath/libgmp
全部没拷进去，dropbear 报 `libtomcrypt.so.1: cannot open shared object file` 起不来
——**SSH 从未通过**（之前的会话全靠串口）。改成 `cp -rf` 后正常；S00dropbear 里再
显式给 `LD_LIBRARY_PATH` 双保险。验证：开机 `[34.98] S00dropbear: up`，宿主 ssh 直达。

## 10. 已知问题与未验证项

- **OCR 不可用**（设计如此）：libyocr/libYoudaoStitch 是桩，拍题/扫描类功能不可用；
  其余功能正常。若将来要真 OCR，需要给 VM 造 /dev/ion + /dev/cvi-tpu0 的等效物。
- **QMP `key` 到不了 virtio-tablet，但 `abs` 到得了**（2026-10-20 抓包更正本条旧说法）：
  原文写的是"`abs`/`key` 都到不了"，**测错了** —— 同一台、同一条 QMP 会话，四种批次形状
  （按下与移动同批 / 分开，各两个新位置）**4/4 都在 `/dev/input/event1` 抓到
  `ABS_X`+`ABS_Y`+`BTN_LEFT=1`+`BTN_LEFT=0`**；只有 `key`(BTN_TOUCH=330) 那一批 event1 全空
  （它落在 event0 键盘）。对本包无影响（桥从 event1 读 BTN_LEFT+ABS 自己换算），
  但它把"能不能绕开桥直接用裸 tablet"这件事的判断基线挪了位置。复验：
  `vm/_qwen_s7_btnshape.py`（A/B 四种形状对着抓包）、`vm/_qwen_s7_evprobe.py`（一次 tap 同时录
  event1 与桥发布的 `axs_ts`，按时间戳排）。⚠ 两个脚本都要**先收掉控制台**（QMP 单客户端）。
- **adb 未配**：rootfs 是否自带 adbd 未查；SSH/串口已覆盖调试与传文件。
- **语音助手的「开始激活」页**（2026-10-18 往里验了一步，见 §11）：tap 进去的是语音助手 app
  自己的激活页（其业务激活，与设备的 S00license 开发凭证是两回事）。整页纯黑 + 一颗按钮，
  **没有 ×、没有返回**；`sendkey esc`/`backspace` **完全无效**（厂商应用不吃键盘）。
  点下去进「配置网络」，那页左上角的 `<` 可以退回。
- **`[adapt].ap` 原来是 `false`，本包是七包里唯一关掉虚拟热点的**（2026-10-18 已翻成 `true`，见 §11）。

## 11. 虚拟热点 `ap`：为什么当初关、翻开之后实测差在哪（2026-10-18）

`[adapt].ap` 原值 `false`，注释写"热点先不开（视觉验证用不到）"——**当年是按"只看 UI"的需求关的，
不是因为它坏了**。这次为了走通激活流程翻开，两态对照（同一台、同一份 `state-s7pro.img`）：

| 观测量 | `ap = false` | `ap = true` |
|---|---|---|
| `/root/.vm/ap/`（hostapd + conf + iptables + ld-linux） | 不存在 | 存在，hostapd 跑起来了 |
| `iw wlan1 info` | `type managed`、无 ssid | `ssid Youdao-VM` |
| `wpa_cli -i wlan0 scan_results` | **0 个 AP**（只有表头） | **能扫到 `Youdao-VM`** |
| 「配置网络」页 | **「正在搜索网络.」永远转不完** | 应当列出该 SSID |

**代价（要付的三条）**：① 起机多拉一个 aarch64 hostapd（经 `ld-linux --library-path` 起）+
iptables/NAT 一段；② **本包所有起机时序数字都是在 `ap=false` 下量的**（框架侧控制台那批
`33~185 s`、`UI_ATTACH_GRACE_S` 一类判据），翻开后**要重测**；③ `wlan1` 会占住 `10.201.126.0/24`。

**2026-10-21 更新（Q112）**：`wlan0` 原来扫到 AP 后仍停在 `SCANNING`，因为本包
`wpa_supplicant.conf` 只有占位 SSID。框架的 S00wifi-ap 现在会补 `Youdao-VM` 开放网络块，
S99wifi-link 等 wpa_supplicant 起来后 `select_network` + `reassociate`；重启实测
`wpa_state=COMPLETED`、`ip_address=10.201.126.x`，应用日志 `onInternetConnectChanged connect=1`。
业务激活仍要 `sn`/`uuid`（`dpctl sysinfo` 要人给 `--sn --uuid`，框架不自己编；本包
`system_info.bin` 里第一个串是 `02:00:00:00:00:00`）。**Wi-Fi 通了 ≠ 激活能过，后半段仍未验证。**
- runDictPen 替代版删除了厂商的"每次清日志"与"崩溃 5 次 reboot"分支；splash 隐藏块
  也已删除（该文件在本固件中**不存在**，find 全盘无——那块代码从第一天起就是死代码，
  当年的黑屏真因见 §4）。

## 12. 「右侧画面被截、触控仍是原画面位置」—— 2026-10-20 复核：**没复现**

人点名这条时先按"画面被截 → 触控跟着偏"去查，四个口径先对齐，**全都是 1020×240**：

| 口径 | 取值方式 | 实测 |
|---|---|---|
| 客机 framebuffer | `cat /sys/class/graphics/fb0/virtual_size` / `modes` | `1020,240` / `U:1020x240p-0` |
| app 认的屏 | `/etc/miniapp/resources/cfg.json` 的 `width/height/direction` | `1020 / 240 / 0` |
| QEMU 扫描出来的整帧 | `dpctl` 里 `read_ppm(screendump())`（**不裁剪**，`--w/--h` 会裁） | `1020 x 240`，734400 B |
| 包声明 | `pack.toml [display] xres/yres` | `1020 / 240` |

**"右边少一块"来自固件首页自己的可横滑卡片行**：4 张卡每张 ~310 px、间距 ~5 px，整行 ~1270 > 1020，
所以第 4 张（随身听）只露个边。**滑一下就看得到它完整的样子**（`vm/_qwen_s6_drag.py 900 120 200 120`
→ `vm/_s7_paged.png`：滑到底第 4 张完整、再往右就是黑边，说明整行只有 4 张，不是被切掉）。
对照：随身听自己的四宫格（听力精选/云盘社区/有道云盘/我的）在 1020 里**摆得下**（`vm/_s7_console2.png`）。

**触控按"看得见的地方"命中，右侧也验了**：「配置网络」页最右那颗 Wi-Fi 开关（x≈893）
`dpctl tap 893 122` 一次就 ON→OFF、再点一次 OFF→ON（`vm/_s7_wifi_toggle.png`）；
桥的换算也是准的 —— tablet 收到 `ABS_X=16062/ABS_Y=16384`，桥发给 app 的是 `500/120`
（面板 1020×240，`vm/_qwen_s7_evprobe.py` 抓的）。控制台那侧：绘制与点击逆变换用的是**同一个**
`ViewTransform`（`vmgui/view/canvas.py:146` 画在 `t.ox/t.oy`，`window.py:2054` 用 `t` 反解），
所以就算窗口窄到画面被裁，点下去仍跟着画面走，不会"按原画面位置"。

**所以这条先记"未复现"**，不是"已修"。要复现请给**具体那一屏**：
① 是首页还是某个 app 页；② 当时工具条「缩放」在哪一档、窗口多宽；③ 点的是哪个目标、
落到了哪儿（有截图最好）。拿到这三条就能直接对着 evdev 抓包比，而不是猜。

**同日再复核（按"在控制台里看"的方式，`vm/_s7_console.png` + `vm/_s7_sweep.png`）**：

- 控制台里 **1020×240 整帧都在**，上下黑边是画布比画面高，不是裁。
- 首页是**焦点轮播**，逐列量出来：焦点卡 **12..320（宽 308）**，其余三张 **325..553 / 558..786 /
  791..1019（各 228）**，4 张正好铺满 1020。**AI拍照 在 558..786**（我先前把 638..951 当成了它）。
- `tap 170 / 480 / 794` → **查词翻译 / 语音助手 / 随身听**，与画在那三个 x 上的卡一一对应 → 映射没偏。
- 控制台点击链路：canvas 上反算 (794,120) → `session.pointer(794,120)`，一个像素不差。

## 附：本轮调试的截图索引（vm/ 下）

| 文件 | 内容 |
|------|------|
| `s7-bisect-noise.png` | 二分实验：fb0 写随机噪声 → 上屏（证明扫描链路通） |
| `s7-fixed5.png` | 第一次出桌面：四卡片（查词翻译/语音助手/AI拍照/随身听） |
| `s7-tap-final.png` | tap 第二卡片 → 语音助手「开始激活」页 |
| `s7-clean-home.png` | 干净启动（重启后）的桌面 |
| `s7-clean-tap.png` | 干净启动后 tap 第一卡片 → 「规范扫描」页 |
| `_s7_act_tap.png` | 2026-10-18：「开始激活」点进去的「配置网络」页（`ap=false` 时「正在搜索网络.」永远转不完）。图已在本机清理中丢失（未入库，无法恢复），现象与结论见本节 |
| `_s7_ap4.png` | 2026-10-18：`ap=true` 之后同一趟走查，进了「查词翻译」子页（拍照翻译/键盘查词/语音查译）。同上，图已丢失 |

## 13. 实体键（2026-10-20）

- **能力**：Home，码 **102**（`Y08_CV1813_V0.home_key_id = gpio-keys`）；本包固件没有音量键项。
- **做法**：`patches/vm-keys.pl` + `vm-keys-apply` + `vm-keys.env` + `vm-keys.sh` 钩子；
  板型串 `Cvitek Cv1826 for Youdao Y08 V0 version`；钩子在 `S01systeminit` 的
  `guardian_run runDictPen` 行前插入 `vm-keys-apply`（首 app 之前、显示初始化之后）。
- **验证**：冷启动后查词翻译 → 桌面（`vm/_s7_persist_after.png`）；控制台工具条只亮 Home。
- 快速启动可选：`dpctl fast save --pack s7pro`（无音频，见 README 与台账 Q93）。

## 14. 设置存储跟随 VM 状态盘（Q104，2026-10-21）

**症状**：空间清理页的容量不跟状态盘走 —— 4G 盘显示 1.3GB/4GB；把状态盘扩到 6G 后
总容量仍是 4GB、已用变成 **-0.8GB**（负数）。

**根因两层**：
1. 厂商 `YSystemInfoAdapter::getStorageInfo` 返回的是写死的机型容量/分区账本，不是
   `statvfs`；盘变大后分子（已用）按真实文件算、分母不动，就出负数。
2. 这台 app 是 **ELF32 armhf**（`/usr/bin/miniapp`、`libyddal_adapter_core.so` 都是
   `ld-linux-armhf.so.3`/glibc 2.33）。x6pro 的 AArch64 shim 在这里根本不会被加载。
   armhf 的 ABI 也不同：`getStorageInfo`（public/private 两个 mangled 名）是
   **r0=sret、r1=this**，`std::string` 24 字节。

**做法**：`patches/vmstorage.c` 用 Zig 编成 `libvmstorage-armhf.so`
（`zig cc -target arm-linux-gnueabihf -shared -fPIC -O2 -nostdlib -L vm/_s7root/lib -lc`（`vm/_s7root/` 为清理后不保留的本机 sysroot，配方照抄即可），
对 s7 的 glibc 2.33 动态链接），覆盖两个 `getStorageInfo` 符号，返回
`statvfs("/userdata")` 的 `{totalSpace(GB), user(used MB), available(free MB)}`。
`miniapp-wrapper.sh` 用 `LD_PRELOAD=/usr/lib/libvmstorage-armhf.so` 起
`/usr/bin/miniapp.real`；`runDictPen.sh` 在首 app 前把原 `/usr/bin/miniapp` 挪成
`.real` 并装上包装器。

**安装链的坑**：`[adapt.replace]` 走 rcS 的 `S00replace`，它只覆盖**目标已存在**的文件
（`[ -e "$dst" ] || continue`）。`/usr/lib` 这两个是新增文件，initrd 里带了、replace.list
也写了，boot 后仍然没有。因此 fixup.sh 第 11 节直接拷进 `$TARGET/usr/lib`；
pack.toml 里那两行只负责把文件带进 initrd。串口验证：
`S00replace: libvmstorage-armhf.so -> /usr/lib/libvmstorage-armhf.so rc=0`。

**NMT 的副作用（同一份 shim）**：app 启动时 `YAsyncLocalTranslatorPrivate::loadNmtPatch`
找不到 `/userdisk/uresource/localdict/patchV2.bin`（rootfs 只有 nmt 模型，没有补丁），
抛 `youdao::mini_nmt::MiniNmtException` 直接 abort（core 栈在 `libmini-nmt.so`）。
shim 里把 `_ZN28YAsyncLocalTranslatorPrivate12loadNmtPatchEv` 做成 no-op 跳过加载；
**代价是本地翻译功能不可用**，词典/在线翻译不受影响。

**验证**（2026-10-21，6GB 状态盘）：
- `/usr/lib/libvmstorage-armhf.so` 在、`/usr/bin/miniapp` 是包装器、`.real` 在跑；
  `grep libvmstorage /proc/<miniapp.real>/maps` 命中；`df -h /userdata` = 5.9G/1.3G 用。
- 设置 → 空间清理：**已使用 1.8GB/6GB**（`vm/_s7_pack_storage.png`）。
- 设置 → 关于 → 存储容量 → 已用存储空间：同样 **1.8GB/6GB**（`vm/_s7_pack_about_storage.png`）。
- 单测：`vmgui.tests.test_units.TestS7StorageShim`（ELF32/机器 40/三个符号/fixup 安装链）。

## 声音（2026-10-22）

`[audio] cards = 2`：厂商播放链路按 card 序号打开设备，只有 `card0` 时
SoundPlayer 报 ALSA `Invalid value for card`。补第二块 `usb-audio` 后
`dpctl audio-check` 通过（端点峰值约 34.5%~34.9%）。SoundPlayer 由厂商 init
自启，本包不需要额外脚本。随身听的 USB 上传目录是 `/userdisk/Music/`（大写 M）。
