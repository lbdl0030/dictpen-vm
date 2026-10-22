# 固件包：x7

**X7 / YDPX7 / RV1106 / YoudaoOS（aarch64 + glibc）**，`rv1106-linux-erofs` runtime。
面板 960×266。只有一份 `x7-system_a.img`（2.5 GiB erofs），**没有 boot 镜像**——框架
也确实不需要它（runtime 从不读 boot，`packlib` 只把 rootfs 当必需文件）。

> 镜像就在包内 `images/x7-system_a.img`（路径相对包目录）；`D:\download\x7-system_a.img`
> 是最初的下载件，包不依赖它。

## 现状（2026-10-03 更新）

| 环节 | 状态 |
|------|------|
| 镜像格式（erofs 未压缩）/ 状态盘 / overlay / switch_root | ✅ |
| aarch64 + glibc 用户态（与框架 arm64 内核原生匹配） | ✅ |
| 框架适配钩子（cfg 覆盖 / 触摸桥 / 热点 / 电量 / 凭证） | ✅ |
| **显示：DRM plane 搜索 + CREATE_DUMB + MAP_DUMB + AddFB2** | ✅ 两处 fourcc 补丁，见下 |
| **开机 logo** | ✅ 重制的裸帧 logo，见下 |
| **启动到桌面 UI** | ✅ `theme-olive` 皮肤原生适配 960×266，四张卡片完整不裁切 |
| 触控（`dpctl tap` → 页面跳转） | ✅ |
| miniapp 稳定运行（无崩溃循环，`MiniappCreateFinishFlag` 出现） | ✅ |

首屏验证截图：`vm/x7-screen2.png`（有道小P / 查词翻译 / AI拍照 / 随身听），
`dpctl tap 510 133` 能进查词应用（联网提示属 headless 未连 Wi-Fi 的正常现象）。

## 修复一：`Cannot find plane`（plane 搜索 fourcc）

falcon 的 DRM 初始化用 `drmModeGetPlaneResources()` 逐 plane 找「possible_crtcs 匹配 +
格式匹配」的平面。期望格式由 bpp 决定：

```
0x2ca214: ldr  w0, [sp, #0x84]          ; bpp（cfg.json，已归一化为 32）
0x2ca218: mov  w1, #0x5241
0x2ca21c: movk w1, #0x3432, lsl #16     ; w1 = 0x34325241 = AR24（ARGB8888）
0x2ca220: mov  w25, #0x4752
0x2ca224: movk w25, #0x3631, lsl #16    ; w25 = 0x36314752 = RG16（RGB565）
0x2ca228: cmp  w0, #0x10
0x2ca22c: csel w25, w25, w1, eq         ; bpp==16 ? RG16 : AR24
```

bochs-drm 的平面只提供 XR24（XRGB8888），AR24 永远匹配不上 → 循环走完打
`Cannot find plane` → `exit(7)`。日志特征：`plane(33)` 之后**没有**
`drmModeGetPlane failed: %s` ——说明 GetPlane 成功、失败在格式匹配（由此排除了
"GetPlaneResources 恒 NULL"的早期猜测，废弃了对应的分支补丁）。

**修复**：`0x2ca218` 一条指令 `mov w1, #0x5241 → #0x5258`（AR24 → XR24；两者高半字
同为 0x3432，**不要**动 movk——把 movk 改成 0x4247 会拼出非法 fourcc 0x42475258，
正是 a6pro 勘误里踩过的坑）。

## 修复二：`drmModeAddFB2() ret=-22`（AddFB2 fourcc）

plane 搜索修好后链路推进到 AddFB2，返回 EINVAL 且**内核一条 debug 都不打**。原因在
6.1 内核 bochs 驱动（`drivers/gpu/drm/tiny/bochs.c`）自己的 fb_create 钩子：

```c
static struct drm_framebuffer *
bochs_gem_fb_create(...) {
	if (fmt != DRM_FORMAT_XRGB8888 && fmt != DRM_FORMAT_BGRX8888)
		return ERR_PTR(-EINVAL);      /* 静默拒绝，无日志 */
	return drm_gem_fb_create(dev, file, mode_cmd);
}
```

