# dictpen-vm — 有道词典笔实机模拟开发环境

在 Windows（无需管理员权限、无需 WSL）上用 QEMU 运行**词典笔真实固件**的虚拟机。

**虚拟机是一个框架，固件是可拆卸的包。** 框架本身不含任何机型信息（镜像、面板尺寸、
显示 HAL 补丁…一律没有）；一台机器的全部会变的东西放在 `packs/<名字>/` 里，启动时用
`dpctl up --pack <名字>` 挑一个。仓库自带七个固件包：**a6pro / a7pro / s6pro / s7pro /
x5pro / x6pro / x7**，覆盖 Rockchip RV1106、CVITEK CV1826、RK3562 三个平台；各包的
固件版本、面板与适配边界见各自的 `packs/<名字>/README.md` 或 `DEVLOG.md`。

## 文档

| 文档 | 给谁看 | 内容 |
|------|--------|------|
| [docs/使用手册.md](docs/使用手册.md) | 想用起来的人 | 重建依赖、取固件、选包启动、看画面与操作、传文件/adb、持久化、虚拟硬件、激活 |
| [docs/固件包开发教程.md](docs/固件包开发教程.md) | 要加一台机器的人 | 三个概念、`pack.toml` 全字段、声明式/命令式适配、写 runtime、调试与检查清单 |
| [docs/插件模型.md](docs/插件模型.md) | **要改框架 / 写插件的人** | 宿主与四个插件点、决策树、包/runtime/适配/显示接口契约、零机型自检、接口变更纪律 |
| [docs/疑难排查.md](docs/疑难排查.md) | 卡住的人 | 按现象分类的排查：黑屏/花屏、触摸不响应、联网误判、首启、激活、SSH/adb |
| [docs/发布说明.md](docs/发布说明.md) | 要安装/升级的人 | 当前版本更新、四步安装、镜像下载与命名、已知问题、升级说明 |
| [.trae/documents/firmware-packs-design.md](.trae/documents/firmware-packs-design.md) | （**历史设计文档**） | 早期设计：包格式、runtime 接口、换包机制、迁移阶段与验收判据 |
| [.trae/documents/vmgui-design.md](.trae/documents/vmgui-design.md) | 想改显示前端的人 | vmgui 的配置 schema、适配层接口、模块划分与测试计划 |
| 本文件 | 第一次来的人 | 是什么、快速上手、能力表、`dpctl` 命令、目录结构、引导机制、换机型、分发重建 |

## 快速开始

```bat
python scripts\dpctl.py packs          :: 有哪些固件包、镜像是否就位
python scripts\dpctl.py up --pack a6pro  :: 启动 VM（vmgui 画面 + 持久化状态盘）
python scripts\dpctl.py wait --adb     :: 等实机就绪，并连通 adb
python scripts\dpctl.py shell          :: 进实机命令行（可直接 dpctl shell）
python scripts\dpctl.py sh -c "id"     :: 跑单条命令，返回真实退出码
python scripts\dpctl.py put a.txt /userdata/a.txt   :: 传文件进实机
python scripts\dpctl.py get /userdata/a.txt a.txt   :: 取文件到电脑
python scripts\dpctl.py adb shell      :: 电脑端 adb 调试（自动 connect）
python scripts\dpctl.py status         :: 查看 VM / 端口 / adb 状态
python scripts\dpctl.py down           :: 关闭虚拟机
python scripts\dpctl.py console        :: 打开词典笔控制台窗口（也可双击 vm-console.cmd）
```

也可以继续用原始脚本：

```bat
vm-console.cmd      :: 打开词典笔控制台（设备画面 + 状态栏 + 抽屉），等价于 dpctl console
run-vm.cmd          :: 启动虚拟机（弹出 GTK 显示窗口，即"词典笔画面"）
ssh-vm.cmd          :: 新开窗口，root@127.0.0.1 -p 2222 登录实机系统
console.cmd         :: 串口控制台（看启动过程/调试，Ctrl+] 退出）
stop-vm.cmd         :: 关闭虚拟机
```

> `dpctl` 与 `run-vm.cmd` 等价，但会额外管理持久化状态盘与 pid、并在恢复重启后仍能识别 VM 进程。

## 已验证能力

| 能力 | 状态 |
|------|------|
| 实机 rootfs（EROFS/ext4）完整引导 | ✅ 主机名 `YoudaoDictionaryPen-000` |
| root SSH 登录（密钥认证，端口 2222） | ✅ |
| 串口控制台（TCP 4444，可脚本化） | ✅ |
| 可写根文件系统（overlayfs） | ✅ 改动本次运行有效 |
| GUI 显示窗口（vmgui，默认；QEMU GTK 为回退） | ✅ 按包声明的面板尺寸（560×170…960×480）显示 framebuffer；vmgui 按取到的帧尺寸自适应分辨率与缩放（见"用 vmgui 客户端"） |
| 鼠标 = 触摸（点击 QEMU 窗口驱动笔的 UI） | ✅ uinput 触摸桥 + BTN_TOUCH，见"触控"一节 |
| 键盘/鼠标输入设备 | ✅ virtio-keyboard/tablet 已接入 |
| 词典笔控制台（`dpctl console`） | ✅ 设备画面 + 状态栏 + 抽屉（串口 / guest 应用 / 命令 / 文件 / 配置 / 蓝牙 / 声音）；设备下拉切换、F11 全屏、实体键与快速启动工具条 |
| 声音输出（宿主） | ✅ QEMU usb-audio + 宿主 dsound；控制台声音页读写客机音量/静音，并常驻提示宿主输出设备的静音/音量；选中其他操作会中断上一条；a6/a7 固件仅带 tinyalsa（`tinyplay` 需 48 kHz 立体声） |
| 实体键（Home：6 台；音量±：仅 x6pro） | ✅ 客机内 uinput 替身 + 控制台工具条；Home 回桌面；音量改真实 ALSA（见各包 README 的实体键节、`packs/x6pro/DEVLOG.md` §7.11、台账 Q92） |
| 快速启动（首页快照） | ✅ `dpctl fast save` 把**普通**状态盘复制成 qcow2 并在首页 savevm；`dpctl up --fast` / 控制台「快速启动」约 6 s 恢复首页。快照无音频（usb-audio 不可迁移），要声音请用普通「启动」冷启动（见台账 Q93） |
| 持久化状态盘（`/state`、`/userdata` 重启不丢） | ✅ 第二块 virtio 盘，首次开机自动格式化 ext4 |
| PC 端 adb 调试（`adb shell` / `push` / `pull`） | ✅ guest `adbd` 走 TCP，host 侧自动 connect |
| 有道 UI（miniapp/falcon/Skia）渲染 | ✅ 上屏、方向正确；窗口按包声明的面板尺寸显示（见"显示与输入"节） |
| Wi-Fi 硬件虚拟化 | ✅ `mac80211_hwsim` 虚拟网卡（`wlan0`）+ 笔自带的 nl80211 栈（`wpa_supplicant`/`wifi-manager`）；`wlan1` 上是可扫描/可关联的虚拟热点 `Youdao-VM`，并带转发+NAT 上行（见"无线"） |
| 蓝牙硬件虚拟化 | ⏳ 客机内虚拟 HCI（`hci_uart` + 宿主 `bt-hci-emu.py`）；控制台可上电/有界扫描，能触发模拟设备；**不是电脑蓝牙**，连接真实设备需 USB 直通 |
| 跳过激活（开发凭证） | ✅ `S00license` 开机写入 `/userdata/cfg/license`，app 直接进桌面（见"激活与凭证恢复"） |
| 电池/电源 | ✅ `S00battery` 用 tmpfs 发布健康电量，修掉进桌面后约一分钟的"低电量关机"（见"电源"） |
| 崩溃日志清理 | ✅ `S00corelog` 开机把 `/userdisk/corefile` 裁到最新 500 个，并常驻 janitor 每 10 分钟再裁（防状态盘被堆满导致点击失效） |

