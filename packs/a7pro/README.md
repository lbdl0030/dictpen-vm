# 固件包：a7pro

**A7pro / RV1106 / YoudaoOS**（a6pro 同族，`rv1106-linux-erofs` runtime）。
面板 640×172。

## 现状（2026-10-02 更新）

| 环节 | 状态 |
|------|------|
| 镜像就位 + MD5 校验 | ✅ |
| erofs 挂载 / overlay / 状态盘 | ✅ |
| 包适配声明（触摸桥 / cfg / 电量 / 热点 / 凭证） | ✅ |
| `libfalcon.so` 四像素格式补丁生效 | ✅ |
| 启动到桌面 UI | ✅ |
| **机型皮肤（决定 UI 设计尺寸）** | ✅ 由 `runDictPen.sh` 里的 `MOCK_THEME=theme-y09` 钉死，见「画面分辨率」一节 |
| 触控（`dpctl tap` → 页面跳转） | ✅ 落点精确，见「触控偏移」一节 |
| 开发凭证校验 | ⚠ 固件仍判失败，已用补丁绕过，不再影响使用，见「提示激活」一节 |

首屏截图 `vm/_a7pro-verify/fix-boot.png`（640×172，"查词翻译 / 语音助手 / 随身听"三张卡片，
完整不裁切），`dpctl tap 150 85` 能进"规范扫描"页 —— 显示与触控两条链都通了。

## 画面分辨率为什么"对不上"（2026-10-02 修复）

**症状**：桌面能起来、触控也灵，但画面像是被放大后又裁掉了右边和下半边——640×172 上
只能看到三张卡片里的一部分。

**取证**（都是实测）：

| 事实 | 值 |
|------|-----|
| 包声明 / 固件 `cfg.json` / DRM 模式 / `fb0` / falcon 窗口 | 全部 **640×172**（`stride 2560`、`bpp 32`） |
| boot DTB 面板时序 | `hactive=172 vactive=640`（=640×172 逻辑，270° 旋转，70Hz） |
| 把虚拟屏临时改成 960×266 起一次 | 首页**像素级完整** |
| 拿那张 960×266 的图裁出左上角 640×172，与 640×172 屏上的截图比 | **逐像素完全相同**（172/172 行一致，差异 0%） |

⇒ 不是缩放问题：应用把界面按 **960×266** 排版后 1:1 画进 640×172 画布，右侧/下侧直接被裁。

**根因链**（逆向 + 串口日志）：

```
/dev/vendor_storage 不存在
  └─ get_sku 读取失败（日志：vendor_storage read failed → vs_sku read from backup file failed, set default!）
       └─ 机型码（YDPA7-1）拿不到
            └─ YSettingManagerPrivate::themeName()（libyddal_business_core.so @0x10f858）
               查不到机型 → 落到常量 MOCK_THEME = "theme-melon"
                 └─ melon 那套 UI 是按 960×266 排版的 → 在 640×172 上被裁
```

对照表（都在 `libyddal_business_core.so` 的字符串池里）：`YDPA7-1 → theme-y09`、
`YDPA6-1 → theme-pear`、`YDP037/YDP021 → theme-apollo/exam` 等；`theme-melon` 是
YDP037 那一代的皮肤，也是"读不到机型"时的兜底常量。

**修复**（`patches/runDictPen.sh`，包内一行，不动任何二进制）：

```sh
export MOCK_THEME=theme-y09
```

固件自己留了后门：`themeName()` 第一件事是 `YEnvironment::value("MOCK_THEME", "")`，
**返回值非空就直接当主题名**，不再查表。把它设成本机对应的 `theme-y09`，等价于
"让笔认得出自己是 YDPA7-1"。

**验证**（干净首启 `up --pack a7pro --fresh`，从空状态盘全程走一遍）：

- `/userdata/miniapp/resources/env.json` 变成 `currentScreenWidth 640 / currentScreenHeight 172 / theme theme-y09`
  （修复前是 `960 / 266 / theme-melon`，且是应用自己算出来写回去的）；
- 应用日志：`dictpenHome===loadRootPage themeName= theme-y09`、
  `$falcon.env.custom.theme: theme-y09`（修复前都是 `theme-melon`）；
- `vm/_a7pro-verify/fix-boot.png`：三张卡片完整；与旧的 960×266 渲染左上角**像素差异 98%**
  （说明是真换了版式，不是裁切）；
