# p5 —— 词典笔 P5 / YDPP5-1 / Almond 板 / RK3566 / OS 3.5.0

**P 系产品线首台**。来源：固件库 `Almond-OTA-OS99.99.91` 全量 OTA（ABUP 测试通道号；
rootfs `/Version` 实测 **3.5.0，Build 2025-12-18**）。

## 型号判定

- install 产品注册表：`YDPP5-1 → youdao-OVERHEAD_P51_SKU_CHN_PRO`（productId 1654930955）
- DTB：`Rockchip RK3566 Youdao Almond Board`（rootfs 映射表 → `Almond_V0`）
- 外部 deviceConfig：词典笔P5，3.68 英寸，RK3566，32GB，1350mAh

## 与 x6pro 同代同构

RK356x + 原始 ext4 + weston + miniapp（**xdg-shell 代**，非 x3s342 的 wl_shell）+
aarch64 glibc。面板同为 480×960 竖屏 DSI（横用 960×480），cfg.json 逻辑屏同为
`960×266 / yoffset=107 / direction=270`。适配直接克隆 x6pro 套路：Debian weston 10
（pixman + kiosk-shell）载荷、S50vm-ui 带 model bind、S45vm-wifi、通用 touchbridge。

## 切分（RKAF @ 0xb4a26，偏移相对 RKAF）

| 分区 | 绝对偏移 | 大小 | 落点 |
|---|---|---|---|
| boot_a | 0x106b226 | 12 599 296 | `images/p5-boot.img`（FIT：kernel + 多板 DTB，板级 DTB 在 +0x800） |
| system_a | 0x1c6f226 | 2 097 152 000 | `images/p5-rootfs.img`（原始 ext4；本 OTA 无 uresource 分区） |

## 固件事实（建包取证）

- UI 链：`weston -B=drm-backend.so --tty=2` → wait wayland-0 → `runDictPen`（内部到 miniapp）
  → `wait_miniapp_finish` → `runResourceManager`；rcS 里另有 S22syslogd/S10udev/S30dbus/S41dhcpcd/S80dnsmasq
- 音频（youdao_hal_audio_config `Almond_V0` 节）：mic=card0/sub1、**speaker=card1**、
  数字耳机探测 `/sys/class/sound/card2` → `cards=3`；`speaker_vol_ctl_numid` 原值 6，
  prepare.sh 按 QEMU usb-audio 布局改成 3
- 按键（hal_key_config `Almond_V0` 节）：电源=platform-fdd40000.i2c-rk805-pwrkey（116）、
  **home=platform-adc-keys-event（115）**、抬头键=led_control（134）——与 A 系（gpio-keys 102）、
  Melon（adc 键表）都不同，fake-keys 未做
- tp：`/dev/input/by-path/platform-4020000.i2c-event`（同 x3s342），cfg_overlay 覆盖成 axs_ts
- NPU：rootfs 带 `Almond-rec-*.rknn` OCR 模型（RK3566 NPU，VM 无——离线 OCR 大概率不可用，待实测）

## 首启实测（2026-10-24）

**合成器链已通，应用卡在 NPU 依赖的 OCR 初始化。** 过程与结论：

1. **weston 载荷三连修**（S50vm-ui/prepare.sh 已固化）：
   - x6pro 那份 bookworm 载荷直启报 `GLIBC_2.32/2.33 not found`（P5 系统 glibc 只有
     2.29）→ 必须经载荷自带 loader 起；
   - loader 路径还要带上 `usr/lib/aarch64-linux-gnu/weston`（libexec_weston.so.0
     在那），且模块目录 `libweston-*`/`weston` 要拷到编译期写死的系统路径；
   - **weston 10 也不行**：P5 的 libfalcon 只引用 `wl_shell_interface`（0 个 xdg），
     weston 10 把 wl_shell 编译掉了，应用建窗后 SIGSEGV（exit 139）——**必须
     Weston 9**（x3s342 同病同修）。现用双载荷：weston.tar（bookworm 运行库+loader）
     + weston9.tar（weston 9 本体），S50vm-ui 按 x3s342 方式启动。
2. **wayland 窗口已建立**：`WLNativeWindow::setSurfaceSize(960,480)` →
   `setSize(960,266)` → `setPosition(0,107)`，与 cfg.json 逻辑屏完全一致；
   kiosk 灰底常驻（截图 `vm/shots/_p5_boot7.png`）。
3. **当前阻塞：OCR 栈强依赖 RK3566 NPU**。miniapp 初始化走到 OCR 线程加载
   `Almond-rec-*.rknn.encrypt` 模型时 SIGSEGV（breakpad dmp 落在
   `/userdisk/corefile/3.5.0/`）；把 ocr 模型目录挪走后走到
   `TornLineRKNN: model path is empty` 又空指针崩——**喂模型崩在 NPU、饿模型
   崩在空路径**，两条路都死。应用其余模块（声音/TTS/sysconfig）在崩溃前都正常
   运行过。

### 后续路径（专项）——已于同日闭环 ✅