## 开发与调试（dpctl）

`scripts/dpctl.py` 是本项目的开发入口，覆盖"启动 → 进系统 → 传文件 → 跑命令/用 adb 调试"的闭环：

| 子命令 | 说明 |
|--------|------|
| `packs [NAME]` | 列出固件包（runtime / soc / 面板 / 镜像是否就位）；给 NAME 时打印该包解析后的全部路径、补丁列表并核对 `[checks]` 的 MD5 |
| `up [--pack NAME] [--fresh] [--fast] [--headless] [--display vmgui\|gtk\|none] [--rescue] [--dev] [--pointer\|--no-pointer] [--license\|--no-license]` | 启动 VM；**用哪个固件由 `--pack` 决定**（`packs/<名>/pack.toml`，只装了一个包时可省略）。`--fresh` 清空该包的状态盘重来，`--headless` 不开窗口；`--display` 选显示前端（默认 `vmgui`：QEMU `-display none` + 客户端走 QMP；`gtk` 回退；`none` 不要窗口）；`--rescue` 直接进串口 root shell（不启 pen 的 init，用于修坏掉的 `/userdata`）；`--dev` 保留 miniapp launcher；GTK 窗口下默认调出触控反馈指针（`--no-pointer` 关掉），默认开机尝试恢复激活凭证（`--no-license` 关掉） |
| `fast save [--pack NAME] [--force]` / `fast status` | 建立/查看快速启动快照：把**普通**状态盘复制到 `vm/fast-<包>.qcow2`，无音频冷启动到首页后 `savevm ready`。恢复用 `up --fast`（约 6 s；无声音）；与普通状态盘互相独立 |
| `console [--pack NAME]` | **打开词典笔控制台窗口**（`vmgui --console`，即 `vm-console.cmd` 双击的那件事）。只负责"起进程 + 记账"：解释器由 `find_pyside_python()` 一处决定；已有一个控制台在跑（读 `vm/console.pid`）或 QMP `tcp:4446` 被别人占着时**直接拒绝**，因为 QMP 只服务一个客户端——想让控制台独占画面，用 `dpctl up --display none`。窗口起来后 stdout/stderr 落 `vm/console.out`。**关掉它只有关窗口那颗键**：`dpctl down` **故意不收控制台**（#70，管理器不该被"关机"顺手关掉），虚拟机停止后显示「虚拟机未运行」。`--pack` 只定**初始**目标，之后换台在窗口里：工具条那颗「设备 x5pro」下拉一台一行、当前那台打勾，缺少镜像的灰显不可选（#28）；虚拟机运行中选择另一台会先确认，同意后**仅关闭，不自动启动**（措辞见 `vm/ui-council/31-console-copy.md` §20）。键盘：**F11 全屏 / Esc 退出全屏**（全屏的是窗口，工具条与抽屉都还在；画面按「适合」跟着放大、不裁不拉伸），Tab 收/开抽屉；工具条另有 **快速启动 / 保存快照**：保存快照需先关机（约 1.5–2 分钟，无音频启动一次），之后「快速启动」约 6 s 恢复首页；普通「启动」仍是有声音的冷启动（见台账 Q93）。工具条还有实体键 **Home / 音量－ / 音量＋**（按包映射 Home 码：s6/x5=139、其余=102；音量只有 x6pro；x7 无键，见台账 Q92） |
| `pack export NAME [-o FILE]` / `pack import FILE [--force]` | 把整个固件包打成单个 `.dpm`（tar.gz）带走 / 装回来 |
| `down` / `status` | 关闭 / 查看状态（即使 VM 是 `run-vm.cmd` 起的也能识别） |
| `wait [--timeout N] [--adb]` | 等实机 shell 就绪；`--adb` 同时拉起并等待 adb |
| `sh -c CMD [--timeout N] [--serial]` | 实机内执行命令并返回**真实退出码**；SSH 未起来时自动回落串口 |
| `shell` | 交互式实机命令行（SSH） |
| `put LOCAL REMOTE` / `get REMOTE LOCAL` | 传文件（优先 adb push/pull，未连 adb 时走 SSH） |
| `adb [ARGS...]` | 电脑端 adb：自动确保 guest `adbd` 在跑并 `adb connect`，随后透传参数，如 `adb shell`、`adb logcat`、`adb push` |
| `view [--w N --h N] [--zoom N]` | **直接输出词典笔画面**：开一个按笔分辨率定尺寸的窗口，实时显示笔的 GUI（默认可读性放大 2×）；不指定 `--w/--h` 时用当前包的面板尺寸 |
| `pointer` | **触控反馈指针**：在 QEMU 窗口上贴一层透明、可点穿的置顶覆盖层，鼠标位置画一个圈（按下变色变大），让"笔屏的手指在哪"看得见（仅 Windows 宿主）。`dpctl up` 开窗口时会**自动调出**，`--no-pointer` 关闭 |
| `nopointer` | 把上面那个覆盖层收掉（`up` 之后想中途隐藏时用） |
| `license` | 在实机里跑一次激活凭证恢复（`scripts/restore_license.sh`），见"激活与凭证恢复" |
| `screenshot [--out FILE] [--zoom N]` | 把笔的 GUI 存成 PNG（默认 `vm/screen.png`；裁剪尺寸默认取当前包的面板尺寸） |
| `tap X Y [--w N --h N]` | 在笔屏坐标注入一次触摸（默认用当前包的面板尺寸换算） |

### 端口