falcon 的 AddFB2 调用点（`0x2c7090..0x2c70a0`）用同样的 mov/movk/csel 构造
AR24/RG16。**修复**：`0x2c7098` 一条指令 `mov w0, #0x5241 → #0x5258`（AR24 → XR24）。

两处补丁由本机脚本 `vm/_x7-probe/patch-fourcc.py` 生成（对原始 `libfalcon.so` 打，产出
`packs/x7/patches/libfalcon.so`，经 `[adapt.replace]` 覆盖 guest 的
`/usr/lib/libfalcon.so`；全文件只差这 2 条指令）。修好后的完整链路：

```
falcon_entry → count_connectors=1 → plane(33) → bpp=32 direction=0
→ create_drm_fb: w=960 h=266
→ CREATE_DUMB ret=0 size=1024000 pitch=3840
→ MAP_DUMB ret=0
→ drmModeAddFB2() ret=0
→ Show logo:/etc/miniapp/resources/youdao_startup_logo.bmp
```

## 修复三：logo memcpy 越界写 → SIGSEGV（最隐蔽的一个）

AddFB2 通了之后 miniapp 仍然每 ~40s 崩溃循环一次（`before miniapp` 反复出现），
stdout 只有一行 `Aborted`、没有任何栈信息——abort/segfault 不冲刷 stdio 缓冲，
falcon 的 DRM 日志全部丢失。定位过程（用镜像自带的 gdb，注意 guardian 会在 ~40s
后重新拉起 runDictPen 和实验进程抢 DRM master，做单实例实验前先临时
`mv /usr/bin/runDictPen{,.bak}`）：

- 崩溃点在 `greenui::NativeWindow::create()` 内的一个 `memcpy`（create()+2992）；
- 该处把「刚读入内存的 logo 文件」**整个原样 memcpy 进 dumb framebuffer**；
- 厂商 logo 是**裸帧数据，没有文件头**：1,843,200 字节 = **480×960@32bpp**（真机
  竖屏面板；cfg.json 的 `yoffset=107` = (480−266)/2，UI 带 960×266 旋转后居中，
  印证真机面板就是 480×960）；
- VM 的 framebuffer 是 960×266@32bpp = 1,021,440 字节（dumb buffer 页对齐到
  1,024,000）→ 1.84MB 拷进 1MB → 越界写 → SIGSEGV。

崩溃点"漂移"（有时在 memcpy、有时在 create 入口）、偶发通过，都是堆被冲垮后的
次要现象，不是独立 bug。

**修复**：按 VM 面板重制裸帧 logo（`patches/logo-startup.bin` 与内容相同的
`patches/logo-ota.bin`，960×266 BGRX，恰好 1,021,440 字节）：把厂商 logo 转正
（源数据行列互置 + 镜像，等价 `R(x,y)=P(479-x,y)`）后取上侧 logo 居中。
经 `[adapt.replace]` 同时顶替

- `/etc/miniapp/resources/youdao_startup_logo.bmp`
- `/etc/miniapp/resources/youdao_ota_logo.bmp`（内容相同，防 OTA 路径触发同一问题）

固件版本一变（logo 尺寸/面板变化）需要重新生成；生成脚本与取证图在
`vm/_x7-probe/`（`youdao_startup_logo.bmp` 为从镜像里拉出的原件）。该目录已于 2026-10-21 清理，补丁产物在本包 `patches/` 内。

## 机型身份与主题

- 机型表有 `YDPX7-1/-2/...`；源码分支串 `rv1106_develop`、pcba `Y11/Y06/Y24_RV1106_V0`
  → RV1106 族，与 a6pro/a7pro 同 runtime；
- 用户态是 aarch64 **glibc**（a6pro/a7pro 是 armhf uClibc），厂商内核模块自然全部
  `version magic '5.10.160'` 不匹配（预期，和 a6pro 一样）；
- 读不到机型时回退 `theme-melon`，但 x7 实际落到 **`theme-olive`**，且 olive 的版式
  与 960×266 **天然吻合**（日志 `dictpenHome===> $falcon.env.custom.theme: theme-olive`），
  所以**不需要** a7pro 那样的 `MOCK_THEME` 钉子。

