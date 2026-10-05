# vmgui — QEMU 虚拟机客户端 GUI（设计）

> 状态：设计已定稿，待实现。技术栈 **Python 3.11+ / PySide6(Qt6)**，显示后端 **QMP 优先**，
> 定位为 **dictpen-vm 的默认显示前端**（替代 `-display gtk`）。

## 1. 目标与现状对齐

需求原文（分辨率自适应 / 配置化 / 差异适配 / 鼠标输入 / 可扩展架构）在本项目里的具体形态：

| 需求 | 在本项目里的实际含义 |
|---|---|
| 分辨率自适应 | guest 画面尺寸随固件/机型变化（如 560×170、960×266），窗口需自动跟随 |
| 配置化 | 机型相关参数（显示模式、旋转、裁剪、鼠标类型、触摸按键、快捷键）全部进配置 |
| 差异适配 | 显示协议（QMP / VNC）、输入（绝对 tablet / 相对 PS2）、旋转、裁剪、DPI |
| 鼠标与输入 | 笔屏 UI 需要 BTN_TOUCH 而非 BTN_LEFT；捕获/释放与快捷键提示 |
| 可扩展 | 新增机型 = 加一个 profile；新增协议 = 加一个 Backend 实现 |

现有可复用资产：

- `scripts/dpctl.py` 已实现 QMP 通信：`screendump`（PPM，**尺寸即 guest 当前 console 分辨率**）、
  `input-send-event`（绝对坐标 + `BTN_TOUCH`，见 `cmd_tap`/`cmd_pointer`）、`query-*`。
- QMP 端口 `4446`；已有 `dpctl view`（tkinter 简易视图，缩放/HiDPI 能力弱）与
  `dpctl pointer`（触摸反馈指针）。
- 当前客户端 = QEMU 自带 `-display gtk`。bochs-display，`xres=560,yres=170`。

**范围决策**：MVP 只做 QMP；VNC（原生 desktop-resize 事件）作为可选后端；SPICE / 音频 /
USB 重定向 / 剪贴板 列为**可扩展项**，不进 MVP。

## 2. 技术栈

**Python 3.11+ + PySide6(Qt6)**：

- 同栈复用：dpctl 已是 Python，QMP 逻辑可直接抽共享模块；
- Qt 原生支持 1:1 / fit / stretch / fullscreen / HiDPI / 平滑与最近邻缩放；
- `QSettings` 存取配置；无边框窗口、快捷键、剪贴板、拖放开箱即用；
- Windows（当前宿主）与 Linux/macOS 均可跑。

备选（未采用）：tkinter（零依赖但缩放/全屏/HiDPI 弱）、Electron+noVNC（VNC 场景强但重）、
Rust+egui（性能好但引入新工具链）。

## 3. 配置 schema（TOML）

原则：**核心代码中不出现任何机型/分辨率/设备名常量**，全部来自配置或运行时探测。

```toml
[connection]
kind = "qmp"                       # qmp | vnc
uri  = "tcp:127.0.0.1:4446"
timeout_ms = 3000
reconnect = { enabled = true, backoff_ms = 500, max_backoff_ms = 5000 }

[display]
mode        = "fit"                # 1:1 | fit | stretch | free | fullscreen
scale_algo  = "nearest"            # nearest | bilinear | lanczos
rotation    = 0                    # 0 | 90 | 180 | 270
aspect      = "keep"               # keep | ignore
dpi_hint    = 0                    # 0 = 自动（用屏幕 DPI）
frame_poll_ms = 30                 # 拉帧节流；QMP 无原生 resize 事件，靠轮询尺寸
background  = "#000000"
[display.crop]                     # 只显示画面的一部分（笔屏只占 framebuffer 一角时用）
enabled = false
x = 0; y = 0; w = 0; h = 0

[input]
pointer_mode   = "auto"            # auto | absolute | relative
pointer_device = ""                # 留空 = 按 capabilities 自动
touch_button   = 330               # BTN_TOUCH（笔屏需要，不是 BTN_LEFT）
send_click_with_touch = true       # 同时发 BTN_LEFT
keymap         = "auto"            # auto | us | de | ...
grab = { enabled = true, toggle_key = "Ctrl+Alt+G", release_on_focus_loss = true }
hotkeys = { fullscreen = "F11", screenshot = "Ctrl+Alt+S", reset = "Ctrl+Alt+R", quit = "Ctrl+Alt+Q" }

[audio]     enabled = false
[clipboard] enabled = false
[usb]       redirection = false
[logging]   level = "info"; file = ""

# 机型/固件 profile：覆盖上面任意键
[profiles.pear-a6pro]
display.crop = { enabled = false }
input.touch_button = 330

[profiles.apollo]
display.rotation = 0
display.crop = { enabled = true, x = 0, y = 0, w = 960, h = 266 }
```