| 用途 | 宿主机 | 客机 |
|------|--------|------|
| SSH (dropbear) | 127.0.0.1:2222 | 22 |
| ADB (adbd) | 127.0.0.1:15037 | 5555 |
| 串口控制台 | 127.0.0.1:4444 | — |
| 蓝牙 HCI（`hci_uart`） | 127.0.0.1:4447 | `/dev/ttyAMA1` |
| QEMU 监视器 | 127.0.0.1:4445 | — |
| QMP（`tap` 注入用） | 127.0.0.1:4446 | — |

ADB 宿主端口特意选在 adb 的模拟器探测区间（5554–5585）之外，避免 `adb devices` 里出现重复的 `emulator-XXXX`。

排查显示问题时，可以用 QEMU 的 trace 看 guest 到底有没有把画面推给 host：

```bat
set DICTPEN_TRACE=virtio_gpu*
python scripts\dpctl.py up
:: 事件写到 vm\trace.log，关注 res_flush / res_xfer_toh_2d 是否还在增长
```

### adb 说明

真机的 `adbd` 默认只走 USB（`usb_ffs` + `S98usbdevice`），QEMU 里没有 USB gadget，
因此由 `dpctl adb` 在实机内以 TCP 方式拉起它（`ADB_TCP_PORT=5555`，见
`/etc/profile.d/adbd_tcp_port.sh`），并预建 `adbd` 需要的 `/data/misc/adb/adb_keys`
与 `/tmp/.adb_auth_verified`（后者是 `/usr/bin/adbd_auth.sh` 的免密标记）。

手工使用时：`adb connect 127.0.0.1:15037` 之后即可 `adb shell`。

### 持久化

改动落在第二块 virtio 盘上，**每个固件包一份**（`vm/state-<包名>.img`，不同机型不串味），
重启不丢：

- overlay 的 upper 层改为 `/state/upper`（系统级修改变为持久）；
- `/userdata`、`/userdisk` 由 `/state/...` bind mount（`/data` 是指向 `/userdata` 的软链，随之一并持久）；
- 状态盘格式为 ext4，**首次开机**由实机自带 e2fsprogs（`chroot` 调 `/sbin/mke2fs`）创建；
- 若状态盘不可用，自动回落到原来的 tmpfs 行为，VM 仍能正常开机。

> **扩容只改 VM 状态盘**：控制台「配置」页把它扩到更大后，客机 `df` 会变；x6pro、x7、
> a6pro、x5pro、s6pro、s7pro 的设置页已由 preload shim 改成读 VM 状态盘（扩容后会跟着变，
> 两条入口「空间清理」「关于→存储容量」都跟随），其他机型逐步适配。
> 实际数字看「配置」页的宿主/客机两格。

`dpctl up --fresh` 可删掉当前包的盘重来（下次开机重新格式化）。

## GUI 与硬件虚拟化说明