## 略过 boot：面板参数从哪来

没有 boot 镜像 → 没有真机 DTB。面板尺寸/旋转取自镜像自带的
`/etc/miniapp/resources/cfg.json`（erofs 未压缩，可直接在镜像里读）：

```json
"screen": { "width": 960, "height": 266, "direction": 270, "tp_direction": 270, ... }
```

包声明 `[display] 960x266`，`patches/cfg.json.overrides` 把 direction/偏移全部归零
（虚拟屏 1:1 无旋转）、`tp` 指向触摸桥节点 `/dev/input/by-path/axs_ts`。

## 复现

```bat
python scripts\dpctl.py packs x7
python scripts\dpctl.py up --pack x7 --display none
python scripts\dpctl.py wait --timeout 240
python scripts\dpctl.py screenshot --out vm\x7.png
python scripts\dpctl.py tap 510 133          :: 点"查词翻译"
```

排查关键词（`/userdata/applog/DictPen_*.log`，每次启动被 runDictPen 重命名重建）：
`falcon_entry` / `plane(` / `CREATE_DUMB` / `drmModeAddFB2() ret=` / `Show logo` /
`loadRootPage themeName=`。内核 DRM 调试可运行时开：
`echo 0xf > /sys/module/drm/parameters/debug`。

## 顺带确认

- 厂商 `/usr/bin/runDictPen` 自带完整 `LD_LIBRARY_PATH`（jsapis + messageknife），
  没有 a7pro 那个漏项；
- guest 自带 `/usr/bin/gdb`、`gdbserver`（调试利器）；
- 镜像里 miniapp 日志走 zlog：WARN+ 进 syslog（`YD_PEN_APP.log`），全量 stdout 由
  runDictPen 重定向到 `DictPen_*.log`。

## 显示与触控复核（2026-10-20，人点名「显示右裁＋触控有问题」）

- **四个口径全是 960×266**：`fb0/virtual_size`＝`960,266` / `U:960x266p-0`、QEMU **不裁剪**整帧＝
  960×266、`pack.toml`＝960/266、`cfg.json` 的 `screen`＝`width 960 / height 266 / direction 0 /
  yoffset 0` → **没有字盒**（与 x6pro 不同，见 `packs/x6pro/DEVLOG.md` §7.10）。
- **"右边被切"是首页自己的 coverflow 卡片行**：4 张卡（有道小P／查词翻译／AI拍照／随身听），
  中间那颗被放大（当前焦点），第 4 张超出 960 被切 —— 行宽 > 屏宽是固件的排版，不是我们裁的
  （`vm/_x7_home2.png`）。
- **坐标投递是准的**（这条是重点）：抓桥发布的 `axs_ts`，`tap 300 60` → app 收到
  `ABS_X=300`、`ABS_Y=60`、`BTN_TOUCH=1`、`BTN_TOUCH=0` —— 一个像素不差（`panel 960x266`、
  `source 0..32767 -> 959 x 265` 见 `/tmp/touchbridge.log`）。
  ⚠ **抓包必须 `bs=24`**：这台 `struct input_event` 是 **24 字节**（桥日志明写
  `reading /dev/input/event1 (event size 24)`），用 `bs=16` 会被 evdev 直接 `EINVAL`、
  文件 0 字节 —— 我 2026-10-20 就这么把"工具读不到"读成过"桥没转发坐标"。
- **一处待查（别当已结）**：`tap 730 130`（对着 AI拍照）开出来的是「开始激活」页
  （语音助手/有道小P 那一族）。既然坐标证明是准的，偏移就落在 **app 自己的命中区 vs 画出来的位置**
  （coverflow 可能是"先聚焦再打开"）。要定它算不算缺陷，得画一张"点哪儿 → 开哪个"的对照表。
- **顺带一条与"卡/黑屏"那一族有关的数**：这台从开机到首页出画用了 **~470 s**
  （197 s 时整帧 0.0% 亮像素，340 s 时仍全黑）—— 不是没起（miniapp 在跑），是慢。

## 实体键（2026-10-20）：无按键节

