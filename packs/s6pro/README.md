# 固件包：s6pro

S6pro 词典笔固件在 dictpen-vm 里的包。设备代号 **CoCo-1826**（`etc/version`），
Cvitek **CV1826** 平台，rootfs 为只读 **ext4**（`S6-1-pro-ro-system_b.img`），
用户态 **32 位 armhf**（跑在框架 arm64 内核的 COMPAT 层上）。

- [1. 文件来源](#1-文件来源)
- [2. 与 a6pro 的差异与本包的对策](#2-与-a6pro-的差异与本包的对策)
- [3. 面板与触摸参数的证据链](#3-面板与触摸参数的证据链)
- [4. 适配现状](#4-适配现状)

## 1. 文件来源

| 文件 | 说明 | MD5 |
|------|------|-----|
| `images/S6-1-pro-ro-system_b.img` | 只读根（ext4，1.6 GiB） | `6e3b2107e37be3dbccc203ac79cb0f33` |
| `images/S6-1-pro-ro-boot_b.img` | boot（FIT：cvitek 内核 + 4 块板 DTB，仅作参考） | `3f85a6da16fd0ec8a1afb67a5436f264` |

MD5 为本地计算的自校验值。镜像版权归网易有道，仅供个人学习研究。

## 2. 与 a6pro 的差异与本包的对策

| 差异 | 对策 |
|------|------|
| rootfs 是 ext4，不是 erofs | `[platform].runtime = "cv18xx-linux-ext4"`（框架现成 runtime，`DICTPEN_ROOTFSTYPE=ext4`） |
| 板型由 `get_pcba_version`（读 `/proc/device-tree/model`）决定，决定 S01panel 的 insmod 参数与 **S50-app-launcher 是否拉起 UI 栈** | `[adapt].replace` 用 `get_pcba_version.sh` 覆盖 `/usr/bin/get_pcba_version`，报告 **Plum_V0**（证据见下） |
| 这台机器的 `S50launcher` 只拉起 hal 服务（wpa_supplicant/wifi-manager/bluetoothd…），**应用由 `S50-app-launcher` 启动** —— 与 a6pro 相反 | 框架会按 a6pro 语义把 S50launcher 改名禁用；`hooks/fixup.sh` 把它改回来 |
| rootfs **无 dropbear** | SSH 不可用：`fixup.sh` 装 `S00adbd`（TCP adb，guest 5555），`dpctl sh/put/get` 走 adb；串口 shell 由 console-loop 兜底（同 x5pro：vendor busybox init 在 rcS 后不起 respawn 条目） |
| rootfs **无 perl** | 框架的 touchbridge 钩子跑不了（它硬编码 `/usr/bin/perl`）—— 触摸问题的处理见第 4 节 |
| 电池读 `/sys/class/power_supply`（hal-battery → libydhal_devices.so，字符串证据） | `[adapt].battery = true`（框架 S00battery 假电量钩子，同 a6pro） |
| `cfg.json` 有 `tp_direction/tp_xoffset/tp_yoffset`（a6pro 没有） | `cfg_overlay` 一并归零：虚拟触摸直接报 UI 像素 |

## 3. 面板与触摸参数的证据链

**S6pro = Plum_V0 分支**（CV1826 板型四选一：Cherry V0 / Cherry V1 5G / Melon V0 / Plum V0）：

1. 固件 `cfg.json`：`screen.width=260, height=640, direction=270, tp_xoffset=110`；
2. `S01panel` 的板型分支里 **只有 Plum** 是 `cvifb start_x=110 width=260`（Cherry=254/113、
   Melon=266/107），且只有 Plum 分支 insmod `hynitron_ts.ko`（I2C 触摸）——与 cfg.json 的
   `tp=/dev/input/by-path/platform-4020000.i2c-event`（I2C 事件节点）吻合；
3. boot FIT（`S6-1-pro-ro-boot_b.img`）里带 4 块板 DTB，`cv1826_zt_0005a_plum` 的
   FDT 字符串为 "Cvitek Cv1826 for Youdao Plum V0 version"，与 `get_pcba_version`
   的板型表一一对应。

**结论（2026-10-04 实测修正）**：逻辑屏是 **横屏 640×260**，所以 `[display] xres=640
yres=260`，并且 `cfg_overlay` 要**同时**覆盖 `width=640` / `height=260` 和
`direction=0` —— 三者必须一致，原因见第 4 节的「画面曾被转过去」。

## 4. 适配现状

| 能力 | 状态 |
|------|------|
| 引导（ext4 root + overlay + 状态盘） | ✅ |
| UI 上屏 | ✅ 640×260 完整正立（`vm/s6pro-landscape.png`） |
| 触摸 | ✅ `dpctl tap 110 130` → 进「规范扫描」页（`vm/s6pro-tap.png`） |
| 设置存储 | ✅ 跟随 VM 状态盘（shim；空间清理与关于→存储容量都显示 0.5GB/8GB，见 §14） |
| 本地翻译（NMT） | ⛔ localdict 缺失，shim 跳过补丁加载；词典在线功能正常 |
| adb | `S00adbd`（TCP），`dpctl adb` |
| SSH | 不可用（无 dropbear；串口 shell 兜底） |
| 电池 | S00battery 假电量 |
| 激活 | 需再验（日志有 `yd_misc_info open fail`；模型已能识别成 `YDPS6-1`） |
| 热点 | S00wifi-ap（Youdao-VM） |

触摸最终没走"给 guest 配 perl + 框架 touchbridge"那条路：**这台 app 直接吃
Virtio Tablet 的 `BTN_LEFT`**，fixup 钩子把 tablet 的 event 节点链到 cfg.json 的
tp 路径（`/dev/input/by-path/axs_ts`）就够了，所以 `[adapt].touchbridge` 不需要开。

## 5. 画面曾被"转过去"（2026-10-04 修复）

**症状**：屏幕要么全黑（miniapp 起不来），要么像被转过 90°／只画了左上角一块——
竖屏 260×640 的 framebuffer 上，只能看到 640 宽横屏版式的左半截。

**根因**（两条，都是实测）：

1. **fb 尺寸必须等于 `cfg.json` 的 `screen.width/height`**。这台 app 走 **fbdev**
   （`/dev/fb0`），启动时读 cfg.json 再 `FBIOPUT_VSCREENINFO`：

   ```
   vinfo xres=640 yres=260 ... bits_per_pixel=32      ← 当时虚拟屏是 640x260
   fb vinfo diff, try FBIOPUT_VSCREENINFO
   Error putting variable information with bits_per_pixel(32)
   Error putting variable information with bits_per_pixel(16)
   Screen size overstep.
   ```

   失败即退出 → guardian 反复重启 miniapp（串口刷 `start /usr/bin/runDictPen`）→ 全黑。
2. **UI 版式是横屏 640×260**，而厂商 cfg.json 的 `screen` 是竖屏 `260×640` +
   `direction=270`（面板竖扫 + 显示引擎旋转）。只把虚拟屏改成 260×640 让它不崩，
   它就以 `direction=0` 按 1:1 把 640 宽的版式写进 260 宽的缓冲——右侧裁掉、下方留白。

**修复**：让"虚拟屏 = app 要的 fb = UI 版式"三者都是 640×260：

- `pack.toml`：`xres=640 yres=260`；
- `patches/cfg.json.overrides`：`width=640` / `height=260`（转置厂商值）+ `direction=0`
  （VM 的 bochs-display 没有平面旋转，UI 必须 1:1 写 framebuffer）。

**验证**：fb0 `640,260 / stride 2560`；cfg.json 覆盖后为 `width 640 / height 260 /
direction 0`；miniapp 常驻不重启；截图三张卡片（查词翻译 / 语音助手 / 有道随身听）
完整正立（`vm/s6pro-landscape.png`，修复前被裁的样子见 `vm/s6pro-portrait.png`）；
`dpctl tap 110 130` 正常进「规范扫描」页（`vm/s6pro-tap.png`）。

**教训**：这台机器**不能用"面板扫描方向"当虚拟屏尺寸**。判断依据是 app 的
`vinfo` 请求（它认 cfg.json），不是 DTB/`cvifb` 的扫描参数——`cvifb start_x=110
width=260` 描述的是真机显示引擎的旋转窗口，VM 里没有这一层。

## 6. 触摸光标缩放修复（2026-10-06）

**症状**：进卡片类大目标"看运气"（有时点得中），小目标（返回箭头、键盘查词按钮）
永远点不中。

**根因**：这台 app 的 UI 光标 = `/dev/input/mouse0` 的增量（mousedev 字节流），
点击发生在光标当前位置。mousedev 把 ABS 增量换算成像素用的 xres/yres 是 Debian
内核默认的 **1024×768**，与本机面板 640×260 不符 —— 每次位移都差 41 倍，光标
根本到不了目标。

**修复**：fixup 追加 `S00mouse-scale`——在 miniapp 之前把面板尺寸写进
`/sys/module/mousedev/parameters/{xres,yres}`（运行时可写，实测）。PANEL 取自
pack.env。与 x5pro 包 2026-10-06 的修法相同（两台的 app 同一族输入代码）。

**验证**（`vm/s6fix-*.png`）：首页 tap (110,130) → 查词翻译页；tap (32,28) →
关掉规范扫描浮层回到词典主页；tap (450,130) → 精确命中「键盘查词」弹出键盘
—— 三连击全部位置正确。

## 7. 「设置 → 关于」点开全黑（2026-10-20 实测并修复）

**症状**（人点名：「s6pro应用功能问题，如设置关于界面缺失」）：设置能打开、列表一直翻到底都有
「关于」这一项，**点下去整屏纯黑**，没有返回键（`dpctl tap 32 28` 无效，只能
`miniapp_cli start 8080222437664451` 把首页拉回来）。

**直接证据**（`/userdata/applog/YD_PEN_APP.log`，2026-10-20 07:35 那一枪）：

```
fpath(/etc/miniapp/resources/framework/js_modules/settings.js.bin) open failed, errno 2.
ReferenceError: could not load module filename 'settings'
fpath(…/framework/js_modules/SettingsManager-635b89d6.js.js.bin) open failed, errno 2.
ReferenceError: could not load module filename 'SettingsManager-635b89d6.js'
ReferenceError: could not load module filename 'about.js'
console.error: cannot read property 'prototype' of undefined
    at __loadPage (jsfm-nvue.js:5000)  at loadPage (jsfm-nvue.js:4976)
```

**根因**：状态盘里装的 设置 是 **2.1.6** —— 既不是本机固件预设的 **2.1.1**
（`/etc/miniapp/resources/presetpkgs/8080272425914438.amr`，由 `packs/s6pro/pack.toml` 的
`[adapt.replace]` 从 `patches/presetpkgs/` 覆盖进去，两边 md5 同为 `bda49fa6704685d302a4ac9aef671284`），
也不是宿主 `amrs/` 里那份 **2.7.3**（`meta.otaVersion=4.7.0`，本机 OS 是 4.0.5）。2.1.6 的
`SettingsManager-635b89d6.js.bin`（172 B 字节码）`require('settings')`，而这套包体里只有
`services/settings.js.bin`（模块名是 `services/settings`）、native 侧只有 `libs/arm-pear/`
（Pear＝a6pro 的板名），于是整条链断在第一个 require 上 → 页面没渲染 → 黑。
**同款失败在 10-06 的日志里就有**，不是这一轮改出来的。

**修法与复验**：就地重装本机那一份，不动 `amrs/` 里更新的 2.7.3（版本新不等于配这台）——

```bat
python scripts\dpctl.py sh -c "miniapp_cli install /etc/miniapp/resources/presetpkgs/8080272425914438.amr"
```

装完注册表回到 `version=2.1.1`，再走 下拉菜单 → 齿轮 → 翻到底 → 关于：**页面正常**
（型号 `YDPS6-1 CHN PRO`、版本 `词典笔OS 4.0.5`，`vm/_s6_about_fixed.png`），日志零
`could not load module`。动手前把旧包整目录备份到 `/userdata/_qwen_bak_s6_setting`（4.6 MB，要退回去就
`cp -a` 回 `pkg/8080272425914438`）。

**顺带澄清一件事：桌面上没有「设置」这张卡，不是缺文件。** 首页只有 3 张卡（查词翻译／语音助手／
有道随身听），设置的注册项 `props.addDesktop` 是
`{"almond": true, "apollo": false, "coco": true, "x3s": true}` —— **没有 `plum_platform` 这个键**，
而 HOME（查词翻译）的字节码里只认 6 个板名（almond/apollo/coco/melon/plum/x3s，`pear` 等都不在表里）。
本机自报 `get_pcba_version` = **Plum_V0**，所以按板型开关它就不该上桌面；真入口是**下拉菜单那颗齿轮**
（实测可用：`vm/_s6_top.png` = 下拉面板与那颗齿轮，`vm/_s6_setting.png` = 设置首屏）。
2.7.3 的 `addDesktop` 同样没有 plum —— 厂商就没给这块板开。

**还没查完的（别当成已修）**：把"盘上版本"与"本机预设版本"逐条对了一遍
（`vm/_qwen_s6_preset_audit.sh`，客机里跑；`dpctl put` 时用 `//tmp/…`，Git Bash 会把 `/tmp/` 换成
`C:/Users/…/Temp/`），24 条里 **10 条与预设不一致**（播放器 2.0.1/2.0.0、背单词 2.1.2/2.1.1、
应用商店、parentcontrol-service、单词本、英语同步学、跟读、下拉菜单、有道随身听、个人中心）、
1 条预设没装（`8080212483527402` 2.0.1）、1 条装了但无预设（学习计划）。
**"不一致"不等于坏** —— 比预设新可能是 OTA/商店来的正常升级；但这正是同一条失败链的候选面，
逐个点开验证是下一步。而 Q70/Q71 的「应用体检」只看"文件在不在盘上"，这一轮它报的是"24 条坏 0 条"，
**口径缺的就是"是不是本机那一份"**（已作为提案记台账 Q75）。

## 13. 实体键（2026-10-20）

- **能力**：Home，码 **139**（`hal_key_config.json` 的 `Cherry_Cvitek_1826_V0.home_key_id =
  platform-gpio-keys-event`）；本包固件没有音量键项。
- **做法**：`patches/vm-keys.pl` + `vm-keys-apply` + `vm-keys.env` + `vm-keys.sh` 钩子。
  板型串 `Cvitek Cv1826 for Youdao Cherry V0 version`；`runDictPen.sh` 在 miniapp 之前调用
  `vm-keys-apply`（显示初始化已完成，不能再提前到 S00/S21）。
- **验证**：冷启动后查词翻译 → 桌面（`vm/_s6_new_after.png`）；控制台工具条只亮 Home。
- 快速启动可选：`dpctl fast save --pack s6pro`（无音频，见 README 与台账 Q93）。

## 14. 设置存储跟随 VM 状态盘（Q105，2026-10-21）

**症状**：空间清理页显示写死的 **4.0GB/4GB**（状态盘已 8GB），扩盘不跟随。设置里
「空间清理」「关于→存储容量」两条入口都会遇到。

**根因三层**：
1. 厂商 `getStorageInfo` 返回固定账本（totalSpace=4GB、user/available=0），盘扩了分母不动。
2. 本机 app 是 **ELF32 armhf + glibc 2.23**（s7pro 是 2.33）。x6pro 的 AArch64 shim 不适用；
   armhf 的 `getStorageInfo`（public/private 两个 mangled 名）同样是 **r0=sret、r1=this**，
   24 字节 `std::string`。
3. **NMT 连锁**：一旦覆盖存储符号，app 启动时 `loadNmtPatch` 在
   `/userdisk/uresource/localdict` 缺失的情况下抛 `MiniNmtException` 并 abort（s6 单独跑
   不 abort，是存储 shim 影响了这条链）。shim 里必须同时把
   `_ZN28YAsyncLocalTranslatorPrivate12loadNmtPatchEv` 做成 no-op。

**做法**：`patches/vmstorage.c` 用 Zig 编成 `libvmstorage-armhf.so`（对 s6 的 glibc 2.23
动态链接），返回 `statvfs("/userdata")` 的 total/used/free，并带 NMT no-op。
`miniapp-wrapper.sh` 用 `LD_PRELOAD=/usr/lib/vmstorage/libvmstorage-armhf.so` 起
`/usr/bin/miniapp.real`；`runDictPen.sh` 在首 app 前装包装器。

**两个安装坑**：
- **不能放 `/usr/lib` 顶层**：strace 实测这台 app 的加载器在找缺失库时会把
  `/usr/lib/libvmstorage-armhf.so` 当候选（例如 `libresolv.so.2`）打开；放子目录
  `/usr/lib/vmstorage/` 后干净。
- `[adapt.replace]` 只覆盖已存在目标，新增文件装不上；`fixup.sh` 第 10 节直接拷进
  `$TARGET`。

**设置版本必须钉在 2.1.1**：2.1.6 的 `SettingsManager-635b89d6.js.bin` 只
`require('settings')`，而本机包体只有 `services/settings.js.bin` → 存储/关于页
`could not load module` 全黑（Q75 修过一次，联网后 app 自动升级又升回 2.1.6：
log 里 `oldVersion=2.1.1 newVersion=2.1.6`）。本机固件预设是 2.1.1，只有它与 OS 4.0.5
的框架模块对得上。`fixup.sh` 第 11 节把 `hardware-iot.youdao.com`（应用商店/升级接口）
指到 127.0.0.1；**已有状态盘若已经升到 2.1.6，需要手动降一次**：

```bat
python scripts\dpctl.py sh -c "miniapp_cli install /etc/miniapp/resources/presetpkgs/8080272425914438.amr"
```

**验证**（2026-10-21，8GB 状态盘、设置 2.1.1）：
- `/proc/<miniapp.real>/maps` 命中 `/usr/lib/vmstorage/libvmstorage-armhf.so`；
  `df /userdata` = 7.8G/452.8M 用。
- 设置 → 空间清理：**已使用 0.5GB/8GB**（`vm/_s6_storage_shim.png`）。
- 设置 → 关于 → 存储容量：**8GB**，点进去同样 **0.5GB/8GB**
  （`vm/_s6_about_shim2.png`、`vm/_s6_about_storage_shim.png`）。
- 从卡片内部上拖才滚得动设置列表（从两列之间的空隙拖会回桌面）；
  下拉菜单 → 齿轮 才是设置入口（桌面无设置卡片）。
- 单测：`vmgui.tests.test_units.TestS6StorageShim`。

## 声音（2026-10-22）

`[audio] cards = 2`：厂商播放链路按 card 序号打开设备，只有 `card0` 时
SoundPlayer 报 ALSA `Invalid value for card`。补第二块 `usb-audio` 后
`dpctl audio-check` 通过（端点峰值约 34.5%~34.9%）。SoundPlayer 由厂商 init
自启，本包不需要额外脚本。随身听的 USB 上传目录是 `/userdisk/Music/`（大写 M）。