**优先级（高 → 低）**：命令行参数 > `--profile`（可叠加，后者覆盖前者）> 用户配置
（GUI 面板写入）> 内置默认。  
启动时打印"生效配置 + 每键来源"；支持热重载（监听文件 + GUI 改动即时生效，只重建必要部件）。

## 4. 适配层接口

```python
@dataclass(frozen=True)
class Frame:                       # 与协议无关的一帧
    pixels: bytes
    width: int
    height: int
    stride: int
    fmt: str                       # "xrgb8888" | "rgb565" | ...

class Capabilities(NamedTuple):
    absolute_pointer: bool
    send_keys: bool
    resolution_events: bool        # VNC=True（事件），QMP=False（轮询）
    clipboard: bool
    audio: bool
    hotplug: bool

class FrameSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> Frame | None: ...
    def resolution(self) -> tuple[int, int] | None: ...
    def capabilities(self) -> Capabilities: ...

class InputSink(Protocol):
    def pointer(self, x: int, y: int, buttons: int, absolute: bool) -> None: ...
    def key(self, code: int, down: bool, mods: int) -> None: ...
    def capabilities(self) -> Capabilities: ...

class Backend(Protocol):            # 组合；允许只读 / 只发
    source: FrameSource | None
    sink:   InputSink | None
```

要点：

- **不假设固定分辨率**：`resolution()` 每次向后端取（QMP=取 `screendump` 尺寸；VNC=桌面尺寸/resize 事件）。
- **不假设设备名**：`pointer_mode="auto"` 时按 `capabilities()` 选绝对/相对；`pointer_device` 缺省不参与。
- 新增协议 = 加一个 `Backend`；新增机型 = 加一个 `profiles/*.toml`。

## 5. 模块划分与目录

```
vmgui/
  __main__.py             # CLI：--config --profile --mode --uri --fullscreen ...
  config.py               # schema 校验 / 合并 / 保存 / 热重载 / 来源追踪
  core/session.py         # 连接生命周期、重连（指数退避）、错误上报
  core/events.py          # 事件总线: frame_ready / resolution_changed / error / connected
  core/logging.py
  backends/base.py        # 上述 Protocol / Frame / Capabilities
  backends/qmp.py         # 参考实现（与 dpctl 共享 QMP 代码）
  backends/vnc.py         # 可选
  view/window.py          # 主窗口、状态栏、快捷键
  view/canvas.py          # 缩放/裁剪/旋转/fullscreen；分辨率变化时原地重建 surface
  view/scaling.py         # nearest / bilinear / lanczos
  view/settings_dialog.py # 设置面板
  input/mapper.py         # 屏幕 → guest 坐标逆变换（scale/crop/rotation 全反算）
  input/grab.py           # 捕获/释放、焦点、快捷键提示
  input/keymap.py
  profiles/*.toml         # 每机型一份
  tests/
```

## 6. 实现步骤

1. **MVP**：`config.py` + `backends/qmp.py`（`screendump` / `input-send-event`）+ 窗口/缩放/全屏 +
   鼠标捕获快捷键 → 在本机 dictpen（560×170）跑通。
2. **自适应**：分辨率轮询 → `canvas` 原地重建（不闪烁、焦点不丢）。
3. **配置化**：设置面板 + 保存/重载/恢复默认 + profile 运行时切换。
4. **差异适配**：`pointer_mode=auto`、keymap、crop/rotation、DPI。
5. **扩展**：VNC backend、剪贴板、断线重连、诊断面板（生效配置 / 后端能力 / 帧率）。
6. **测试**。

## 7. 关键伪代码

```python
# 帧循环：分辨率自适应，不闪、不丢焦点
def on_frame(frame: Frame) -> None:
    if (frame.width, frame.height) != canvas.guest_size:
        canvas.set_guest_size(frame.width, frame.height)   # 只改 surface/sizeHint
        window.apply_display_mode(cfg.display.mode)        # 重算缩放；不 hide/不重建窗口
        status.show(f"{frame.width}x{frame.height}")
    canvas.blit(frame.pixels)                              # 内存合成后一次 swap

# 坐标逆变换（不对任何尺寸做硬编码假设）
def screen_to_guest(px: int, py: int) -> tuple[int, int]:
    x, y = inverse_rotate(px, py, cfg.display.rotation, view_size)
    x, y = inverse_scale(x, y, scale)                      # mode="1:1" 时 scale == 1
    x += cfg.display.crop.x
    y += cfg.display.crop.y
    return clamp(x, 0, guest_w - 1), clamp(y, 0, guest_h - 1)

# 输入：按能力选绝对/相对
def on_mouse_move(px, py):
    if sink.capabilities().absolute_pointer and cfg.input.pointer_mode != "relative":
        x, y = screen_to_guest(px, py)
        sink.pointer(x, y, buttons=0, absolute=True)
    else:
        sink.pointer(dx, dy, buttons=0, absolute=False)     # 相对模式用增量
```

## 8. 与 dictpen-vm 的集成

