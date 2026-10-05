# 固件包（firmware packs）设计 —— 虚拟机与固件彻底分离

> 状态：**P1–P3 已落地**（P4 异构平台 runtime 另行排期）。
> 一句话：**虚拟机 = 通用框架（不含任何固件）；固件 = 可拆卸的包；启动时选包。**
>
> 落地情况（与本文的差异，均已实现）：
> - `[adapt]` 里原本的 `falcon = "patches/libfalcon.so"` 改成了通用的 `[adapt.replace]`
>   （键 = 包内路径，值 = guest 绝对路径）：框架只按表搬运文件，连 HAL 的名字都不出现，
>   这样才能满足 §7.2 的 grep 判据。
> - `[adapt].license` 由"指向包内脚本"改为布尔开关：脚本是框架自带的通用激活脚本
>   （所有有道设备共用一份），避免每个包重复一份。
> - 虚拟热点（hostapd/iptables）留在**框架**的通用 initramfs 里（是不含机型的 Debian
>   arm64 二进制），包只声明 `[adapt].ap` 是否启用。
> - `dpctl` 的 `machines` 子命令与 `machines/*.toml` 已删除（不做两套真相），
>   统一为 `packs` / `pack export|import` / `up --pack`。
> - `[checks]` 之外的 MD5 由 `dpctl packs <名>` 现算现比（不再有 `--image-dir` 之类的覆盖项）。

## 1. 目标与硬约束（来自需求）

| 约束 | 落地方式 |
|---|---|
| 虚拟机是框架 | 框架只做：包发现/校验、QEMU 组装启动、状态盘、传输/调试、显示前端（vmgui） |
| 固件做成可拆卸的包 | 一个包 = 一台机器的**全部会变的东西**（镜像 + 面板参数 + 补丁 + 平台适配） |
| 启动时选包 | `dpctl up --pack <name>` |
| 虚拟机不含固件 | 框架代码/资产里不出现镜像、HAL 补丁、logo、面板分辨率等 |
| 虚拟机内含"多种运行框架" | 框架内置可插拔 **runtime**（boot backend）：`rv1106-linux-erofs` 先做，`rockchip-ota`/`cv18xx`/`android-ab` 后续加；包声明用哪个 |
| **虚拟机不依赖任何某个固件模块** | 验收判据见 §7：删掉 `packs/` 与 `downloads/` 后框架仍能正常运行（只是告诉你"没有固件包"） |

## 2. 现状耦合清单（要搬进包的东西）