- `dpctl tap 150 85` → 正常进「规范扫描」页（`fix-tap.png`）。

**这条修复只解决"UI 尺寸"**：机型本身仍然读不到，所以凡是要用机型的逻辑（`system_info.bin`
里那个错的 `800*254`、激活/凭证、按机型的云端配置）还是走 mock。要彻底修好，得让
`get_sku` 拿到 SKU —— 逆向结论（可直接照着做）：

- `get_sku` @`libydhal_devices.so` 0xaf78 调 `rkvendor_read(id=0xE, buf, len=0x400)`（调用点 0xb1b4），
  期望返回 **ASCII 串**，格式 `<前缀>_<机型>_SKU_<区域>_<硬件档>`，用 `_` 分隔；
- 机型词表：`RAE6/A61/A62/RAE6P/Y09/Y09P/Y11WG/Y06/Y24` → `Y09` 对应 serial 码 `0x1e`，
  机型表索引 `0x1e` 正好是 **`YDPA7-1`**（`libydhal_devices.so` 0x1b4d0）；
- 所以只要让 id `0xE` 返回例如 `OVERHEAD_Y09_SKU_CHN_ADV`，机型就会变成 YDPA7-1。
- 可走的两条路：① 替身 `libvendor_storage.so`（只导出 `rkvendor_read`/`rkvendor_write`，
  5 KB，需要 armhf 交叉编译或手工打补丁，经 `[adapt.replace]` 覆盖）；② 找到
  `youdao_load_sys_config("sku", ...)` 真正读的那个文件（**不是** `/userdata/cfg/sys_config.conf`，
  实测写进去会被应用抹掉）再补一份。

## 触控偏移（2026-10-02 修复）：触摸桥的面板尺寸是硬编码的

**症状**：点哪儿都有反应，但落点偏——越靠右偏得越多（右边缘大约偏 12%）。

**根因**：`patches/touchbridge.pl` 把面板尺寸写死成 **560×170**：

```perl
PANEL_X => 560,     # ← 这是 a6pro 的面板，不是 a7pro 的
PANEL_Y => 170,
```

桥用这个范围声明 uinput 触摸设备、并把平板的 0..32767 重标到它；而 UI 按 640×172
换算坐标，于是 X 被整体压缩 560/640 ≈ 12.5%。三份 `touchbridge.pl`
（a6pro / a7pro / x5pro）原本**是同一个文件**（同一哈希），所以 a6pro 的常量被抄到
了所有机型上——这类"共享脚本里的机型常量"正是框架设计上要避免的东西。

**验证**（`TOUCHBRIDGE_DEBUG=1` 看桥实际转发出去的坐标）：

| `dpctl tap` | 修复前转发 | 修复后转发 |
|-------------|-----------|-----------|
| (500, 100)  | ~(437, 99) | **(499, 99)**（1 像素是取整） |
| (60, 30)    | (52, 30)   | **(60, 30)** |

**修复**：尺寸改成"读"而不是"写死"，优先级为 第 3 个命令行参数 `WxH` >
环境变量 `TOUCHBRIDGE_PANEL` > guest 的 `/etc/miniapp/resources/cfg.json` 里的
`screen.width/height`（这正是 UI 用的坐标空间，框架本来就认这个文件，见
`initramfs-init.sh` 的 cfg_overlay 部分）；全都取不到才回退 560×170 并打警告。
启动日志会写明来源：

```
touchbridge: panel 640x172 (from /etc/miniapp/resources/cfg.json)
touchbridge: source x 0..32767 y 0..32767 -> panel 639 x 171
```

三份 `touchbridge.pl` 已同步为同一份。a6pro 的 cfg.json 是 560×170，读出来与原常量
一致，所以它的行为不变（`x5pro` 同理，尚未实测）。

## 用一段时间后提示激活、笔用不了（2026-10-02 修复）

**症状**：桌面正常、能用几分钟，之后跳到「验证引导」页，笔就没法用了。

**根因**：JS 侧的 `global.verifyLicense` 会**重新校验**许可，失败就自己导航到
验证引导应用。历史日志（那次开机约 5 分钟后）：

```
console(8080222437664451): ------- global.verifyLicense  false redo verify
[YConfiger] modifyDeviceProvisioned, key: device_provisioned, value: "false"
init->89 global dynamic_load_jsapi .../8080282888534774/a/libs/libjsapi_verify_1286195000.so
console: onCurrentAppIdChanged,appid:8080282888534774     ← 前台被切到「验证引导」
```