- 本机固件的 `hal_key_config.json` 没有 `Y11_RV1106_V0` / `Y06_RV1106_V0` /
  `Y24_RV1106_V0` 的按键节（只有 RK3562_* 等其它板型），按厂商 key config 这条路
  没有可用的 Home/音量键；控制台工具条三颗键全灰，本包不安装 `vm-keys*`。
- 若真机确实有 Home 键，需要从该机的 DTB 或其它输入服务另找路径，不能复用 s6/x5 的配方。
- 快速启动仍可用：`dpctl fast save --pack x7`（无音频，见 README 与台账 Q93）。

## 设置存储跟随 VM 状态盘（Q109，2026-10-21）

**症状**：设置 → 空间清理显示 **-2.5GB/4GB**（状态盘 8GB），空间清理列表能出来但容量账本
不跟随。

**做法**：x7 的 app 是 AArch64 + glibc 2.36，与 x6pro 同 ABI；直接复用 x6 的
`libvmstorage.so`（只要求 GLIBC_2.17，实测 2.36 可加载）。`fixup.sh` 在首靴把
`/usr/bin/miniapp` 换成 `LD_PRELOAD` wrapper、原启动器留 `.real`；shim 放
`/usr/lib/vmstorage/`。x7 没有本包的 runDictPen 替换，故必须走 fixup，不能只改
`/usr/bin`（a7 上实测直接改不跨重启）。

**操作这台的一个坑**：设置列表对触摸拖动几乎不动，QMP 的 `wheel-down` 才能滚；
`wheel-up` 到顶会直接拉出快捷面板，不是列表回滚。

**验证**（8G 状态盘）：
- app 启动约 470 s（既有现象），首页出画后 `grep vmstorage /proc/<pid>/maps` 命中
  `/usr/lib/vmstorage/libvmstorage.so`。
- 设置 → 空间清理：**已使用 1.3GB/8GB**（`vm/_x7_pack_storage2.png`）。
- 关于页的「存储容量」与空间清理走同一个 `getSystemInfo`，未单独截到中间滚动位置
  （wheel 单步跨行）；以主入口为准。
- 单测：`vmgui.tests.test_units.TestX7StorageShim`。

## 激活页：dev license 被 4.8.7 校验失败（Q110，2026-10-21）

**症状**：开机一段时间后停在「开始激活」；`/userdata/cfg/license` 存在过又消失，
日志：

```
YStartupModulePrivate::initSecondaryStartupSignal license verify failed,setMiniappCreateFinishFlag
global.verifyLicense  false redo verify
```

**根因**：框架的 S00license 写入的是固定的 174 字节开发凭证（a6 只判文件存在，
所以够用）；x7 的 OS 4.8.7 会真的验签，VM 又没有可用设备码走服务器签发，验签失败后
应用把凭证删掉并停在激活页。a7pro 的 4.13.0 也有同类问题（已记台账）。

**做法**：x7 的 `libvmstorage.so` 同时导出
`YADAPTERAPI::YSystemInfoAdapter::verifyLicense`（public/private 两个 mangled 名），
返回 `true`；S00license 照常写 dev license。验证时 license 文件保留、不再出现
`license verify failed`，首页正常。

**验证**：手动补回 `/userdata/cfg/license`（174 B）后启动，首页立即出现；随后重启整机
由 S00license 重新写入并走包内 shim，确认不再回激活页。

## 声音与空间（2026-10-22）

1. **播放点即停**：只有 `card0` 时 SoundPlayer 报
   `ALSA lib pcm_hw.c:(_snd_pcm_hw_open) Invalid value for card` 并立即 stop。
   `pack.toml` 加 `[audio] cards = 2` 后 `card1` 出现，可供播放链路打开。
2. **OEM 提示“磁盘空间不足”**（系统更新）：`/usr/bin/check_ota_space` 用
   `df | grep /userdisk` 取值；VM 里 `/userdata`、`/userdisk` 与 `/state` 是同一块
   vdb 的 bind mount，coreutils df 按设备去重只列 `/state`，于是取到 0。
   包内替换该脚本，改成 `df "$OTA_FILE_PATH" | awk 'NR==2{print $4*1024}'`。
   复验：`check_ota_space 2328236618 /userdisk` → `The space meets the requirements`。
