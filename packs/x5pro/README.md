# 固件包：x5pro

X5pro（板型 **Cherry_Cvitek_1826_V1_5G**，Cvitek **CV1826** 平台，rootfs 只读
**ext4**，用户态 32 位 armhf）的固件包。与 s6pro/s7pro 同族，boot 镜像是 FIT
（cvitek 内核 + 4 块板级 DTB），仅作参数参考。

**现状（2026-10-06）：除触摸 UI 响应外全部就绪。** 引导、桌面 UI 上屏
（800×254 三卡片）、SSH、静音、磁盘治理、假电量、激活、热点均验证通过；
触摸事件已逐层送达 app（见 §6），但 3.4.6 版 app 的 UI 对其无响应 ——
完整取证留在本文，待逆向 app 输入派发后收尾。

- [1. 文件来源](#1-文件来源)
- [2. 能力现状](#2-能力现状)
- [3. 与 s6pro/s7pro 的差异与对策](#3-与-s6pros7pro-的差异与对策)
- [4. OCR abort：与 s7pro 同源同修](#4-ocr-abort与-s7pro-同源同修)
- [5. 显示：横屏 800×254（s6pro 配方）](#5-显示横屏-800254s6pro-配方)
- [6. 触摸：事件全通、UI 无响应（未解，取证完整）](#6-触摸事件全通ui-无响应未解取证完整)
- [7. 其他适配](#7-其他适配)
- [8. 下一步](#8-下一步)

## 1. 文件来源

| 文件 | 说明 | MD5 |
|------|------|-----|
| `images/x5-1-pro-ro-rootfs_b.img` | 只读根（ext4，1.5 GiB） | `d4b8420fff99e8d9daa00d8956c6036b` |
| `images/x5-1-pro-ro-boot_b.img` | FIT（仅参考） | `90786ebdfb327d41094fb252e506d575` |

镜像版权归网易有道，仅供个人学习研究。

## 2. 能力现状

| 能力 | 状态 |
|------|------|
| 引导（ext4 root + overlay + 状态盘） | ✅（state 2 GiB；老盘 512M 重建后生效） |
| UI 上屏 | ✅ 800×254 桌面三卡片完整正立（`vm/x5-home.png` / `vm/x5-v3-home.png`） |
| OCR | ⛔ 桩化（同 s7pro，无 CVITEK TPU；未桩化时 miniapp assert 秒崩重启循环） |
| SSH（arm64 dropbear，:22） | ✅ 开机自启（`S00dropbear: up`），`dpctl sh/put/get` 全走 SSH |
| 网络 eth0 | ✅ 原生可用（S40network/S41dhcpcd 在 VM 内核正常，**不需要** s7pro 的 S00net-up） |
| 诊断服务静默 + applog 上限 | ✅ S50launcher 的 run_uevent_monitor/runCapFrame 等（串口刷屏与磁盘写爆源头） |
| 电池 / 激活 / 热点 | ✅ 框架钩子 |
| 触摸 UI 响应 | ⛔ **未通**（事件已逐层送达 app，见 §6；对 UI 零效果，原因待逆向） |
| TCP adb | ⛔ 这族 adbd 是 android-tools 4.2.2，只走 USB gadget（s6pro 同款结论）；S00adbd 保留为兜底 |

## 3. 与 s6pro/s7pro 的差异与对策

| 差异 | 对策 |
|------|------|
| 板型 Cherry V1 5G | `[adapt.replace]` 的 `get_pcba_version.sh` 报 `Cherry_Cvitek_1826_V1_5G` |
| update_engine 会在 VM 里重启机器 | `[adapt.replace]` 换成 `noop_update_engine.sh` 空操作 |
| OCR 依赖链（同 s7pro） | 桩化 `libyocr.so`/`libYoudaoStitch.so` 整体替换（§4） |
| rootfs 无 dropbear、无 perl | arm64-tree + perl-tree（s7pro 同款素材） |
| 网络 | **原生正常**（与 s7pro 不同，勿加 S00net-up） |
| app 输入语义 | 读 `/dev/input/mouse0`（mousedev 增量）+ cfg tp 的 evdev 节点（§6） |

## 4. OCR abort：与 s7pro 同源同修

**现象**（`DictPen_*.log`）：`[OCR INFO] ydstitch_init` 之后

```
miniapp: ../../src/soc/common/cvi_device_mem.cpp:528:
virtual bmerr_t CviDeviceMem::device_init(int, bm_context**): Assertion `0' failed.
Aborted
```

miniapp 秒崩，guardian 无限重启（app 版本 3.4.6）。根因与 s7pro 相同：
libyocr 初始化链要 CVITEK TPU（/dev/ion、/dev/cvi-tpu0），VM 没有。

**修复**：`patches/libyocr.so` 与 `patches/libYoudaoStitch.so` = 真库的全导出
桩化版（init/open/create 类返回 -1，其余 0；`DT_INIT` 改惰性标签、
`DT_INIT_ARRAYSZ` 清零，构造器不跑），经 `[adapt.replace]` 整体替换。
方法、坑（ELF32 符号表 16 字节步进）与验证详见 s7pro README §4。
**DT_NEEDED / presetpkgs 均不需要改动**（依赖链自然解析到桩上）。

验证：重启后日志 0 abort，miniapp 常驻，桌面渲染完整。

## 5. 显示：横屏 800×254（s6pro 配方）

厂商 cfg.json 是 `width=254/height=800/direction=270`（面板竖扫 + 显示引擎旋转）。
这台 app 的 UI 版式是**横屏 800×254**：

- 竖屏 254×800 直接给：app 不崩（vinfo 协商成功）但 UI 以 800 宽版式写进
  254 宽缓冲 —— 斜切+裁剪（`vm/x5-first.png`）；
- 正确姿势（s6pro README §5 同一配方）：`[display]` 800×254（EDID）+
  cfg_overlay `width=800/height=254/direction=0` —— 虚拟屏 = fb = UI 版式，
  1:1 写入，完整正立（`vm/x5-home.png`）。

## 6. 触摸：事件全通、UI 无响应（未解，取证完整）

这台 app（3.4.6）的输入读取（strace 实证）：

- **fd6 = /dev/input/mouse0**（mousedev 字节流：buttons+dx+dy）——**UI 光标来源**；
- **fd7 = cfg tp 的 evdev 节点**（`by-path/axs_ts`）——打开并读取，但 UI 不响应；
- **没有 tslib**（maps 无 input.so），也**不认裸 tablet 的 BTN_LEFT**
  （s6pro 式"直接吃 BTN_LEFT"在 3.4.6 上不成立，实测无反应）。

### 6.1 已逐层验证为"通"的部分

| 层 | 证据 |
|----|------|
| QMP → QEMU 输入核心 | `-trace input*`：`input_event_abs/btn` 全部出现 |
| QEMU → guest 内核 | LED 探针：QMP key(CAPSLOCK) → `input0::capslock/brightness` 0→1；event1 抓包（dd 自终止版）：EV_ABS(16384)+BTN_LEFT 全部到位 |
| 桥（uinput） | touchbridge DEBUG 日志：完整 MT-B + 单点帧（SLOT/TRACKING_ID/POSITION_X/Y/BTN_TOUCH/SYN，坐标=面板像素） |
| app 读取 | strace：fd6 收到 mousedev 增量包、fd7 收到完整触摸帧 |

### 6.2 已确认/已修的坑

- **mousedev 缩放**：ABS→像素换算用模块参数 xres/yres，Debian 默认 1024×768，
  与 800×254 面板差 41 倍 —— 光标增量全部错位。参数**运行时可写**，
  `S00mouse-scale` 在 miniapp 之前写入面板尺寸（fixup §6b）。
- **QMP `abs`/`key` 事件在长会话后可能停止投递**（10-05 观察：全新会话 LED 探针
  通过，跑了近一小时+大量 QMP 连接后 event1 只剩 BTN_LEFT；换包重启即恢复）。
  长会话调试触摸时先重启 VM。
- **vendor busybox 没有 `timeout` applet**：`timeout N cat ...` 的抓包全是假象
  （rc=127，空文件是 shell 重定向建的）。抓包用 `dd bs=16 count=N`（自终止）。
- app 光标初值疑似屏幕中心（负向增量包 + 网格扫描综合推断），相对增量式点击
  天然带 +center 偏移。

### 6.3 已排除的假设

| 假设 | 实验 | 结果 |
|------|------|------|
| app 吃裸 tablet BTN_LEFT（s6pro 式） | tp 直链 event1，app 重启后 tap | 无反应 |
| app 需要单点 ST 帧 | 桥发 BTN_TOUCH+ABS_X/Y（tslib 式） | 无反应 |
| app 需要 MT 协议 B | 桥追加 SLOT/TRACKING_ID/POSITION_X/Y 帧 | 无反应 |
| 触摸能力被 HAL 门控（touch-valid 类） | 无任何相关日志；无 EVIOCGBIT 能力查询（strace 启动全程） | 未发现门控点 |
| run_input_event_monitor 是输入中继 | 恢复运行后再测 | 无差别 |
| mousedev 缩放 | xres/yres=800/254 + app 重启 | 无差别 |

### 6.4 下一步（最有价值的线索）

- **libfalcon.so 里硬编码 `/dev/input/event0`**（strings 实证）——真机上 I2C 触摸
  最先枚举 = event0；VM 里 event0 = virtio 键盘。已试"mknod 别名 event0→桥设备"
  但 app 的 fd 未变（它启动时已打开 mouse0/event2，alias 后未重启到位——
  **该实验要做干净：alias 必须在 app 启动前就位**，本轮最后一次实验时序未满足）。
- 逆向 3.4.6 app 的输入派发：定位读 mouse0/event2 的线程属于哪个 .so，找
  `dispatchTouchEvent` 调用链与事件过滤条件（libfalcon 的
  `FBNativeWindow::dispatchTouchEvent action=%d, x=%f, y=%f` 是现成锚点）。
- 对照 s6pro（同族、触摸正常）：s6pro 的 app 读 event2（桥）+ mouse0 双流即响应，
  与 x5pro 布局完全相同 —— 差异只在 app 版本，**把 s6pro 的 libfalcon /
  libyddal_business 的输入相关版本号与 x5pro 比对**，可能是同一只库的新旧版本行为差异。

## 7. 其他适配

- **update_engine → noop**：VM 里没有 A/B 升级引擎，原版会动 misc/reboot。
- **串口 shell**：vendor init 不起 respawn（同族通病），S51console-loop 兜底；
  另有 busybox64 `nc -ll -p 22` 裸 shell 兜底（**与 dropbear 端口冲突**，
  2026-10-06 起 fixup 会删掉旧 S52telnetd，统一走 arm64 dropbear）。
- **OCR 模型恢复**：早期实验挪走过 `ocr/*.cvimodel.encrypt`（`hidden_cvimodel.bak`），
  fixup 幂等恢复；桩化后模型已无关紧要，保留恢复逻辑仅为兼容旧状态盘。
- **cp -rf 坑 / ln -sfn 空目录坑 / busybox 无 timeout**：同 s7pro，见 s7pro README
  §9 与本文 §6.2。

## 8. 下一步

1. **触摸收尾**（§6.4 两条线索，优先 falcon 的 event0 别名实验做干净）。
2. 状态盘当前 77% 占用：`[state] size_mb=2048` 已声明，旧盘跑一段时间后
   `up --fresh` 重建即可。
3. 验证激活/账号链路（license 已写开发凭证，账号云功能依赖网络，未深测）。