`verifyLicense` 为什么是 false，见上面「画面分辨率」一节末尾和下面「开发凭证校验」
一节：a7pro 的凭证是 RSA 签名 + 设备绑定校验，设备码又要从 vendor storage 取，
VM 里没有这个设备。

**修复**：`patches/libydhal_devices.so` + `[adapt.replace]`，让 `yd_hal_verify_license`
恒真——只改一个 32 位字：

| 项 | 值 |
|----|-----|
| 文件偏移 | `0x8f84` |
| 原始 | `03 00 a0 e1`（`mov r0, r3`） |
| 补丁 | `01 00 a0 e3`（`mov r0, #1`） |
| 生成 | `python vm\_a7pro-verify\patch-license-bypass.py --patch hal <原始.so> <输出.so>` |
| 原始来源 | `dpctl get /usr/lib/libydhal_devices.so <原始.so>`，md5 应为 `1324f23135e9a4e22fab3566ace3ae28` |

**固件版本一变必须重新生成**（脚本会先核对原字，对不上就拒绝改写）。
这是**开发用绕过**，不是"让凭证通过校验"——真机上这一步是厂商的许可校验。

**验证**（干净启动后盯够 6 分钟）：

- `grep -c -E 'redo verify|verifyLicense' <本次 DictPen_*.log>` → **0**；
- 前台 app 只切到 `8080252464522508 → 8080222437664451`(HOME)，**没有**切到
  8080282888534774（修复前约 5 分钟必切）；
- 服务器那条 `activation/sys/lockStatus` 仍会因设备码为空返回 400，但应用对它的
  反应是 `{"checkSuccess":false,"isLocked":false}` —— **不锁机**，可以忽略。

> 想不靠补丁彻底解决，只能让 `get_sku` 拿到真设备码（伪造 vendor storage，见上），
> 或者用**已注册设备**的 `system_info.bin`（`dpctl put <你设备的> .../system_info.bin
> /userdata/cfg/`）换真凭证。

## 黑屏的真正原因（2026-10-01 修复）：runDictPen 替代脚本漏了库搜索路径

**症状**：链路全通、`libfalcon` 建出正确的 640×172 dumb buffer（`pitch=2560`、
`bpp=32 direction=0`），但 QEMU 侧帧缓冲 100% 纯黑。

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
python scripts\dpctl.py sh -c "grep -c 'launchApp fail' /userdata/applog/DictPen_*.log"
```

**教训**：替代厂商启动脚本时，"与厂商原版一致"的那部分要逐字核对，尤其是环境变量；
改这一行必须重新验证 UI，不能只看进程起来了。

> ⚠ `packs/x5pro/patches/runDictPen.sh` 有同样的漏项（同一份脚本抄的），
> 尚未在 x5pro 上验证；动 x5pro 之前先按上面的方法确认。

## 两个已修复的问题

### 1. 状态盘被填满，overlay 退化成只读

a7pro 的 `/userdata` 占用比 a6pro 大得多。512 MiB 的状态盘被填满后：

```
overlayfs: failed to create directory /state/work/work (errno: 28); mounting read-only
/init: line 192: can't create /mnt/etc/init.d/S00license: Read-only file system
```

`errno 28` 是 ENOSPC。overlay 的 upper 层本身就是 `/state/upper`，跟盘上其他
数据在同一块 ext4 上，盘满时连 workdir 都建不出来，于是整个 overlay 只能
只读挂载，所有 S00 钩子（凭证、触摸桥、电量、热点、`replace`）全部写不进去。
guest 里所有服务反复重启并报 `No space left on device`。

**为什么原来自带的清理机制救不了**：`S00corelog` 是装到 guest 里的，而装它的
那一步就失败在只读 overlay 上——鸡生蛋问题。所以清理必须在 `/init` 里、
overlay 建 upper **之前**做，直接对 `/state` 干活。

**修复**（`scripts/initramfs-init.sh`）：
- 挂载状态盘后、创建 `/state/upper` 前，先清掉两块已知浪费：
  - `/userdisk/swap/swap.file` **257 MiB**——厂商的 swapfile。VM 有 2 GiB 内存，
    `/proc/swaps` 里 `Used = 0`，纯浪费。`/init` 阶段它还没被 `mkswap` 激活
    （那是 vendor init 在 `switch_root` 之后才做的事），所以可以直接删。
  - 按数量封顶 `*/applog`、`userdisk/corefile`（默认留 500 个）。
- **特意不动** `userdisk/secondary`：它不是 `/userdata/miniapp` 的重复副本，
  两者 inode 不同（`65040:92` vs `65040:2073`），miniapp 会把二级应用直接解压
  进 `/userdisk/secondary/miniapp`。早期版本误删过它，只是下次启动又被重建，
  白跑一遍。

### 2. 状态盘大小是硬编码的

`dpctl.py` 原来写死 `512M`。现在改成 `[state].size_mb` 可声明，留空则用默认
512。本包声明 `1024`。

## 开发凭证校验为什么过不了（与黑屏无关）

> 2026-10-01 晚更正：先前把黑屏归因于"激活校验挡住"，**这是错的**。
> 实测把 `yd_hal_verify_license` 改成恒真（`vm/_a7pro-verify/patch-license-bypass.py`），
> 校验失败的日志消失了，但屏幕**依然全黑** —— 黑屏是 `LD_LIBRARY_PATH` 那个
> 问题造成的（见上文），与激活无关。补丁已撤销。
> 本节的事实仍然成立，但它们影响的是"能不能激活"，不是"能不能出画面"。

清完盘、overlay 可写之后，凭证校验仍然失败。根因已经定位到具体的
一行代码，但需要一台真机的 `system_info.bin` 才能通。

### 失败点

a7pro 固件（`4.13.0`，比 a6pro 的 `4.7.4` 新很多）在启动路径里多做了一步
**真实的凭证校验**，而框架写入的开发凭证通不过：

```
10-01 18:59:19.565495 ERROR [YStartupModule] [initSecondaryStartupSignal]
    YStartupModulePrivate::initSecondaryStartupSignal license verify failed,
    setMiniappCreateFinishFlag
