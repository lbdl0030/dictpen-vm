# 固件包：x5pro

X5pro（板型 **Cherry_Cvitek_1826_V1_5G**，Cvitek **CV1826** 平台，rootfs 只读
**ext4**，用户态 32 位 armhf）的固件包。与 s6pro/s7pro 同族，boot 镜像是 FIT
（cvitek 内核 + 4 块板级 DTB），仅作参数参考。

**现状（2026-10-16）：触摸已通，点卡片崩溃已修。** 引导、桌面 UI 上屏
（800×254 三卡片）、SSH、静音、磁盘治理、假电量、激活、热点均验证通过；
触摸事件已逐层送达 app 并正确派发（见 §6.7）。点中卡片后 miniapp SEGV 的
问题**与摄像头/麦克风无关**，真因是本包的 OCR 桩库把 libstdc++ 的弱定义符号
一起抹成了 `return 0`（见 §10，已修）。

- [1. 文件来源](#1-文件来源)
- [2. 能力现状](#2-能力现状)
- [3. 与 s6pro/s7pro 的差异与对策](#3-与-s6pros7pro-的差异与对策)
- [4. OCR abort：与 s7pro 同源同修](#4-ocr-abort与-s7pro-同源同修)
- [5. 显示：横屏 800×254（s6pro 配方）](#5-显示横屏-800254s6pro-配方)
- [6. 触摸：事件全通、UI 无响应（未解，取证完整）](#6-触摸事件全通ui-无响应未解取证完整)
- [7. 其他适配](#7-其他适配)
- [8. 下一步](#8-下一步)
- [9. runDictPen.sh 的 `LD_LIBRARY_PATH` 漏了两段（2026-10-09 修复）](#9-rundictpensh-的-ld_library_path-漏了两段2026-10-09-修复)
- [10. 点卡片崩溃的真因：桩库把 libstdc++ 的弱定义也抹了（2026-10-16 修复）](#10-点卡片崩溃的真因桩库把-libstdc-的弱定义也抹了2026-10-16-修复)

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
| 触摸 UI 响应 | ✅ 已通（事件逐层送达 app，坐标正确派发，见 §6.7） |
| 点中卡片后崩溃 | ✅ **已修**（2026-10-16，真因是桩库抹掉 libstdc++ 弱定义，见 §10） |
| TCP adb | ⛔ 这族 adbd 是 android-tools 4.2.2，只走 USB gadget（s6pro 同款结论）；S00adbd 保留为兜底 |
| 设置存储 | ✅ 跟随 VM 状态盘（shim；空间清理与关于→存储容量都显示 0.4GB/1GB，见 §12） |

## 3. 与 s6pro/s7pro 的差异与对策

| 差异 | 对策 |
|------|------|
| 板型 Cherry V1 5G | `[adapt.replace]` 的 `get_pcba_version.sh` 报 `Cherry_Cvitek_1826_V1_5G` |
| update_engine 会在 VM 里重启机器 | `[adapt.replace]` 换成 `noop_update_engine.sh` 空操作 |
| OCR 依赖链（同 s7pro） | 桩化 `libyocr.so`/`libYoudaoStitch.so` 整体替换（§4），桩化时必须回填 libstdc++ 弱定义（§10） |
| rootfs 无 dropbear、无 perl | arm64-tree + perl-tree（s7pro 同款素材） |
| 网络 | **原生正常**（与 s7pro 不同，勿加 S00net-up） |
| app 输入语义 | 读 `/dev/input/mouse0`（mousedev 增量）+ cfg tp 的 evdev 节点（§6） |
| **runDictPen.sh 漏了 `LD_LIBRARY_PATH` 两段** | 已补上 `/etc/miniapp/jsapis` 和 `/etc/miniapp/messageknife`（§9） |

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

> ⚠ 这套「全导出桩化」把 libstdc++ 的弱定义模板实例也一起抹成了 `return 0`，
> 而这两份库正是全进程 `unordered_map<string,string>::operator[]` 的提供者
> —— 后果是点首页卡片必崩。修法与取证见 §10（`patches/fix-stub-cxxrt.py`）。

## 5. 显示：横屏 800×254（s6pro 配方）

厂商 cfg.json 是 `width=254/height=800/direction=270`（面板竖扫 + 显示引擎旋转）。
这台 app 的 UI 版式是**横屏 800×254**：

- 竖屏 254×800 直接给：app 不崩（vinfo 协商成功）但 UI 以 800 宽版式写进
  254 宽缓冲 —— 斜切+裁剪（`vm/x5-first.png`）；
- 正确姿势（s6pro README §5 同一配方）：`[display]` 800×254（EDID）+
  cfg_overlay `width=800/height=254/direction=0` —— 虚拟屏 = fb = UI 版式，
  1:1 写入，完整正立（`vm/x5-home.png`）。

## 6. 触摸：已通（根因与修法见 §6.7；§6.1–§6.6 是排查过程记录）

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

1. 逐屏走查（§10 修复后卡片可进）：查词翻译 / 有道随身听这类要摄像头、麦克风的
   功能仍未验证，缺硬件时是否优雅降级未知。**先注意**：要在这台上验证「联网类」
   功能，别去调路由/DNS——这类固件的 `global.isOnline()` 是厂商 wifi 服务
   （`wifi-manager` → D-Bus → JS）**推**给应用的状态，eth0 通着也可能一直显示
   「此功能需要联网使用」。x6pro 上这条链的完整取证与解法见
   `packs/x6pro/DEVLOG.md` §5.7（根因是缺 `/dev/bes2600` 芯片节点）。
2. 状态盘当前 77% 占用：`[state] size_mb=2048` 已声明，旧盘跑一段时间后
   `up --fresh` 重建即可。
3. 验证激活/账号链路（license 已写开发凭证，账号云功能依赖网络，未深测）。
4. 给同族的桩库做同样的体检：**x6pro 已做完，结论是不用修**（2026-10-16，见
   §10.5 与 x6pro DEVLOG §5.6(3)）；还剩 s7pro 的 `libyocr.so` /
   `libYoudaoStitch.so`——判据不是"桩库里弱定义没了"，而是"该机有没有别的 ELF
   UND 引用这些符号"，两步都要做（先 `stubaudit` 列被抹掉的运行期符号，再在
   guest 侧扫 app 树的 `.dynsym` UND 表取交集）。
5. 自研触摸桥（`patches/touchbridge.pl`）改坐标处理前先读 x6pro DEVLOG §5.8：
   **QEMU 的 input 核心不会重发没变化的绝对轴**，所以 `BTN_LEFT` 按下/抬起那一帧
   往往没有 ABS；桥若要求"本帧收到坐标才动手"，症状是"点能用（或点也不能用）但
   滑动全废"。坐标要当**状态**存。另外 `dpctl tap` 发的 `BTN_TOUCH(330)` 经
   virtio-tablet 到不了 guest，触摸语义只能由桥合成。取证用 `evtest <源节点>`
   （不加 `--grab`，可与桥并行读）。

### 6.5 触摸深挖续（2026-10-06/07，仍未闭环）

本轮把事件链逐层验证到"全部通畅"，但 app 的 UI 依旧不响应。证据链更新：

| 层 | 2026-10-06/07 实测 |
|----|------------------|
| QEMU 核心分发 | ftrace `input_event <-virtinput_recv_events`：tap 的 ABS+BTN+SYN **全部到达 guest 内核输入核心**（此前"abs 不投递"的结论是抓包脚本被 ssh 会话清掉的假象——vendor busybox 无 `timeout`，`(dd ... &)` 随 ssh 断开被杀） |
| 桥转发 | 旋转桥（`touchbridge-rot.pl`→`touchbridge.pl`）把 tablet 的横屏 ABS 旋转成竖屏物理坐标（MT_X=Uy、MT_Y=799-Ux），MT-B+单点双帧，写入成功无报错 |
| app 读取 | strace：app 的 ts 读取线程（fd6=event2）完整读到上述帧 |
| app 派发 | zlog 提级 DEBUG 后（`zlog_miniapp.conf` 的 `*.WARN`→`*.DEBUG`）：`FBNativeWindow::dispatchTouchEvent action=1, x=0.000000, y=0.000000` —— **tap 时序精确、坐标恒 (0,0)** |

**新确认的事实**：

- 这台 app（3.4.6）的 UI 触摸坐标来源**不是** fd6（event2，cfg tp）也不是 mouse0 光标
  ——两个流都正常送达，派发坐标恒 (0,0)；
- 其 libfalcon.so 有**硬编码 `/dev/input/event0`** 字符串（真机上 I2C 触摸最先枚举
  = event0；VM 里 event0 = virtio 键盘）——**alias 成 event0 的实验必须保证
  alias 先于 app 启动**（上轮时序没满足，未重做干净）；
- mouse0 光标初值疑似屏幕中心（x5cal 网格扫描推断），相对增量式点击带 +center
  偏移，但这不是唯一问题（任何位置都不导航）。

**本轮已固化的基建**（对未来任何修法都有用）：

- `touchbridge.pl` 升级为**旋转桥**：MT-B + 单点双帧、竖屏物理坐标旋转
  （MT_X=Uy+110、MT_Y=799-Ux）、**BTN_TOOL_FINGER** 标记、
  udev `seat` 标签直写（fork 子进程等 /run/udev 出现后写 c13:N 数据库条目，
  因为 S00 桥先于 S10 udevd 运行，规则来不及）；
- `S00mouse-scale`：mousedev 的 xres/yres 对齐面板（app 若走 mouse0 光标路径，
  增量单位正确）；
- uinput 接口改用 **legacy write(uinput_user_dev)** 路径（现代 UI_ABS_SETUP 的
  ioctl 组合在 perl 下 EINVAL，legacy 已在 s6pro/s7pro 验证）。

**下一步（按价值排序）**：

1. **干净重做 event0 别名实验**：桥启动后（S00）立即
   `rm /dev/input/event0 && mknod /dev/input/event0 c 13 N`（N=桥设备次设备号），
   再让 app 启动——验证 falcon 的硬编码 event0 是否就是它的触摸源；
2. 逆向 libfalcon 的触摸坐标变换：从 `dispatchTouchEvent` 的调用方往上找
   坐标来源（是 event0 的 EV_ABS 还是别的），确定期望的协议/量程；
3. 对照 s6pro（同族、fbdev、触摸正常）：diff 两台 app 的输入相关库版本，
   找 3.4.6 输入栈的架构差异。

### 6.6 最终状态（2026-10-07，本会话收尾）

所有外部手段已穷尽。事件链每一层都用工具验证通过（QEMU trace、内核 ftrace
`input_event <-virtinput_recv_events`、桥的 uinput 写入、app 的 strace 读取），
但 app 的 `dispatchTouchEvent` 坐标恒 (0,0)——包括在 event0 别名生效、MT-B 协议、
旋转坐标、mousedev 缩放对齐等所有组合下。两个标定点的坐标变换拟合互斥，
3.4.6 app 的坐标源是内部状态，不随外部事件流更新。

**已验证不读的设备**：/dev/input/event0（别名后 fd 表不变）。
**app 的输入 fd**：mouse0（mousedev 字节流，增量恒 0）+ event2（cfg tp，
本桥触摸屏，帧完整）。

**触摸挂起**。要恢复触摸需要逆向 3.4.6 app 的输入派发（libfalcon 的
`dispatchTouchEvent` 调用链），或对照同族可工作版本 diff 输入库。
本轮基建（旋转桥、udev 标签、mousedev 缩放、DEBUG 日志）全部保留，
复用时直接可用。

### 6.7 触摸已通（2026-10-09，本会话解决）

反汇编 libfalcon（`greenui::winInputLoop` @ `0x1ca7a8`）后，前几轮的困惑全部
收敛到两个真实原因，§6.6 的"坐标源是内部状态"结论**被推翻**——

#### 根因一：cfg 里 `tp_direction` 写错了（180 → 应为 0）

`winInputLoop` 同时 poll **两个设备**，各自 feed 同一个 `(s17, s16)`：

| 链 | 设备 | cfg 键 | 解析 | 坐标变换 |
|----|------|--------|------|----------|
| TP | fd = cfg `device.tp` | `tp` | 16B `input_event`：`EV_ABS` code 0/0x35→X、1/0x36→Y；`EV_KEY` 330→落/抬；`EV_SYN`→派发 | 按 `tp_direction` 分支 |
| 鼠标 | fd = cfg `device.mouse` | `mouse` | mousedev 3 字节（buttons/dx/dy，bit4/5 是符号位） | **增量累加**，不做方向变换 |

`tp_direction` 各分支（W/H = 窗口宽高，VM 里是 800/254，不是 0）：

```
dir=0   : x = ABS_X - tp_xoffset            y = ABS_Y - tp_yoffset      <- 恒等
dir=90  : x = H-1-y'                        y = W-1-x'
dir=180 : x = min(W-1, max(0, W-1-x'))      y = min(H-1, max(0, H-1-y'))  <- 180° 镜像
dir=270 : x = y'                            y = W-1-x'
```

实测（面板 800×254，桥上报 UI 像素）：`dir=0` 点 (100,60) → 派发 **(100,60)**；
`dir=180` 同一点 → **(699,193)** = (799−100, 253−60)。

§6.6 之所以判定"写 180 才恒等"，是把**鼠标链的假坐标**（见根因二）当成了 TP 的
结果：鼠标链的光标初值是 (0,0)，于是 TP 的正确值被假 DOWN 覆盖/混淆。

#### 根因二：`device.mouse` 那条链会注入一条假 DOWN(0,0)

mouse 链是增量累加的。app 同时读 mouse0 与 TP 设备，每次点击会派发**两条 DOWN**：
TP 链的（坐标正确）+ mouse 链的（在光标位置，通常是 (0,0)）。那条 (0,0) 落在屏幕
左缘，触发 `LeftSwipeBackGesture` / `TopSwipeGesture`，随后 SEGV：

```
dispatchTouchEvent action=1, x=0,   y=3      <- mouse 链的假 DOWN
LeftSwipeBackGesture Start
dispatchTouchEvent action=1, x=689, y=124    <- TP 链的正确值
LeftSwipeBackGesture onHandle
Segmentation fault
```

把 `device.mouse` 置空后，只剩 TP 一条链，派发变成干净的
`action=1 → 3 → 2` 且坐标正确。

#### 落地的改动

- `patches/cfg.json.overrides`：`tp_direction=0`（`tp_xoffset/tp_yoffset=0` 不变）；
- `patches/fixup.sh` 的 `S00cfg-tp` 钩子：`tp_direction=0` + `device.mouse=""`
  （框架的覆盖表只认"数字 / 以 `/` 开头的字符串 / `true|false`"三种值，**表达不了
  空字符串**，所以这一项只能在 guest 阶段由包内钩子写；该钩子同时解决 overlay
  upper 遮蔽问题——见 §6.2 与 fixup 里的注释）；
- `patches/touchbridge.pl`：与 s6pro 同源，另加一组 **MT-B 帧**
  （`ABS_MT_SLOT/TRACKING_ID/POSITION_X/Y` + `BTN_TOOL_FINGER`），单点帧保留；
  所有轴量程 = 面板像素（800×254），上报 **UI 坐标**，不做旋转。

#### 验证（2026-10-09，重启 VM 后）

```
$ dpctl tap 205 130        # 卡片之间的空白
dispatchTouchEvent action=1, x=205, y=129
dispatchTouchEvent action=3, x=205, y=129
dispatchTouchEvent action=2, x=205, y=129
```
连点两次坐标稳定，app 不崩；点 (100,60) → (100,60)、(599,130) → (599,129)。

**踩过的坑**（复现时注意）：

- **桥重启后必须重启 app**：桥死掉会销毁它创建的 uinput 设备，而 app 的 fd 仍指向
  那个已销毁的节点，读不到事件 → 派发恒 (0,0)。看起来像"坐标又坏了"，其实是
  fd 失效。验证脚本一律"先起桥、再重启 app"。
- **内核会过滤重复的 ABS**：同一坐标连点，只有变化的轴产生事件（实测第二次点击
  只到 `ABS_X`）。桥靠缓存值不受影响，但**抓包只看一次会以为 ABS_Y 丢了**。
- **`dbuffer` 不能关**：把 cfg 的 `dbuffer` 改成 false 会让这台 app 彻底黑屏
  （它走双缓冲才上屏），已验证后恢复 true。
- vendor busybox 无 `timeout`、PowerShell 会吃 `$()` 与管道——脚本一律写文件再执行。

#### 遗留：点中卡片后 app 崩溃（与触摸无关）—— 2026-10-16 已修，见 §10

点空白不崩；点中卡片时 **同一个触摸线程在 UP/MOVE 之后约 80ms SEGV**
（`BottomSwipeGesture ACTION_DOWN` 在两个场景里都打，所以不是手势本身）：

```
dispatchTouchEvent action=1, x=599, y=129
BottomSwipeGesture ACTION_DOWN 129.000000, 20.000000, 40.000000
dispatchTouchEvent action=3, x=599, y=129
dispatchTouchEvent action=2, x=599, y=129
Segmentation fault            <- 80ms 后，UI 已经开始执行卡片的动作
```

即 **UI 已经把点击识别成 click 并去打开卡片对应的功能**（查词翻译 / 有道随身听），
崩在功能内部。**本节当时归因为"缺摄像头/麦克风"，是错的**：崩溃点是埋点
（log-spy）链路上的 `std::string::assign(this=0)`，与硬件无关，
真因与修法见 §10。app 静置 300 秒可以完全不崩（不发埋点）。

## 9. runDictPen.sh 的 `LD_LIBRARY_PATH` 漏了两段（2026-10-09 修复）

**症状**：app 启动后卡住不动，或者反复重启。

**根因**：本包 `patches/runDictPen.sh` 替代厂商 `/usr/bin/runDictPen` 时，把
`LD_LIBRARY_PATH` 写成了

```sh
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH
```

而厂商原版是

```sh
export LD_LIBRARY_PATH=$APP_PATH/libs:$LD_LIBRARY_PATH:/etc/miniapp/jsapis:/etc/miniapp/messageknife
```

少了后两段。`/etc/miniapp/jsapis/libjsapi_export_core.so` 的 `DT_NEEDED` 里有
`libgetdevicename.so`，而它只存在于 `/etc/miniapp/messageknife/`。uClibc 找不到它，
`dlopen` 失败（`dlerror()` 返回的字面量就是 `File not found`），于是：

```
ERROR:File not found:dlopen in dynamic_load_jsapi   <- 格式串是 ERROR:%s:dlopen in dynamic_load_jsapi
loadQJSModule(global) not found
ReferenceError: could not load module filename 'global'
  -> globalManager-*.js / dictNavgationbarManger-*.js / app.js 接连失败
  -> eval $falcon.launchApp fail / TypeError: not a function
  -> 框架退回启动「验证引导」(8080282888534774)，该应用在无激活状态下不画东西
  -> 全黑
```

`dynamic_load_jsapi` 的逻辑在 `/usr/lib/libfalcon.so`（`JQuick::dynamic_load_jsapi`
@0x4f7dc8；它先读 `cfg.json` 的 `debugger` 段，dlopen 前后各有 trace）。

**对照验证**：a6pro 的核心 jsapi **不依赖** `libgetdevicename.so`，所以它的日志里
`loadQJSModule` / `could not load module` / `launchApp fail` 都是 0 次，第二个启动的
app 直接是 HOME（8080222437664451）。a7pro 修好后与之一致。

**修复**：把那两段路径补回去（现在就是上面那一行，别删）。判断是否生效：

```bat
python scripts\dpctl.py screenshot --out vm\x.png      :: 400 字节纯黑 = 没好；~100 KB = 桌面
python scripts\dpctl.py sh -c "grep -c 'could not load module' /userdata/applog/DictPen_*.log"
python scripts\dpctl.py sh -c "grep -c 'launchApp fail'      /userdata/applog/DictPen_*.log"
```

**教训**：替代厂商启动脚本时，"与厂商原版一致"的那部分要逐字核对，尤其是环境变量；
改这一行必须重新验证 UI，不能只看进程起来了。

> ⚠ `packs/a7pro/patches/runDictPen.sh` 有**同样**的漏项（同一份脚本抄的），
> 已在 a7pro 上验证并修复（见 a7pro README「黑屏的真正原因」）。

## 10. 点卡片崩溃的真因：桩库把 libstdc++ 的弱定义也抹了（2026-10-16 修复）

**症状**：桌面点中任意卡片 → `Navigator navToApp(...)` → 约 80 ms 后 miniapp
SEGV（§6.7 遗留问题）。当时归因"缺摄像头/麦克风"，**是错的**：崩溃点在埋点
（log-spy）链路上，与任何外设无关，而且**是本包自己造出来的**。

### 10.1 取证（guest gdb + host ELF 解析）

抓法：`gdb -batch -q -x cmd -p $(pidof miniapp)`，断点打在 packLog 里
`bl <operator[]@plt>` 的下一条指令上，条件 `if $r0 == 0`（base 从
`/proc/<pid>/maps` 里 libyddal_base.so 的首段地址取，偏移 0x1028cc）：

```
=== NULL-RETURN ===
GOT 0xf38e9570 -> callee=0xf3c1a338
0xf3c1a338 <_ZNSt8__detail9_Map_baseI...Lb1ELb0ELb1EEELb1EEixERS8_>:  mov r0, #0
0xf3c1a33c:                                                           bx lr
#0 YDALBASEAPI::YLogSpyEntity::packLog(unordered_map<string,string>&, const ...)   libyddal_base.so
#1 YDALBASEAPI::YLogSpy::spyMultiple(const string&, unordered_map<string,string>&&) libyddal_base.so
#2 YDALAPI::Internal::YLogManagerModulePrivate::logSpy(const string&, const string&) libyddal_business.so
#3 JSLogManagerProxy::logSpy(JQFunctionCallbackInfo&)                                libjsapi_export.so
```

链路：JS 埋点 → `logSpy` → `spyMultiple` → `packLog(dst,src)` 把 src 逐项
`dst[key] = value` 合进 dst。`dst[key]` 返回 **0**，紧接着
`std::string::_M_assign(this=0, ...)` → `ldr r7,[r8],#8`（r8=0）→ SEGV。

### 10.2 根因

`_Map_base<std::string, pair<const string,string>, ...>::operator[]` 是
**WEAK 模板实例**，libstdc++.so.6.0.22 **不导出**它（GCC6 起改成头文件内联），
所以每个用它的库都只留 UND 引用（实测 libyddal_base 3 处、libcapt 22 处、
libytts/libopencc/libcviruntime/libyddal_db/libfuzzy_search/libyddal_business
各 1~4 处），全进程靠**谁先被加载谁兜底**——`libyocr.so` 里带着一份真定义
（vaddr `0x271338`，336 字节，WEAK），而 libyocr 在符号搜索顺序里排在前面，
于是全应用的 `unordered_map<string,string>::operator[]` 都绑到它身上。

§4 的「全导出桩化」把 libyocr 的**每一个**函数体都换成了 `mov r0,#0; bx lr`
（包括这份替别人兜底的 libstdc++ 弱定义）→ 埋点一发就崩。
`packs/x5pro/patches/libyocr.so` 与原始库逐字节对比：大小相同、922 个代码块被改写，
其中 382 块属于 `std::*/__gnu_cxx::/operator new/delete/__cxa_/_Unwind_` 符号。

### 10.3 修法

`packs/x5pro/patches/fix-stub-cxxrt.py <原始.so> <桩.so> <输出.so>`：
只把 **.text 里属于 C++ 运行期符号**的字节从原始库回填，其余（OCR/stitch 业务
入口、`DT_INIT` 惰性化、`DT_INIT_ARRAYSZ=0`）一概不动 —— 桩"在但不工作"的语义
保持不变，`yocr_*` 仍返回 -1，不会再去碰 CVITEK TPU。

```
libyocr.so          回填 382 个运行期符号 / 171936 字节
libYoudaoStitch.so  回填 166 个 / 73676 字节
```

原始库怎么拿：guest 里 overlay 的 `lowerdir=/ro` 切根后是**空目录**，看不到原文件；
直接 `mount -o ro /dev/vda /mnt` 就是原始根（rootfs 是整盘 ext4，无分区表）。

### 10.4 验证（冷重启后）

```
md5sum /oem/YoudaoDictPen/output/libs/libyocr.so   = 7ada5c9d45c72a5dde1cc0cf2d301727
点首页卡片 → 「我的」→「我的下载」（"暂无下载内容"）两级导航正常渲染
grep -c Segmentation /userdata/applog/DictPen_*.log = 0     （navToApp 2 次，pid 不变）
埋点上报正常：hardware-iot.youdao.com/app/store/log/report → {"code":0,...,"msg":"OK"}
```

### 10.5 教训与同类风险

- **「全导出桩化」必须跳过 C++ 运行期符号**：业务库替 libstdc++ 兜底的弱定义
  （`_ZN?St*`、`_ZN?9__gnu_cxx*`、`_ZSt*`、new/delete、`__cxa_*`、`_Unwind_*`）
  不是"这个库的 API"，抹掉它们等于改写了**全进程**的 STL 行为。
- 同族用同一套桩化脚本生成的 `packs/s7pro/patches/libyocr.so`、
  `packs/*/patches/libYoudaoStitch.so` 有**同样的隐患**（是否发作取决于那台的
  app 版本走不走这条埋点）；复现/修复方法同上：从该机 rootfs 取原始库，跑
  `fix-stub-cxxrt.py`。
- **x6pro 已体检，结论是不用修**（2026-10-16，见 x6pro DEVLOG §5.6(3)）：那台的桩库
  同样抹掉了 823 个运行期弱定义（`libyocr.so` 517 / `libYoudaoStitch.so` 348，
  其中 849 个 rootfs 的 `libstdc++.so.6.0.29` 也不提供），但扫完该机
  `/mnt/oem`+`/etc/miniapp`+`/usr/lib`+`/usr/bin` 全部 1159 个 ELF 的 `.dynsym`
  UND 表，**命中 0** —— 没有别的对象依赖这些弱定义，隐患不成立。
  判定顺序记牢：先看「有没有别的库 UND 引用」，只有引用为 0 才算安全；
  别一看到"被抹了几百个符号"就动手回填。
- 取证坑：`/proc/<pid>/mem` 用 perl `seek` 读 0xf3xxxxxx 会因 32 位 off_t 溢出
  **静默失败**（读出全 0，看着像"GOT 未解析"）——读高地址一律用 gdb。
- gdb 断点必须写 `break *0xADDR`（少星号会当成函数名，报
  `Function "0x..." not defined`）；`commands` 里 `silent` + `detach` + `quit`
  是 batch 模式一次性抓现场的标准写法。

## 11. 实体键（2026-10-20）

- **能力**：Home，码 **139**（`Cherry_Cvitek_1826_V0.home_key_id =
  platform-gpio-keys-event`）；本包固件没有音量键项。
- **做法**：`patches/vm-keys.pl` + `vm-keys-apply` + `vm-keys.env` + `vm-keys.sh` 钩子；
  板型串 `Cvitek Cv1826 for Youdao Cherry V0 version`。本机首次 app 启动前换 model 会黑屏，
  因此由 `S50vm-keys` 等首 app 起来后 apply、停掉 runDictPen 守护链与 app、直接启动一个
  miniapp（避免守护链与兜底各起一个）。
- **验证**：冷启动后查词翻译 → 桌面（`vm/_x5_final_after.png`）；单实例；控制台只亮 Home。
- 快速启动可选：`dpctl fast save --pack x5pro`（无音频，见 README 与台账 Q93）。


## 12. 设置存储跟随 VM 状态盘（Q106，2026-10-21）

**症状**：设置 → 空间清理显示 **4.0GB/4GB**，而状态盘只有 1GB；扩盘不跟随。

**根因与 s6pro 同族**：厂商 `getStorageInfo` 返回固定账本；本机 app 是 **ELF32 armhf +
glibc 2.23**；一旦覆盖存储符号，`loadNmtPatch` 在 `/userdisk/uresource/localdict` 缺失时
抛异常 abort，所以 shim 必须同时带 NMT no-op（同 s6pro）。

**做法**：`patches/vmstorage.c` 编成 armhf shim，覆盖 public/private 两个
`getStorageInfo`（r0=sret、r1=this、24B string）并返回 `statvfs("/userdata")`；
`miniapp-wrapper.sh` 用 `LD_PRELOAD=/usr/lib/vmstorage/libvmstorage-armhf.so` 起
`/usr/bin/miniapp.real`；`runDictPen.sh` 首 app 前装包装器，`fixup.sh` 把新增文件直接
拷进 `$TARGET`（`S00replace` 只覆盖已存在目标）。

**验证**（状态盘 1GB）：`df /userdata` = 990.7M/398.4M 用。
- 设置 → 空间清理：**已使用 0.4GB/1GB**（`vm/_x5_storage_shim3.png`）。
- 设置 → 关于 → 存储容量：**1GB**，点进去同样 **0.4GB/1GB**
  （`vm/_x5_about_shim2.png`、`vm/_x5_about_storage_shim.png`）。
- 设置列表要从卡片内部上拖才滚得动（通用 `swipe.py` 即可，x5 走 BTN_TOUCH 桥）；
  点「空间清理」后要等 2 s 再截图。
- 这台设置包是 **1.5.2**（OS 3.4.6 自带），没有 s6pro 的 2.1.6 自动升级问题，本轮未动更新。
- 单测：`vmgui.tests.test_units.TestX5StorageShim`。
