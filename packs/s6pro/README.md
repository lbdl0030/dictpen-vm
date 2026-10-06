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