```

紧跟着就是 `get_sn failed, sn len is 0` / `get_uuid error` / `vendor_storage
open fail`。

**框架里那条假设在这里不成立了。** `initramfs-init.sh` 的注释写的是"a6pro 的
应用只测文件存不存在"。a7pro 不是：它真的解析凭证并拿设备码去对，而设备码要从
`vendor storage` 取，VM 里没有这个设备，所以判定失败。

失败时 `initSecondaryStartupSignal` 会调 `setMiniappCreateFinishFlag()`。
**注意：这并不决定哪个 app 在前台** —— 实测在"校验失败"和"校验恒真"两种情况下，
启动的 app 序列完全一样（都是 `8080252464522508` → `8080282888534774` →
`8080222501178405`）；黑屏是 `LD_LIBRARY_PATH` 那个 bug 造成的（见上文）。
修好之后前台 app 变成 `8080222437664451`(HOME)，与 a6pro 一致。

校验链（已用 `.rel.plt` 逐级确认）：

```
initSecondaryStartupSignal                (libyddal_business_core @0xeeed8)
  -> YDALAPI::YSystemInfoModule::verifyLicense          (@0x118310)
  -> YSystemInfoModulePrivate::verifyLicense            (@0x11809c)
  -> YADAPTERAPI::YSystemInfoAdapter::verifyLicense     (libyddal_adapter_core)
  -> yd_hal_activator_module_new / yd_hal_verify_license (libydhal_devices @0x8ea8)
       读 /userdata/cfg/license -> 内部 0x8b44 校验（内部还会建 store module，
       即 vendor storage）
```

要临时让校验通过（只为排查用）：`vm/_a7pro-verify/patch-license-bypass.py --patch hal`
把 `yd_hal_verify_license` 的 `mov r0, r3` 改成 `mov r0, #1`，再经
`[adapt].replace` 覆盖 `/usr/lib/libydhal_devices.so`。**它不会让画面出来**，
只影响激活状态。

### 为什么 `dpctl license` 也走不通

`restore_license.sh` 需要 `system_info.bin` 里有 `ME` 开头的 SN 和 32 位 UUID。
VM 里那个文件是应用自己生成的，只有 MAC/版本/分辨率，没有设备码：

```
strings /userdata/cfg/system_info.bin
02:00:00:00:00:00
4.13.0
Linux 6.1.0-50-arm64
800*254
```

脚本已经在 `get_client()` 里映射了 a7pro 的机型（`YDPA7-1 → y09`、
`YDPA7-2 → y09p`），所以**只缺设备码本身**。

### 框架提供的写入钩子（实测**不足以**让 a7pro 通过校验）