### 显示（GUI）
- 虚拟机通过 **bochs-display** 提供显示设备（线性显存，guest 写内存即上屏），内核创建
  `/dev/fb0`（`bochs-drmdrmfb`），QEMU 用 **GTK 窗口**呈现画面（`-display gtk`）。
  早期用的是 `virtio-gpu-pci`，因为 falcon 不做 flush 而看不到画面，见 [显示：换掉 virtio-gpu](#显示换掉-virtio-gpu)；
- 可通过 `console.cmd` 或 SSH 往 `/dev/tty1` 写内容，画面即显示在该窗口。
- ⚠️ **SDL 后端在本机有兼容问题**（QEMU 主线程会卡死），故使用 GTK；
  若 GTK 不可用，可试 `-display sdl` 或 `-display default`。

### 无线：Wi-Fi 与蓝牙硬件虚拟化

真机的无线是 **SDIO 组合芯片**（Wi-Fi 由 `atbm603x_wifi_sdio` / `bl_fdrv` 带 Firmware 跑，
蓝牙由 `rtc_hciattach` 挂在 UART 上），QEMU 里没有这些硬件，VM 用等效的虚拟硬件顶替，
**笔里的软件栈原样运行**（`wpa_supplicant -Dnl80211`、`wifi-manager`、`bluetoothd`、`bt-manager`）。

#### Wi-Fi：`wlan0` + 虚拟热点
- guest 内核加载 `rfkill` / `cfg80211` / `libarc4` / `mac80211` / `mac80211_hwsim`
  （`mac80211_hwsim radios=2`），生成 `wlan0`（给笔的 station 栈）和 `wlan1`；
- 笔自带的脚本会自动接上：`S21mountall.sh` 里的 `guardian_run runWpas` / `runWifiMgr`
  等到 `wlan0` 出现，随即启动 `wpa_supplicant -Dnl80211 -iwlan0` 与 `wifi-manager`，
  所以 app 的「配置网络」页可用（开关、扫描、连接都能走通）；
- **可扫描、可关联的热点**：笔的固件没有 `hostapd`（真机 AP 由芯片固件实现），
  于是让 `wlan1` 跑一份 **Debian aarch64 的 hostapd**——guest 内核就是 aarch64，
  64 位二进制直接能跑（只差 libc/libnl/libssl，和 hostapd 一起打进 initramfs）。
  SSID `Youdao-VM`、open 网络、地址 `10.201.126.1`（与固件 `dnsmasq` 的
  `dhcp-range` 同网段，`S80dnsmasq` 会直接开始发租约）。
  实测：`wlan0` 扫描到 `[ESS] Youdao-VM`，关联后 `wpa_state=COMPLETED`。
- 开关与配置：内核 cmdline 加 `DICTPEN_AP=0` 可关掉热点；
  `hostapd.conf` 落在 guest 的 `/root/.vm/ap/`，加 `wpa=2` + `wpa_passphrase=` 即为 WPA2。
- **热点的上行**：guest 里没有 iptables，所以 initramfs 额外带了一份 **Debian aarch64
  `iptables-legacy`**（libip4tc/libip6tc/libxtables + 用到的 xtables 插件）。
  `S00wifi-ap` 在起完 AP 后会打开转发并装好 NAT：
  ```
  ip_forward = 1
  iptables -t nat -A POSTROUTING -o eth0 -s 10.201.126.0/24 -j MASQUERADE
  iptables -A FORWARD -i wlan1 -o eth0 -j ACCEPT
  iptables -A FORWARD -i eth0  -o wlan1 -j ACCEPT
  ```
  开机日志会打 `S00wifi-ap: uplink NAT ready (10.201.126.0/24 -> eth0)`。
  实测：以 `10.201.126.x` 为源、经 eth0 出去的目标 **3/3 通**，`/proc/net/nf_conntrack`
  里能看到 SNAT（`src=10.201.126.1 → 8.8.8.8`，回包 `dst=10.0.2.15`）。
- ⚠️ **但 pen 自己没法当这个热点的客户端**：热点的 AP（`wlan1`）和 pen 的站点（`wlan0`）
  在**同一个内核**里，`wlan0` 发的 ARP 请求虽然上了空口（`iw dev wlan1 station dump`
  的 rx/tx 计数会涨），但内核给它的 ARP 应答因为"目标 IP 也是本机地址"被本地消化、
  **不会发回空中**，于是网关 MAC 永远解析不出来（`/proc/net/arp` 里一直是 `0x0`），
  客户端方向的数据流根本起不来。这是同内核 AP+STA 的固有限制，不是配置问题。
  真机上热点的客户端是手机（另一个网络栈），所以不受影响——上面的转发+NAT 规则
  就是给那种客户端准备的。
  实际影响：pen 关联热点后仍走 `eth0`（metric 1002 优于 Wi-Fi 的 3003），云功能照常。

#### 蓝牙：`hci0`（`hci_uart` + 宿主控制器模拟）
- 两条常见路子都不可用：QEMU 早已删除蓝牙模拟（没有 `-bt`、也没有 `usb-bt-dongle`），
  Debian 的 arm64 内核里也没有 `hci_vhci.ko`。因此改用 **`hci_uart` + 第二个串口**：
  QEMU 把 `-chardev socket`（TCP 4447）接到 guest 的 `/dev/ttyAMA1`，
  guest 里 `btattach -B /dev/ttyAMA1 -S 115200 -P h4` 挂上 H4 协议 → 得到 `hci0`；
- 链路另一端是宿主上的 **`scripts/bt-hci-emu.py`**（由 `dpctl up` 自动拉起，
  `dpctl down` 自动结束；日志 `vm/bt.log`，每命令的 in/out 都有）：
  它按 H4 收发、扮演一个 4.2 双模控制器（`Reset` / `Read Local Version` / `Features` /
  `Commands` / `BD_ADDR` / `Buffer Size` / LE 系列命令，以及 `Inquiry`、LE 广播报告等）。
  guest 内核因此能走完控制器初始化，BlueZ 会注册并上电 `hci0`；
  - 内核只在"回复比它期望的**短**"时才判失败，所以未知命令一律回**成功 + 16 字节**，
    而不是 `Unknown HCI Command`（否则初始化链会在中途断掉）；
- guest 侧还差两件固件没做的事，initramfs 自动补上：
  1. **系统 D-Bus**：这套固件只起了 session bus，而 `bluetoothd` 需要
     `/var/run/dbus/system_bus_socket`，缺了就直接 `Unable to get on D-Bus` 退出；
  2. **`hciconfig` 垫片**：rootfs 里没有这个命令，而 `bt_init.sh` / `runBluez`
     要用它轮询 `hci0`（缺了会死循环），垫片按 `/sys/class/bluetooth` 报告设备。
- 笔的蓝牙栈与真机一致，由 app 的「蓝牙」入口拉起（`runBluez` → `bluetoothd`，
  `runBtMgr` → `bt-manager`）。手工验证：

```sh
ls /sys/class/bluetooth          # hci0 在不在
hciconfig                        # 垫片输出 hci0
/usr/bin/runBluez &              # 内含 bluetoothd -C -n -E
/usr/bin/dbus-send --system --print-reply --dest=org.bluez / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects | grep hci0
/usr/bin/dbus-send --system --print-reply --dest=org.bluez /org/bluez/hci0 \
    org.freedesktop.DBus.Properties.Set string:org.bluez.Adapter1 \
    string:Powered variant:boolean:true
```

- **现状**：`hci0` 存在、guest 内核初始化通过、BlueZ 有 `/org/bluez/hci0` 并可上电；
  `btmgmt --timeout 8 find` 会下发 `Inquiry`，模拟器也回了 `11:22:33:44:55:66`
  （`vm/bt.log` 可见），但 `btmgmt`/BlueZ 的输出与**连接/配对语义还没补齐**。
- **边界（控制台这一页的读法）**：开关/扫描的是**客机 `hci0`**，不是电脑蓝牙，也不碰
  Windows 的蓝牙适配器；扫描只能碰到模拟器里的假设备。控制台「连接」已禁灰，并写明
  真实耳机/音箱需要把一只 **USB 蓝牙 dongle 直通**给 QEMU（`usb-host` + 客机 `btusb`）；
  内置 Intel 蓝牙不参与。控制台的扫描已改用有界 `btmgmt --timeout 8 find`——
  `bluetoothctl` 在 `bluetoothd` 没起来时会挂住 D-Bus。

### 电源：电池与"低电量关机"

固件的电量来自 `/sys/class/power_supply/{battery,ac,usb,charger}`：`hal-battery` 经
`libydhal_devices.so` 读这些文件，再用 ubus 发布给 app（`YUbusSender`）。VM 没有 PMIC，
该目录是空的，于是电量被读成 0%，app 进桌面约一分钟后弹"低电量即将关机"并真的关机——
每次开机只有一分钟寿命。`S00battery` 钩子用 tmpfs 覆盖 `/sys/class/power_supply`，
写一份健康电量（80%、4.2V、`Discharging`、AC/USB/充电器均未接入），读数从此正常。

```bat
python scripts\dpctl.py sh -c "cat /sys/class/power_supply/battery/capacity"
:: 80
```

数值是静态的：插拔充电器不会改变它，`safe_powerdown` / `system_power_off` 这类脚本
读到的也是这份值。

### CPU 加速（无法使用 WHPX）
- 本机 Windows 的 **WHPX 不支持 ARM64 客户机**（QEMU 的 WHPX 仅支持 x86 客户机），
  因此 **无法使用硬件加速**，只能用 QEMU 的 **TCG 纯软件模拟**。
- 已启用多线程 TCG 并加大资源：`-accel tcg,thread=multi -smp 4 -m 2G`。
- 速度较慢属正常现象（32 位 ARM 用户态跑在 aarch64 内核的 COMPAT 层上）。

## 目录结构

| 路径 | 说明 |
|------|------|
| `scripts/dpctl.py` | **开发/调试入口**：选包启动、传文件、实机命令行、adb（见上节） |
| `packs/<名>/` | **固件包**：一台机器的全部会变的东西（`pack.toml` + `images/` + `patches/` + `README.md`），见"固件包与镜像选择" |
| `scripts/packlib.py` | 包解析/校验/导入导出，以及"包 → initrd"的合并（框架通用 cpio + 包 patches + `pack.env` + EDID） |
| `scripts/runtimes/` | 框架内置的**多种运行框架**（boot backend）：`rv1106_linux.py` 是第一个；包里的 `[platform].runtime` 选一个 |
| `deps/manifest.toml` | **依赖清单**：缺什么、去哪拿、怎么校验；`scripts/bootstrap.py` 按它重建环境 |
| `scripts/bootstrap.py` | 一条命令检查/获取依赖并构建 initramfs（新环境入口） |
| `scripts/package.py` | 打"可传播"的包（只含代码/文档/固件包描述，自检不含固件/私钥/大文件） |
| `vm-console.cmd` / `run-vm.cmd` / `stop-vm.cmd` / `ssh-vm.cmd` / `console.cmd` | 一键脚本（Windows）——只做转发，QEMU 参数与解释器选择统一在 `dpctl.py`，两处不会漂移。`.cmd` 必须**纯 ASCII**（cmd.exe 按 OEM 码页读，UTF-8 中文会碎成乱码并劈断 `echo`），且 `if (…)` 块里的 `echo` **不许带括号**（`)` 会提前结束代码块，实测失败提示会在成功路径上整段印出来）。改这些文件还要**用保留字节的工具**：Git Bash 会把命令行里的 `nul` 重写成 `/dev/null`，用 heredoc 写出来的 `>nul` 落盘就不是那几个字节，cmd.exe 每跑一次多印一行"系统找不到指定的路径"；这三条由 `vmgui/tests/test_units.py` 的 `TestRootLaunchers` 钉住 |
| `scripts/run-vm.sh` | Git Bash 版启动脚本 |
| `tools/msys2/ucrt64/` | 便携版 QEMU 11.1.1 及全部依赖（免安装） |
| `tools/kernel-root-gui/` | Debian `linux-image-6.1.0-50-arm64`（含 DRM/virtio-gpu/ext4 模块）——**框架自带通用内核** |
| `tools/busybox-root/` | busybox-static（initramfs 用） |
| `tools/ssh/` | VM 访问密钥对（公钥已注入 VM 的 `/root/.ssh/authorized_keys`） |
| `tools/platform-tools/` | Android platform-tools（`adb.exe`），供 `dpctl adb` 使用 |
| `images/initramfs.cpio.gz` | 框架**通用** initramfs（busybox + 内核模块 + 引导逻辑 + 虚拟热点的 arm64 hostapd/iptables），**不含任何固件**，只构建一次 |
| `scripts/initramfs-init.sh` | initramfs 的 `/init` 源码（改后需重新构建） |
| `scripts/build-initramfs.py` | 重新打包通用 initramfs |
| `scripts/console.py` | 串口 TCP 客户端（`-c` 单命令 / `-f` 批量，回传真实退出码） |
| `scripts/bt-hci-emu.py` | 虚拟蓝牙控制器（H4，宿主侧），见"无线"一节 |
| `scripts/deb-arm64-fetch.py` | 抓取 arm64 的 Debian 包（hostapd 及其依赖）并解到 `tools/arm64-rootfs/` |
| `tools/arm64-rootfs/` | 从 Debian arm64 包解出的 hostapd + 运行库（打进 initramfs 供虚拟热点用） |
| `scripts/restore_license.sh` | 真凭证恢复脚本（框架通用的激活脚本，包用 `[adapt].license` 打开） |
| `scripts/mitm-verify.py` | 宿主侧假 verify 服务（443 + 自签证书），用于离线复现激活链路 |
| `vm/mitm/` | 假服务的自签证书与请求日志（`cert.pem`/`key.pem`/`requests.log`） |
| `vmgui/` | **QEMU 虚拟机客户端 GUI**（PySide6）：配置化、分辨率自适应、多显示模式、输入映射；`vmgui/profiles/*.toml` 是机型/固件 profile（见"用 vmgui 客户端"） |
| `.trae/documents/firmware-packs-design.md` | 固件包（框架/固件分离）的设计文档 |
| `.trae/documents/vmgui-design.md` | vmgui 的设计文档（配置 schema、适配层接口、模块划分、测试计划与验收） |
| `vm/state-<包>.img` | 持久化状态盘（ext4，512M，按包分，`--fresh` 可重建） |
| `vm/initrd-<包>.cpio.gz` | 本次启动用的 initrd（通用 initramfs + 包内容），按哈希缓存，换包/改包才重建 |
| `vm/` | 运行期产物（serial.log、qemu.log、截屏、pid） |

## 引导机制（改文件前必读）

真实设备启动链：`BootROM → U-Boot → 内核(各机型 DTB) → /sbin/init`。
模拟器替换为：**QEMU virt 机型 + Debian 通用内核 + 框架通用 initramfs + 选中的固件包**。
`dpctl up --pack <名>` 会把「框架通用 initramfs」与「包的 `patches/**` / `pack.env` /
面板 EDID」合并成 `vm/initrd-<包>.cpio.gz`（按哈希缓存），再做引导：

1. initramfs 加载 virtio/erofs/overlay/DRM 模块，把 rootfs（EROFS 只读）挂到 `/ro`；
2. 读 `/pack/pack.env`（本次启动选中的包，没有则不套用任何机型适配）；
3. `DICTPEN_OVERLAY=1` 时用 overlayfs 合并 `/ro`（只读）与可写层为 `/mnt`，
   并改 `/etc/inittab`（login→直接 root shell；去掉 `remount,ro`）；
4. 若存在状态盘 `/dev/vdb`，把它挂到 `/state`（首次为空则用实机 e2fsprogs 建 ext4），
   overlay 的 upper 改用 `/state/upper`，并把 `/userdata`、`/userdisk` 改为
   `/state/...` 的 bind mount；状态盘不可用时回落到 tmpfs；
5. `/data /userdata /userdisk /misc` 若未持久化则用 tmpfs 顶替并预建 `applog`/`cfg` 等目录；
6. 用实机自带 `dropbearkey` 生成主机密钥、注入公钥、在 inittab 加 dropbear 自启；
7. **只按包声明做适配**：`[adapt].replace` 覆盖 guest 文件（如显示 HAL）、
   `cfg_overlay` 只改列出的 `cfg.json` 键、`logo`/`touchbridge`/`battery`/`license`/`ap`
   各自装对应钩子、`hooks` 跑包自带脚本 —— 没声明的键一律不碰；
8. `switch_root` 进入实机 `/sbin/init`（`/state` 会一并移入新根）。

> 框架里没有任何机型分支：`grep -ri -e a6pro -e libfalcon -e pear` 在 `scripts/`
> （除 `runtimes/` 的平台说明）与 `vmgui/` 代码里 0 命中。

### 可写性
- 有状态盘时：根文件系统改动与 `/state`、`/userdata`（含软链 `/data`）**重启保留**。
- 状态盘不可用时：回落为 tmpfs，改动仅本次运行有效。
- `dpctl up --fresh` 删除当前包的状态盘（`vm/state-<包>.img`），下次开机重新格式化。

## 显示与输入（原理摘要）

有道的 UI 框架是 **greenui/falcon**（Skia 渲染），它**硬编码走 DRM/KMS**（`/dev/dri/card0`），
而非 fbdev。VM 里让它上屏需要：换显示设备为 bochs-display、一套 `libfalcon.so` 二进制补丁、
再把面板 `cfg.json` 归一化（尺寸/方向/bpp）。**这些都落在固件包里**
（`packs/a6pro/patches/`，由 `[adapt].replace` / `cfg_overlay` 在开机时应用）。

原理、补丁清单与勘误（fourcc 非法常量、面板方向归一化、`drmModeSetPlane failed (-13 EACCES)`）
见 [docs/插件模型.md §6](docs/插件模型.md#6-插件点④显示与输入)；排障个案见 `docs/疑难排查.md`。

自检（实机日志）——`AddFB2() ret=0` 且没有 `SetPlane failed` 才算真上屏：

```bat
python scripts\dpctl.py sh -c "grep -a -e 'AddFB2() ret' -e SetPlane /data/applog/DictPen_*.log | tail"
```

### 显示：换掉 virtio-gpu

**原因**：falcon 直接往 DRM framebuffer 里画，**不做 dirty / page-flip**（不发
`RESOURCE_FLUSH`）；而 virtio-gpu 只在收到 flush 时才把新像素传给 host，于是画面永久停在
启动图（`-trace virtio_gpu*` 的 trace 文件大小不再增长）。**解法**：换成
**`-device bochs-display,xres=560,yres=170`**——它的 framebuffer 就在设备的线性显存里，
QEMU 扫描输出直接读，guest 写完内存画面即变（语义同真机 LCD 控制器）。
initramfs 按 `ttm → drm_ttm_helper → drm_vram_helper → bochs` 顺序加载，启动后 `/dev/fb0`
是 `bochs-drmdrmfb`。原理详见
[docs/插件模型.md §6.1](docs/插件模型.md#61-虚拟屏与-edid)。

### 看画面
```bat
python scripts\dpctl.py view                :: 定尺寸窗口，实时显示笔的 GUI
python scripts\dpctl.py screenshot --zoom 2 :: 存 PNG
```

### 用 vmgui 客户端（默认显示前端）
`dpctl up` 默认不再用 QEMU 自带的 GTK 窗口，而是把画面交给仓库里的 **vmgui**
（Python 3.11+ / PySide6）：QEMU 以 `-display none` 启动，**画面与输入都走 QMP**。

```bat
python scripts\dpctl.py up                  :: 默认前端；自动找带 PySide6 的解释器
python scripts\dpctl.py up --display gtk    :: 回退到 QEMU 自带窗口
python scripts\dpctl.py up --display none   :: 不要窗口（纯 QMP，可用 screenshot 看）
```

也可以单独跑（VM 已在运行时直接连上去）：

```bat
py -3.13 -m vmgui --profile pear-a6pro      :: PySide6 目前要求 Python < 3.15
py -3.13 -m vmgui --print-config            :: 打印生效配置 + 每键来源
py -3.13 -m vmgui --list-profiles
py -3.13 -m vmgui --mode stretch --scale-algo bilinear --rotation 90
py -3.13 -m vmgui --fullscreen --pointer-mode absolute
```

要点：

- **不做任何分辨率假设**：窗口大小与缩放都按 `screendump` 拿到的帧尺寸算。实测开机
  阶段 640×480 → 560×170 的切换是**原地重建**（不闪、不丢焦点）；换机型/固件只要帧
  尺寸不同就自动跟随，不需要改代码。
- **显示模式**：`1:1` / `fit` / `stretch` / `free`（用 `display.zoom`）/ `fullscreen`；
  缩放算法 `nearest`（默认——笔屏像素少，放大用平滑会糊）/ `bilinear` / `lanczos`；
  另有 `display.rotation`（0/90/180/270）与 `display.crop`（只显示帧缓冲的一角）。
- **配置优先级**：命令行 > `--profile`（可叠加，后者覆盖前者）> 用户配置（设置面板
  写入）> 内置默认。菜单里改显示模式/算法是本次运行有效，「保存到用户配置」才落盘。
- **输入**：笔屏要 `BTN_TOUCH`，所以默认 `input.touch_button = 330` 且
  `send_click_with_touch = true`（同时发 `BTN_LEFT`——touchbridge 正是靠它翻转的）。
  `input.pointer_mode = "auto"` 时按后端能力在绝对/相对之间自动选。
- 画布内自带触摸反馈环（`display.pointer_marker`），所以 **vmgui 前端下不需要**
  `dpctl pointer`；那个覆盖层只服务 GTK 窗口。
- 设置面板（「更多 → 设置…」，默认 `Ctrl+Alt+P`）：连接/显示/输入/日志分节，
  支持应用（热更新，只重建后端）、保存、从文件重载、恢复默认，以及 profile 多选切换。
  默认快捷键：全屏 `F11`、截图 `Ctrl+Alt+S`、重置视图 `Ctrl+Alt+R`、设置 `Ctrl+Alt+P`、
  退出 `Ctrl+Alt+Q`、捕获切换 `Ctrl+Alt+G`（都在 `input.hotkeys` / `input.grab.toggle_key`）。
- 自测：`py -3.13 -m unittest discover -s vmgui\tests -t .`（配置合并/坐标逆变换/缩放/键映射），
  `py -3.13 vmgui\tests\smoke_live.py --tap 280 85`（连真机取帧、按各模式离屏渲染成 PNG、注入点击）。

设计文档（配置 schema、适配层接口、模块划分、测试与验收）见 `.trae/documents/vmgui-design.md`。

> **QMP 同时只能有一个客户端，而且别去"探"它**：vmgui 在前台时会一直占着 4446，此时
> `dpctl tap` 会连不上（QEMU 的 socket chardev 只服务一个客户端）。更要紧的是**不要用
> `port_open` 之类的方式先探端口再断开**——QEMU 的 chardev 会把这种"连上又不读数就断开"
> 的连接一直挂在 backlog 里（backlog=1），此后所有连接都被拒（`WinError 10061`），
> 只能重启 QEMU 才恢复。所以 `dpctl` 不对 4446 做探测，vmgui 连上后就握住连接等问候语。
> `dpctl screenshot` 走监视器 4445，不受影响。

### 触控（鼠标点击 = 触摸笔屏）

app 从 `cfg.json` 的 `device.tp` 打开触摸设备；笔屏是**触摸 UI**（只认 `BTN_TOUCH` +
`ABS_X/ABS_Y`），而 QEMU 的 `virtio-tablet` 是**鼠标**（点击只发 `BTN_LEFT`、从不发
`BTN_TOUCH`）——直接点不会有反应。VM 里由包声明的 `touchbridge`（`touchbridge.pl`：用
uinput 造一个名为 `axs_ts_pen` 的假触摸屏、把 `BTN_LEFT` 翻成 `BTN_TOUCH`、并把 `cfg.json`
的 `tp` 指到它）桥接。原理与踩坑见
[docs/插件模型.md §6.4](docs/插件模型.md#64-触控桥)，个案见 `docs/疑难排查.md`。

操作：

```bat
python scripts\dpctl.py tap 280 85   :: 在笔屏坐标注入一次触摸（= 点一下）
python scripts\dpctl.py pointer      :: GTK 窗口下贴一层触摸反馈指针（up 开窗口时默认自动调出）
python scripts\dpctl.py nopointer    :: 中途收掉上面的反馈指针
```

- `dpctl tap` 坐标就是笔屏像素（560×170），不需旋转换算；
- 反馈指针完全在宿主侧（Tk 置顶、可点穿的覆盖层），**vmgui 前端自带反馈环**，用它时不必开
  `pointer`；排查用 `set DICTPEN_POINTER_DEBUG=1`；
- ⚠️ vmgui 在前台时一直占着 QMP，此时 `dpctl tap` 会连不上——先关掉 vmgui 窗口。

启动后自检（实机日志）：

```
[5.978] S00touchbridge: touch device ready
touchbridge: reading /dev/input/event1 (event size 16)
touchbridge: uinput device "axs_ts_pen" created
touchbridge: /dev/input/by-path/axs_ts -> /dev/input/event2
tsdevice:/dev/input/by-path/axs_ts open success!
winInputLoop open tp success!
```

点击时 app 会走到 `FBNativeWindow::inputTouchEvent`
（`grep -c inputTouchEvent` 可确认事件确实送到了 app）。

> `drmModeSetPlane failed (-13 EACCES)` 是 virtio-gpu 时代的历史问题（换 bochs-display 后
> 不再复现）；根因见 [docs/插件模型.md §6](docs/插件模型.md#6-插件点④显示与输入)，
> 排障见 `docs/疑难排查.md`。

### miniapp launcher 的开关
app 由 `S01systeminit` 的 `guardian_run /usr/bin/runDictPen` 拉起、开机即跑；initramfs 把
`S50launcher` 改名禁用（避免重拉/刷屏）。手工重启 app：`kill -9 <miniapp pid>`（guardian
会自动重启）或 `setsid /usr/bin/runDictPen`。个案见 `docs/疑难排查.md`。

## 激活与凭证恢复（license）

真机开箱要"激活"：app 拿**设备码**去有道服务器登记并换回一份凭证
（`/userdata/cfg/license`，174 字节）。VM 里设备码是空的——它存在设备数据分区的
`system_info.bin` 里，而那份文件由固件按硬件信息生成，QEMU 没有对应硬件：

```bat
python scripts\dpctl.py sh -c "strings /userdata/cfg/system_info.bin"
:: 02:00:00:00:00:00 / YDPA6-2 / 4.7.4 / OVERHEAD_A62_SKU_CHN_PRO / Linux ... / 560*170
:: 注意：只有 MAC/型号/版本/SKU，没有 ME 开头的序列号和 32 位 UUID
```

### 两条路线（两个开机钩子）

| 钩子 | 时机 | 行为 |
|------|------|------|
| `S00license` | app 启动**之前** | VM 没有设备码，直接写一份**开发凭证** |
| `S99license` | **网络起来之后**（dhcpcd 在 S21） | `system_info.bin` 里**有**设备码时，用 `scripts/restore_license.sh` 向服务器换真凭证 |

开发凭证为什么够用：app 只判断这个文件**是否存在**——原生侧
`YSystemInfoModulePrivate::licenseVerified()` 只取一个内部状态位、从不检查内容，
`YVerifyManagerPrivate::checkResponse()` 也只判断响应里某个字段是否为零。所以 174 字节
的固定占位串就能跳过整个激活页，干净开机直接进桌面。反过来，文件若在 app **已经**
显示激活页之后才出现，app 不会重新读取——这就是开发凭证必须放在 S00 的原因。

- 开关：`dpctl up --no-license`（等价内核 cmdline `DICTPEN_LICENSE=0`）**同时**关掉两个
  钩子，开机就会停在激活页；
- 手动跑真凭证路线：**`dpctl license`**（把仓库里的脚本推上去执行，输出能看到卡在哪一步）。

### 用真设备凭证激活

`scripts/restore_license.sh` 自动探测 `cfg`/`applog` 挂载点，从 `system_info.bin` 采集
SN/UUID/SKU/型号/版本/分辨率，判定 `client`，请求

```
https://fanyidan.youdao.com/fanyidanapi/activation/v2/verify
```

把返回的 `license` 校验后写回 `/userdata/cfg/license`，再重启 miniapp 验证。VM 里它会停在
`[!] 参数采集失败 sn= uuid=`——**缺的是一台已注册设备的序列号/UUID**。补齐方式二选一：

1. 把那台设备的 `system_info.bin` 覆盖到 VM 的 `/userdata/cfg/`（
   `dpctl put <你设备的>/system_info.bin /userdata/cfg/system_info.bin`，MAC 字段也建议
   一并改），删掉 `/userdata/cfg/license` 后重启——`S00license` 看到设备码会让路，
   `S99license` 去换真凭证；
2. 或者只把 SN 与 UUID 给我，我在钩子里把它们写进 `system_info.bin` 对应字段
   （400 字节定长结构，SN/UUID 槽位现在是全零）。

### 离线复现激活链路（`scripts/mitm-verify.py`）

想在不断网、不碰真机的前提下看激活请求长什么样，可让宿主冒充服务器：

1. guest `/etc/hosts` 把 `fanyidan.youdao.com`（含测试域名 `fanyiegg-test.youdao.com`）
   指向 `10.0.2.2`（slirp 网关 = 宿主机）；
2. 宿主起 `python scripts/mitm-verify.py`——用 `vm/mitm/cert.pem`（自签，SAN 覆盖两个
   域名，`vm/mitm/key.pem` 为私钥）监听 443，请求记到 `vm/mitm/requests.log`，
   回 `{"code":0,"license":"1:<172 字节>"}`。

> **注意**：`fanyidan.youdao.com` 同时承载笔的**词典查询**
> （`POST /fanyidanapi/dictsearch`），劫持期间查词会失败（界面显示"加载失败"）。
> 用完请把 hosts 里那行删掉。激活本身用不到这条链路（开发凭证已足够），
> 它只在需要观察/伪造激活请求时才有用。

## 换成其他机型（再打一个固件包）

做法：复制一份 `packs/a6pro/` 改成新名字，把 `pack.toml` 的 `[image]` 指到新机型的
镜像（镜像放进 `packs/<新名>/images/`），再 `python scripts\dpctl.py packs <新名>`
确认就位（详见"固件包与镜像选择"）。各机型在仓库里的文件名：

| 机型 | 代号 | 平台 | 仓库文件 | 框架现状 |
|------|------|------|----------|----------|
| A6pro（当前） | Pear_RV1106 | RV1106, Linux | `A6pro`: a6-2-pro-ro-rootfs.img + boot_b(DTB) | ✅ `rv1106-linux-erofs` |
| Hermes / Y09P / Y09 / Pear / A7pro | — | RV1106, Linux | 各 release 的 rootfs + boot | 🔶 同族，应只需改配置（未实测） |
| **X7pro** | YDPX7 | RV1106, Linux（aarch64+glibc） | `x7-system_a.img`（无 boot，面板参数取自镜像内 cfg.json） | ✅ `rv1106-linux-erofs`（fourcc/logo 补丁，见 packs/x7pro/README.md） |
| X5pro | — | RK3566, Android 系 | `X5pro`: x5-1-pro-ro-rootfs_b.img + boot_b | ⛔ 需要新的 runtime |
| X3s / X6plus | Cherry-3566 | RK3566 | `Cherry-3566-OTA-*`（需解包 OTA） | ⛔ 需要新的 runtime |
| X7（RK3562 代） | RK3562_Orange | RK3562 | `X7` 等 | ⛔ 需要新的 runtime（注意：`x7-system_a.img` 实为 RV1106 族，已按上行支持） |
| S6pro / S7pro | PineApple / Y08_CV1813 | Cvitek CV1826 | `S6pro` / `S7pro` | ⛔ 需要新的 runtime |

RV1106 同族与本环境同构（Linux + erofs），理论上只差一份 `pack.toml`；异构平台
（RK*、CV18xx，多为 OTA / 安卓 A/B 分区）需要各自的内核/模块/面板研究，对应
`runtimes/` 里再写一个 boot backend（框架核心不用动）。给异构平台建包后启动会明确
报 `该平台 runtime 未实现：'<名字>'`，**不会**拿 RV1106 的补丁去覆盖它。

## 固件包与镜像选择

**用哪台笔的固件 = 选哪一个包。** 一个包是一个目录，装着一台机器的全部会变的东西：

```
packs/a6pro/
  pack.toml            # 元数据：平台/镜像/面板/适配开关/vmgui profile/校验
  images/              # 大文件（固件镜像），不进版本库，自行下载后放这里
  patches/             # 该机型"会变的东西"：HAL 补丁、logo、触摸桥、cfg 覆盖表
  README.md            # 来源/版本/校验/已知问题
```

```toml
# packs/a6pro/pack.toml（节选）
[platform]  soc = "rv1106"   runtime = "rv1106-linux-erofs"
[image]     rootfs = "images/....img"   boot = "images/....img"
[display]   xres = 560  yres = 170  rotation = 0  bpp = 32
[adapt]     logo = "patches/logo.fb.gz"  battery = true  license = true  ap = true
[adapt.replace]
"patches/libfalcon.so" = "/usr/lib/libfalcon.so"     # 覆盖到 guest 的哪
[client]    profile = "pear-a6pro"                   # 交给 vmgui
```

```bat
python scripts\dpctl.py packs                 :: 有哪些包、镜像是否就位
python scripts\dpctl.py packs a6pro           :: 该包解析后的全部路径 + MD5 核对
python scripts\dpctl.py up --pack a6pro       :: 启动（只装一个包时可省略 --pack）
python scripts\dpctl.py pack export a6pro -o dist\a6pro.dpm   :: 整包带走
python scripts\dpctl.py pack import dist\a6pro.dpm --force     :: 整包装回来
```

`pack.toml` 里的路径一律**相对包目录**（写绝对路径也行）。`dpctl up` 启动前会校验
必需文件在不在、`[checks]` 的 MD5 对不对、该平台的 runtime 有没有，缺了直接告诉你
缺哪个，而不是让 QEMU 报一堆看不懂的错；`[client].profile` 会传给 vmgui。

**框架不含固件**：把 `packs/` 与 `downloads/` 整个移走，`dpctl packs` 只会说
"没有固件包"，`dpctl up` 同样友好退出，框架不会崩。

## 分发与重建

**包里只有代码。** 整个工作目录 4GB+，其中 99% 是可再获取的外部依赖和不该外传的东西：

| 不进包 | 为什么 | 怎么补 |
|--------|--------|--------|
| `packs/*/images/*.img`（固件 rootfs/boot） | 版权归网易有道，且 1.3GB | 按 `deps/manifest.toml` 的指引自行下载到包内 `images/`，MD5 用上游 release 公布的值 |
| `packs/*/patches/libfalcon.so` 等固件派生物 | 从固件里抠出来的二进制，同样归有道 | 同上（`dpctl pack export` 可整包搬，但要自己负责来源与授权） |
| `tools/`（QEMU 3GB、内核、platform-tools…） | 可再获取 | `bootstrap.py` 从镜像拉（MSYS2 / Debian tuna） |
| `tools/ssh/id_devvm`、`vm/mitm/*.pem` | **私钥/证书，绝不能外传** | `bootstrap.py` 在本机生成 |
| `vm/`（状态盘 512MB、initrd、日志、截屏） | 运行期产物 | 首次 `dpctl up` 自动生成 |

```bat
python scripts\package.py                     :: 看会打包哪些文件 + 自检（不写盘）
python scripts\package.py --out dist\dictpen-vm.zip
```

`package.py` 出的是**纯代码包**（不含包内适配二进制）。发布用的整树快照另外出，
它包含各包 `patches/` 下的适配文件，但仍不含镜像、`tools/`、`vm/` 与私钥：

```bat
git archive --format=zip -o dist\dictpen-vm-<版本>.zip HEAD
```

自检会挡住"固件/私钥/状态盘/大文件混进包"（固件包只带 `pack.toml`/`README.md`/
`cfg.json.overrides`/`touchbridge.pl` 这几个描述性文件）。收包的人：

```bat
python scripts\bootstrap.py --check   :: 只看缺什么，不动文件
python scripts\bootstrap.py           :: 能自动的自动拉/构建，缺固件的给出下载指引
python scripts\dpctl.py packs         :: 确认固件包/镜像就位
python scripts\dpctl.py up --pack a6pro
```

`bootstrap.py` 按 `deps/manifest.toml` 顺序做事：取 zstd → 取 MSYS2 包索引并装 QEMU →
解 busybox / 内核 deb → 取 arm64 依赖包（虚拟热点用）→ 生成 SSH 密钥并收紧权限 →
生成 mitm 自签证书 → `build-initramfs.py`。每步"已有就跳过"，可反复跑；
不想用脚本时，`deps/manifest.toml` 每项的 `where` 字段就是手工步骤。

## 已知问题排查

按现象的完整排查手册见 [`docs/疑难排查.md`](docs/疑难排查.md)（黑屏/花屏、触摸不响应、
联网误判、首启、激活、状态盘满、SSH/adb…）。

- 启动日志在 `vm/serial.log`（每次开机重写），QEMU 自身输出在 `vm/qemu.log`。

## 版权与免责

镜像版权归网易有道所有，仅供个人学习、技术研究使用，禁止商用；下载后请遵守
[dictpen-rootfs 仓库](https://github.com/lbdl0030/dictpen-rootfs)的免责声明。