崩溃链按"打印前最后一行 + breakpad dmp（minidump 库解析崩溃线程 pc/模块）"逐个定位，
共四层桩化，全部在 `[adapt.replace]`：

1. **libyocr.so + libYoudaoStitch.so 整库导出桩化**（s7pro §4 / x6pro §5.3 方案）——
   但只桩导出接口不够：libYoudaoStitch **保留的构造器**（中和构造器会 SIGILL，x6pro
   实测）自己起线程走内部代码继续加载 RKNN 模型；
2. **libydhal_devices.so 的 `gsensor_get_data` @0x1c65c 桩为 return 0**——G 传感器
   轮询线程在传感器描述表上越界解引用（dmp：崩溃线程 pc=libydhal+0x1c84c）；
3. **librknnrt.so 桩化**（/usr/lib 与 /oem/.../libs 两份同 md5 副本都换）——rknn_init
   秒回失败，但调用方不检查错误继续用空上下文，仍崩；
4. **libyshake_class.so 整库桩化**——ShakeClassRKNN（摇一摇分类）住在这库，第 3 层
   修完它接着崩；整库桩掉后 **UI 全出**。

**触摸桥换 C 版**（x3s342 的 touchbridge.bin，参数 `959 265`）：P5 的 buildroot
没有 perl，框架自带的 perl 桥起不来（`setsid: can't execute /usr/bin/perl`）。
S50vm-ui 起桥后把 vm-touch 软链到 cfg 的 tp 路径 axs_ts。

**验证结论（2026-10-24）**：起机 → weston9 → **完整 UI**（60k 色，`vm/shots/_p5_stub5.png`）
→ **触控可用**（tap 480,240 画面变化 46%，`vm/shots/_p5_tap3.png`）；应用常驻（2.5G VSZ），
云端上报正常（appUpdateInfoReport code:0）。代价：OCR/扫描/摇一摇等 NPU 依赖功能
整体不可用（桩化标记失败）。

### 声音（2026-10-24 实测）

- **双卡放音通**：客机 aplay（本机自带 alsa-utils）48k 立体声正弦 → 宿主端点峰值
  card0/card1 各 **80.4%**——QEMU usb-audio → 宿主链路两条卡都通。
- **音量补丁生效**：prepare.sh 把 Almond_V0 节的 `speaker_vol_ctl_numid` 6→3；
  实测 card1 的 numid=3（'Audio Output Volume Control Playback Volume'）在位且值为
  240/256（≈94%，sysconfig/hal 初始化写入）。
- SoundPlayer 由 guardian 常驻。
- **应用内媒体播放未验**（随身听需要内容上传，沿用仓库惯例单独验证）。

## 复验（2026-10-25，人点名实机测，不测声音）

- 冷启动约 95 s 出首页（960×480，窗口 960×266@y107），与 10-24 一致。
- 触控可用：`tap 130 255` 从首页进「规范扫描」引导页；再点扫描示意区没有反应也没有崩，
  NPU 桩继续有效（这一轮无新 corefile，应用常驻 2.3 GB VSZ，pid 未变）。
- fake-keys 仍未做：客机没有 `/tmp/fake-keys.fifo`，`vm-keys.env` 与 helper 都不在包里，
  控制台那几颗键对这台是灰的。
- 旧 corefile（`miniapp-20261024220519.dmp` 等）是 10-24 桩化前的历史件，别当这一轮的崩溃。

## 存储页跟随状态盘（2026-10-25）

- 两个入口：设置 →「空间清理」，以及 设置 →「关于」→「存储容量」→「已用存储空间」。
  两边都读厂商 `YSystemInfoAdapter::getStorageInfo`（`/oem/YoudaoDictPen/output/libs/libyddal_adapter.so`，
  ELF64；两个 mangled 符号与 x3s342/x6pro 一致），出厂值不跟随 VM 的状态盘。
- 做法同 x3s342：`patches/vmstorage.c` → `libvmstorage.so`（aarch64），`prepare.sh` 落到
  `/userdata/libvmstorage.so`，`S50vm-ui` 在起 miniapp 前 `export LD_PRELOAD`，
  `pack.toml` 的 `[adapt].extra` 加一份。返回用 `statvfs("/userdata")`。
- 实测（2G 状态盘、已用 664M）：两处都显示「已使用0.7GB/2GB」
  （截图 `vm/shots/_p5_storage.png`、`vm/shots/_p5_about2.png`、`vm/shots/_p5_storage2.png`）。
- 一条操作坑：这台的应用窗口逻辑区只有 **y=107..373**，在控制台/触摸注入里滑动必须在这个区间内起手；
  从 y=420 起手滑不动，会误判成"设置只有 6 项、没有关于"。

## 待办（后续完善）

- [ ] 应用内媒体播放实测（链路与音量已验，见「声音」节）
- [ ] fake-keys（rk805-pwrkey + adc-keys 115 + led_control 134）
- [ ] 摇一摇之外可能还有零星 NPU 调用点（功能性页面实测时按同法处理）
- [ ] 冷启动复验 + 文档登记（README 机型表、盘点 §12.22）