框架新增了 `scripts/write_system_info.sh` 和开机钩子 `S00sysinfo`，把
`/proc/cmdline` 上的 `DICTPEN_SN` / `DICTPEN_UUID`（或 `/patches/sysinfo.creds`
里的 `SN=` / `UUID=`）写进 `/userdata/cfg/system_info.bin` 的对应槽位。
写完 `S00license` 看到设备码会自动退让，`S99license` 就能去服务器换真凭证。

**钩子本身工作正常**（串口会打 `S00sysinfo: rc=0 已写入 …`，自检四个字段都能抽出来），
但 a7pro 上它救不了激活：

- 应用每次启动都会把整份 400 字节记录**无条件写回**，把设备码抹平（已实测：
  预置合法 SN/UUID 后重启，文件里 SN/UUID 又变空、`screen` 被改回厂商的 `800*254`）；
- a7pro 的 UUID 来源根本不是这个文件：`get_uuid`（`libydhal_devices.so` @0xdfec）
  跑的是 `cat /proc/cpuinfo |grep Serial|awk '{print $3}'` 和读
  `/sys/class/block/%s/device/cid`，VM 里两者都不存在。

也就是说：不解决"设备码从哪来"，写 `system_info.bin` 是白写。

两种用法：

```bat
:: 开机就带上凭证
python scripts\dpctl.py up --pack a7pro ^
    --sn ME8123456789012345 --uuid 0123456789abcdef0123456789abcdef ^
    --model YDPA7-1 --sku OVERHEAD_A71_SKU_CHN_PRO

:: 或者在已经跑着的 VM 里写
python scripts\dpctl.py sysinfo --sn ME8123456789012345 ^
    --uuid 0123456789abcdef0123456789abcdef --model YDPA7-1
```

写完之后：

```bat
python scripts\dpctl.py license     :: 服务器换真凭证
:: 或者重启让钩子生效（没有 restart 子命令，用 down + up）
python scripts\dpctl.py down
python scripts\dpctl.py up --pack a7pro
```

### `system_info.bin` 的 400 字节布局

（a7pro 4.13.0 实测，SN/UUID/MODEL/SKU 槽位原来全是零。）

| 偏移 | 长度 | 字段 | 例子 |
|------|------|------|------|
| 0x00 | 4  | magic | `1` |
| 0x04 | 17 | MAC（含 NUL 结尾） | `02:00:00:00:00:00` |
| 0x16 | 41 | SN | `ME8123456789012345` |
| 0x3B | 12 | 固件版本 | `4.13.0` |
| 0x48 | 18 | 未用 | 含两个 u32（`0x51=8`, `0x55=16`，推测是内存/闪存大小） |
| 0x59 | 24 | MODEL | `YDPA7-1` |
| 0x71 | 61 | SKU | `OVERHEAD_A71_SKU_CHN_PRO` |
| 0xB6 | 50 | 内核版本 | `Linux 6.1.0-50-arm64` |
| 0xCC | 42 | 保留 | 全零 |
| 0xF6 | 14 | 屏幕 | `800*254`（真实面板是 640×172，此值不准，但 vendor 不用它） |
| 0x100 | 32 | UUID | `0123456789abcdef0123456789abcdef` |
| 0x11F | 129 | 尾部保留 | 全零 |

注意几点：

- **各字段之间的 NUL 是前一个槽位的尾巴**，不是后一个的开头。SN 槽
  `0x15..0x3A` 里，`0x15` 是 MAC 的 NUL，SN 真正能写的起点是 `0x16`。
  清理 SN 槽时不能碰掉 `0x15`，否则 `strings` 会把 MAC 和 SN 连成一条记录，
  `restore_license.sh` 的 `grep -oE '^ME[0-9]+'` 就抽不到 SN。
- **UUID 是 32 字节占满槽位**（`get_uuid` 里 `mov r2, #0x20`），不需要 NUL
  结尾。清理时只能清 32 字节，不能清那 160 字节的"UUID+保留"整段——否则
  会把文件写到 416 字节。
- **SN 只允许数字**（`[0-9]+`），vendor 的提取正则就是 `^ME[0-9]+`。
  带字母的 SN 会被截断到第一个字母为止。
- strides 不均匀（42 / 48 / 123 / 64），不是简单的定步长数组。

### 三条推进路径

