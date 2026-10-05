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

这正是《固件包开发教程》§11 的"全黑判据"（a7pro 同型）。根因：**libyocr 的初始化链
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
- **QMP abs/key 到不了 virtio-tablet**（QEMU 11.1.1）：未深挖（见 §6 遗留说明）。
  本包不受影响；影响的是所有想绕过桥直接用裸 tablet 的方案。
- **adb 未配**：rootfs 是否自带 adbd 未查；SSH/串口已覆盖调试与传文件。
- **语音助手的「开始激活」页**：tap 进去的是语音助手 app 自己的激活页（其业务激活，
  与设备的 S00license 开发凭证是两回事），未再往里验证。
- runDictPen 替代版删除了厂商的"每次清日志"与"崩溃 5 次 reboot"分支；splash 隐藏块
  也已删除（该文件在本固件中**不存在**，find 全盘无——那块代码从第一天起就是死代码，
  当年的黑屏真因见 §4）。

## 附：本轮调试的截图索引（vm/ 下）

| 文件 | 内容 |
|------|------|
| `s7-bisect-noise.png` | 二分实验：fb0 写随机噪声 → 上屏（证明扫描链路通） |
| `s7-fixed5.png` | 第一次出桌面：四卡片（查词翻译/语音助手/AI拍照/随身听） |
| `s7-tap-final.png` | tap 第二卡片 → 语音助手「开始激活」页 |
| `s7-clean-home.png` | 干净启动（重启后）的桌面 |
| `s7-clean-tap.png` | 干净启动后 tap 第一卡片 → 「规范扫描」页 |