| 位置 | 现在写死的机型假设 | 归属 |
|---|---|---|
| [dpctl.py](file:///D:/download/vm/scripts/dpctl.py) | kernel/initrd 路径、`bochs-display,xres=560,yres=170`、`DICTPEN_*` cmdline | 包（runtime 参数） |
| [build-initramfs.py](file:///D:/download/vm/scripts/build-initramfs.py#L169-L184) | 把 **A6pro 打过补丁的 `libfalcon.so`**、A6pro logo、`touchbridge.pl`、`restore_license.sh` 烘进 `/patches/` | 包（`patches/` 目录） |
| [initramfs-init.sh](file:///D:/download/vm/scripts/initramfs-init.sh#L221-L229) | 无条件把 `cfg.json` 强改 `560x170 / direction 0 / bpp 32 / tp=axs_ts`；覆盖 `/usr/lib/libfalcon.so`；画 logo；伪造电量；装 touchbridge/热点/激活钩子 | 拆成"框架通用逻辑" + "包注入的 `pack.env`/补丁开关" |
| [machines/*.toml](file:///D:/download/vm/machines) | 只记了路径+分辨率+client profile | 升级为 `packs/<name>/pack.toml` |
| `images/initramfs.cpio.gz` | 一份按 A6pro 构建的 initramfs | 框架资产（**通用**），包不再改它 |
| `vm/state.img` | 所有机型共用一份 | 框架按包名分盘（`vm/state-<pack>.img`），防跨机型串味 |
| vmgui | 已与机型解耦（尺寸从帧自适应、QMP 通用） | 仅把 `client.profile` 交给包声明 |

> 实测反证（为什么必须做）：S7pro 走老路径时，框架的 initramfs 覆盖了它的 HAL、强改它的面板参数，而它自己的厂商模块 `insmod → invalid module format`，156s 后自重启循环。

## 3. 包格式

一个包 = 一个目录（便于人工检查/手动放镜像）：

```
packs/a6pro/
  pack.toml            # 元数据（下表）
  images/              # 大文件，不进版本库，从固件库下载后放这里
    a6-2-pro-ro-rootfs.img
    a6-2-pro-ro-boot_b.img
  patches/             # 可选：这台机器"会变的东西"
    libfalcon.so       # 该机型打过补丁的 HAL
    logo.fb.gz
    touchbridge.pl
    restore_license.sh
    cfg.json.overrides # 只覆盖列出的键，不再整文件 sed
  README.md            # 可选：来源/版本/校验/已知问题
```

`pack.toml` 草案：

```toml
name    = "a6pro"
note    = "A6pro / Pear_RV1106 / YoudaoOS 4.7.4"
version = "4.7.4"

[platform]                         # 决定框架用哪个 runtime
soc     = "rv1106"
runtime = "rv1106-linux-erofs"
kernel  = "kernel/vmlinuz-6.1.0-50-arm64"   # 相对包目录；留空=用框架默认
initrd  = "kernel/initramfs.cpio.gz"

[image]                            # 镜像与角色（路径相对包目录）
rootfs = "images/a6-2-pro-ro-rootfs.img"
boot   = "images/a6-2-pro-ro-boot_b.img"

[display]                          # 虚拟屏参数（框架不猜）
xres = 560
yres = 170
rotation = 0
bpp = 32

[adapt]                            # 平台适配开关：包声明了才生效
logo        = "patches/logo.fb.gz"          # 开机画到 /dev/fb0
touchbridge = "patches/touchbridge.pl"      # 鼠标 -> 触摸桥（uinput）
cfg_overlay = "patches/cfg.json.overrides"  # 只覆盖列出的 cfg.json 键
battery     = true                          # 伪造电量（避免"低电量关机"）
license     = true                          # 开机写开发凭证（框架自带脚本，包只开关）
ap          = true                          # wlan1 上的虚拟热点
hooks       = ["patches/xxx.sh"]            # 可选：包自带的 rcS 前置钩子

[adapt.replace]                    # 通用"把包里的文件覆盖到 guest 某处"
# 键 = 包内路径，值 = guest 绝对路径。框架只按表搬运，不解释文件是什么，
# 因此框架里连 libfalcon/HAL 这种名字都不会出现（满足 §7 的 grep 判据）。
"patches/libfalcon.so" = "/usr/lib/libfalcon.so"

[client]
profile = "pear-a6pro"                      # 交给 vmgui 的机型 profile

[checks]                                    # 可选完整性校验
"images/a6-2-pro-ro-rootfs.img" = { md5 = "0dfe781b76955f5dfe3d8aedde585c36" }
"images/a6-2-pro-ro-boot_b.img" = { md5 = "6cfa8d8ac37a057999b69a2c5098e201" }
```

分发：`dpctl pack export <name> -o a6pro.dpm` → 单文件 `tar.zst`；`dpctl pack import a6pro.dpm` 反向。
（镜像版权仍归有道，包内不含任何绕过版权的东西，仅是本机自用；是否外传由使用者决定。）

## 4. 框架侧（不出现任何机型名）

```
scripts/
  dpctl.py              # 只留通用：packs 发现/校验、QEMU 组装、状态盘、传输、调试
  packlib.py            # 包解析/校验/导入导出（新）
  runtimes/             # 「多种运行框架」= 可插拔 boot backend（新）
    __init__.py         # 注册表 + 接口
    rv1106_linux.py     # 第一个：erofs rootfs + boot DTB + 通用 initramfs
  build-initramfs.py    # 只构建「通用 initramfs」；包的补丁由 dpctl 合并
  initramfs-init.sh     # 通用：挂 rootfs(自动判 erofs/ext4) → overlay → state
                        #       → 应用 /pack（若存在）→ switch_root
```

runtime 接口（新增平台 = 加一个文件 + 一个包，不改框架核心）：

```python
class Runtime(Protocol):
    name: str
    def qemu_args(self, pack) -> list[str]:      # 组装该平台的 QEMU 命令行
    def initramfs_entries(self, pack) -> ...:    # 该平台要额外塞进 initrd 的东西
    def validate(self, pack) -> list[str]:       # 缺什么、报什么
```

**包怎么到 guest**：dpctl 把 `packs/<name>/patches/**` + 生成的 `pack.env`（面板参数、开关）
合并进**这一次启动用的 initrd**：`vm/initrd-<pack>.cpio.gz` = 框架通用 cpio + 包内文件（按两者哈希缓存，
只有换包/改包时才重建，约 1 秒）。于是：

- 框架的 `images/initramfs.cpio.gz` **只构建一次、永不含机型内容**；
- 换包 = 换个 initrd 文件，**"即时"**；
- guest 侧通用 init 读 `/pack/pack.env` 决定怎么改 `cfg.json`、装哪些钩子。

（备选：内核支持多段 initrd，可把包作为第二段直接追加；先按上面的"合并重建 + 缓存"做，稳且不依赖内核行为。）

## 5. 数据流（启动一台机器）

```
dpctl up --pack a6pro
  → packlib 读 packs/a6pro/pack.toml，校验 images/*（MD5）、patches/*
  → runtimes[pack.platform.runtime] 组装 QEMU 参数（显示用 pack.display）
  → 生成/复用 vm/initrd-a6pro.cpio.gz（框架通用 + 包 patches + pack.env）
  → QEMU 启动（-display none，状态盘 vm/state-a6pro.img）
  → 通用 initramfs：挂 erofs → overlay → state → 应用 /pack → switch_root
  → 起 vmgui（--profile 来自 pack.client.profile）
```

## 6. 迁移阶段（每阶段独立可验证，不破现状）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| P1 | 把 A6pro 现状抽成 `packs/a6pro/`（镜像、补丁、参数、profile 全搬进去），dpctl 加 `packs`/`up --pack` | 与现在**行为等价**：截图、输入、激活、电量、热点等既有验收项全过 |
| P2 | 拆 initramfs：通用逻辑 + 包 `patches/`；`cfg.json` 改"只覆盖列出的键" | 同 P1 的等价验收；且 `grep` 框架代码 0 机型名 |
| P3 | 把 S7pro 打成第二个包（即使其 runtime 未完整） | 框架不再污染它（`/usr/lib/libfalcon.so` 与 `cfg.json` 保持镜像原样）；报错是"该平台 runtime 未实现"，而不是莽撞覆盖 |
| P4 | （后续独立排期）异构平台 runtime：`rockchip-ota`（RK3566/3562/3326）、`cv18xx` | 各自平台能起；每个平台一轮研究（内核/模块/面板） |

## 7. 验收标准（"框架不依赖固件"的可执行判据）

1. **无包不崩**：移走 `packs/` 与 `downloads/` 后，`dpctl packs` 打印"没有固件包，请安装/新建一个"
   （并给出如何建包的提示），`dpctl up` 同样友好退出；框架不抛异常。
2. **框架零机型引用**：`grep -ri -e a6pro -e libfalcon -e pear -e 560 -e 170` 在
   `scripts/`（除 `runtimes/` 的平台参数说明）、`vmgui/` 中 0 命中（数值来自包）。
3. **一框架多包**：同一份框架分别 `up --pack a6pro` 与 `up --pack s7pro`，
   两者各自能启动、互相不污染（S7 的 HAL/cfg 保持镜像原样）。
4. **包可搬**：`pack export/import` 往返后 `dpctl packs` 能发现且校验通过。
5. 既有能力不回退：`dpctl` 的 sh/put/get/adb/screenshot/tap/license、vmgui 的显示与输入、
   引导钩子（电量/corefile/热点/激活）全部照旧。

## 8. 明确不在本次范围

- 异构平台（RK3566/RK3562/RK3326/CV18xx）的**实际可运行**：P4 才做，且各自需要厂商内核/模块/面板研究。
- 固件包的公开分发/版权处理：包格式支持导出，但镜像来源与授权由使用者负责。