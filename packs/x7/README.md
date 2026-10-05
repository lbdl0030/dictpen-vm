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

两处补丁由 `vm/_x7-probe/patch-fourcc.py` 生成（对原始 `libfalcon.so` 打，产出
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
`vm/_x7-probe/`（`youdao_startup_logo.bmp` 为从镜像里拉出的原件）。

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