> 先说结论：**UI 已经不需要激活就能出来**（见「黑屏的真正原因」）。下面这些
> 只影响"笔认为自己激没激活"（激活页、部分云功能），不再挡开机桌面。
> 而且要留意：写 `system_info.bin` 这条路**结构性无效** —— 应用每次启动都会
> 把整份 400 字节记录无条件写回去（`libyddal_adapter_core.so` 0x4b84c 调
> `file_utils::write_to_file(record, 400, path)`，三个 getter 失败只打日志、
> 不阻止写入），预置的内容必被抹掉；a7pro 的 UUID 也不是从该文件取的
> （`get_uuid` 读 `/proc/cpuinfo` 的 `Serial` 和 `/sys/class/block/*/device/cid`，
> 两者在 VM 里都不存在）。详见 `vm/_a7pro-verify/FINDINGS.md`。

1. **拿真机的 SN + UUID**（唯一能走真凭证的路）：
   ```bat
   python scripts\dpctl.py up --pack a7pro --fresh ^
       --sn <你的ME…> --uuid <32位hex> --model YDPA7-1
   ```
   或者先开机再用 `dpctl sysinfo --sn … --uuid …`。写完 `dpctl license`
   换真凭证。

2. **让开发凭证通过校验**：需要搞清楚 `licenseVerified()` 在 a7pro 里到底校验
   什么（长度？签名？SN 绑定？），可能要构造一份"看起来合法"的凭证。
   入口在 `libyddal_base_core.so` / `libyddal_business.so`。

3. **绕过 `验证引导` 直接进主页**：这条已经不需要了（UI 与激活无关），
   但如果要让笔"认为已激活"，最靠底的一刀是让 `yd_hal_verify_license` 恒真
   （见上文的 `patch-license-bypass.py`）。

排查入口：串口日志搜 `license verify failed`、`get_sn`、`get_uuid`、
`验证引导`、`initSecondaryStartupSignal`。应用清单在
`/userdisk/secondary/miniapp/data/mini_app/pkg/`。

## 复现命令

```bat
python scripts\dpctl.py packs a7pro
python scripts\dpctl.py up --pack a7pro --fresh     :: 干净首启（约 5 分钟装应用）
python scripts\dpctl.py sh -c "df -h /state"        :: 应显示 ~974M，余量充足
python scripts\dpctl.py screenshot --out vm\s.png
```

判断修复是否生效看串口日志：
- `### state: reclaim ...` 后面应出现 `state: removed vendor swapfile`；
- **不应**再出现 `overlayfs: failed to create directory /state/work/work`；
- **不应**再出现 `Read-only file system`（之前有 20 多处）。

### 验证 SN/UUID 写入

```bat
python scripts\dpctl.py sh -c ^
    "strings /userdata/cfg/system_info.bin ^&^& ^
     grep -oE '^ME[0-9]+' /userdata/cfg/system_info.bin ^&^& ^
     grep -aoE '[a-f0-9]{32}' /userdata/cfg/system_info.bin"
```

`write_system_info.sh` 自己跑完会打印一份校验（`SN extract: …` / `UUID extract: …`），
四个字段都能抽出来说明槽位对上了。

## 实体键（2026-10-20）

- **能力**：Home，码 **102**（`Y09_RV1106_V0.home_key_id = gpio-keys`）；本包固件没有音量键项。
- **做法**：`patches/vm-keys.pl` + `vm-keys-apply` + `vm-keys.env` + `vm-keys.sh` 钩子；
  板型串 `Rockchip RV1106 for Y09 V10 Board`。首次 app 启动前换 model 会黑屏，故由
  `S50vm-keys` 等首 app 起来后 apply、停掉 runDictPen 与 app、直接启动一个 miniapp。
- **验证**：冷启动后选年级页 → 桌面（`vm/_a7_final_after.png`）；单实例；控制台只亮 Home。
- 快速启动可选：`dpctl fast save --pack a7pro`（无音频，见 README 与台账 Q93）。

## 显示颜色：R/B 颠倒（Q111，2026-10-21）

与 a6pro 同源：RV1106 falcon 的输出通道序与 bochs 扫描线相反。原截图里「查词翻译」是
黄/橙卡、Aa 图标橙；做 R/B 互换后变成蓝卡 + 蓝 Aa，与同族 UI 一致
（`vm/_a7_color_swapRB.png` 是离线对照）。修法同样是 `[display] swap_rb = true`，
由 vmgui/dpctl 在宿主呈现层交换，不动客机帧。单测 `TestDisplaySwapRB`。