- QMP：`tcp:127.0.0.1:4446`（`dpctl.py` 的 `QMP_PORT`）。
- 作为默认显示前端：把 `dpctl up` 的 `-display gtk` 换成 `-display none`，由 vmgui 连 QMP；
  `--display gtk` 保留为回退（配置项控制）。
- 笔屏特例：`input.touch_button = 330` + `send_click_with_touch = true`（touchbridge 的存在
  就是为了让 app 认 BTN_TOUCH）。
- 缩放默认 `nearest`：guest 分辨率很小（560×170），放大时平滑会糊。

## 9. 测试计划与验收

**单元**：配置优先级合并 / 坐标逆变换（含 crop+rotation）/ 缩放算法 / keymap / 重连退避。

**集成（本机真跑）**：

- 560×170 的 dictpen VM：1:1、fit、stretch、fullscreen 切换；鼠标绝对↔相对；捕获/释放；
- 换一份不同分辨率 profile（如 `bochs-display,xres=960,yres=266`，或另一机型 rootfs）→ 验证零改码自适应；
- 断线（kill QEMU）→ 自动重连并恢复尺寸。

**验收标准**（对应需求原文）：

1. 更换不同固件/分辨率后 GUI 自动适配，无需改代码；
2. 常用差异项均可通过配置或 UI 修改；
3. 鼠标、键盘、显示、缩放行为符合预期；
4. 新增一种固件适配只需增加配置（或一个适配器）。

## 10. 实现补充（与本文的偏差，已落地）

代码在 `vmgui/`，`dpctl up` 默认已经用它当显示前端。与设计稿的差异与实现细节：

**配置键补充**（设计稿里没有、实现中需要的）：

- `display.zoom`（`mode="free"` 的缩放倍数）、`display.initial_zoom`（初始窗口 =
  guest 尺寸 × 该值，默认 2——笔屏 560×170 太小，1:1 的窗口没法用）；
- `display.pointer_marker`（画布内画触摸反馈环，替代原来叠在 QEMU 窗口上的
  `dpctl pointer` 覆盖层）；
- `input.hotkeys.settings`。

**后端（`backends/qmp.py`）**：

- 取帧用 QMP `screendump` 落 PPM 再读文件（实测 22ms/帧 @560×170），**不依赖 4445
  监视器**；文件尺寸即 guest 当前 console 分辨率，所以分辨率完全靠取帧本身获得。
- **一个后台线程**包办取帧与输入：空闲 5ms 醒一次，输入队列里的连续移动会合并，
  所以输入延迟约 5ms（取帧时会短暂让出）。
- 握手必须**握住连接等问候语**，不能"超时就断开重连"——见下面那条 QEMU 的坑。

**热更新边界**：只有后端构造时烘进去的键（`connection.*`、`display.frame_poll_ms`、
`input.touch_button`、`input.send_click_with_touch`）变化才重建后端；改显示模式/缩放
算法/指针标记/快捷键只刷新画布与菜单，**不重连 QMP**。

**实测到的 QEMU 行为（重要，别踩回去）**：

- QMP 的 socket chardev **只服务一个客户端**，被 vmgui 占着时 `dpctl tap` 会连不上；
- 更坑的是**"连上又不读数就断开"会把它的 `listen` 卡死**（backlog=1，连接一直挂在
  backlog 里），此后所有连接都返回 `WinError 10061`，只能重启 QEMU 才恢复。所以
  `dpctl` 不对 4446 做探测，vmgui 连上后立刻等问候语（不退化成半开即弃）。

**分辨率自适应实测**：guest 开机阶段会先报 640×480、再切 560×170，vmgui 是**原地
重建**（只改 surface/sizeHint 与窗口尺寸，不重建窗口/控件），不闪、不丢焦点。

**与 dpctl 的集成**：`dpctl up --display vmgui|gtk|none`（默认 `vmgui`：QEMU 走
`-display none`，前端由 `find_pyside_python()` 找到的带 PySide6 的解释器拉起）；
找不到 PySide6 时自动回落 `gtk`。另外加了 `--rescue`：`DICTPEN_INIT=/bin/sh`，
不启 pen 的 init，串口直接给 root shell（用来修坏掉的 `/userdata`）。

**测试**：`vmgui/tests/test_units.py`（26 项：配置合并/优先级/来源、坐标逆变换含
crop/rotation/stretch/letterbox、缩放、键映射、指针模式）、
`vmgui/tests/smoke_gui.py`（20 项离屏 GUI：窗口/画布/五种显示模式/设置面板增删改）、
`vmgui/tests/smoke_live.py`（连真机取帧 + 各模式离屏渲染 + 注入点击）。

**未实现（仍是可扩展项）**：VNC 后端、音频、剪贴板、USB 重定向；`keymap` 只带
`us`/`de` 两套布局；`lanczos` 目前映射到 Qt 的平滑缩放（真 lanczos 需在此处接
Pillow）；相对指针模式只在后端不具备绝对轴时才会用到（本项目的 virtio-tablet 有绝对轴）。