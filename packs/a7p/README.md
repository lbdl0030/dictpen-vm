# a7p —— 词典笔 A7P / YDPA7-2 / Y09P 板 / RV1106 / OS 4.13.2

**没做过的型号**（与 packs/a7pro 不同台）：a7pro 模拟的是词典笔 **A7**（YDPA7-1 / Y09 板 /
640×172 / OS 4.13.0），本包是 **A7P**（YDPA7-2 / Y09P 板 / **1020×240** / OS 4.13.2）。
仓库里叫 "a7pro" 的那台包名是历史叫法，别混淆。

## 型号判定（三方互证）

1. 固件库 OTA `Y09P_RV1106-OTA-OS99.99.91`（ABUP 测试通道号；`/Version` 实测 4.13.2，
   Build 2026-08-11）。
2. install.img 里 ota_mgr 的产品注册表：`YDPA7-2 → youdao-OVERHEAD_Y09P_SKU_CHN_PRO/PLUS/STD`
   （productId 1729062311）。同表 `YDPS7-2 ↔ Y08` 与 s7pro 包板型吻合，表可信。
3. 固件皮肤表有独立 `theme-y09p`；外部 deviceConfig：词典笔A7P，3.93 英寸，RV1106，麦度。

## 硬件画像（来自 DTB / rootfs）

- RV1106G（Cortex-A7 32 位），用户态 **uClibc armhf**（同 a6pro）
- RK816 PMIC（真电池，VM 用 `[adapt].battery` 假电量顶）
- 面板 240×1020 竖屏 → 横用 **1020×240**（cfg.json：width=1020 height=240 direction=270）
- 触摸 IC axs15206，tp 路径 `/dev/input/by-path/axs_ts`（与 a7pro 同，touchbridge.pl 直接复用）
- 音频：speaker=card1、数字耳机 dhs_*=hw:2,0 → `[audio].cards = 3`（同 a7pro）
- 板号 `Y09P_RV1106_V0`；绑 model 后固件自己选 input-event-daemon_Y09.conf 等分支

## 切分（RKAF @ 0x2da26，偏移相对 RKAF）

| 分区 | 绝对偏移 | 大小 | 落点 |
|---|---|---|---|
| boot_a | 0x664a26 | 3 647 488 | `images/a7p-boot.img`（1536B uboot stub + 板级 DTB @+0x800） |
| rootfs_a | 0x9df226 | 1 043 034 112 | `images/a7p-rootfs.img`（明文 erofs，未压缩） |

## 适配内容

全部走框架 `[adapt]`：touchbridge（通用，面板尺寸自读 cfg.json）、cfg_overlay
（direction=0 + bpp=32）、battery/license/ap、实体键三件套（model 绑成
`Rockchip RV1106 for Y09P V10 Board` + Home=102）。`[adapt].replace` 只覆盖
runDictPen.sh（去 crash-reboot + `MOCK_THEME=theme-y09p` 钉皮肤）。

## 首启实测（2026-10-24）

**已通**：起机（约 60 s）→ SSH → **完整 UI**（1020×240 全渲染，截图
`vm/shots/_a7p_boot7.png`）→ **触控**（`dpctl tap 250 120` 整页切换，`vm/shots/_a7p_tap1.png`）。
应用连云端正常（getWSToken POST 发出）。

两个补丁（都在 `[adapt.replace]`，细节见 pack.toml 注释）：

1. **libfalcon fourcc**：黑屏起手，日志 `Cannot find plane`。32 位 ARM 构建的常量
   装载是 ARM 模式 `movw/movt`，三处常量补了 XR24（0x221edc / 0x2249d4 /
   0x2249dc+e0，keystone 生成编码 + 字节级验证）**仍黑**——plane 格式循环
   （0x2249b8..0x2249cc）的比较值 r7 来源不明（不是任何 AR24 常量对）。
   判定实验：把循环回跳 `bne`@0x2249cc 改 NOP，首轮强制通过 → DRM 链路立即走通。
   NOP 保留在补丁里（AddFB2 侧仍是正经的 XR24 常量补丁）；
   **r7 的真实来源待挖**（挖明后可回退 NOP 换干净补丁）。
2. **license 恒真**：4.13.2 与 a7pro 4.13.0 不同——**首启即** `initSecondaryStartupSignal
   license verify failed`，应用直接进「验证引导」页（小块白蓝内容）。
   `yd_hal_verify_license` @0x95f0（ARM）用 `vm/patch_sym.py` 补恒真后 UI 全出。

另：验证失败路径会让 miniapp 退出（guardian 链断）；license 补好后 guardian_run
runDictPen 链稳定，miniapp 常驻（420 MB VSZ）。

### 声音（2026-10-24 实测）

- **双卡放音通**：客机 tinyplay（本机只有 tinyalsa，须 48k 立体声）正弦 →
  宿主端点峰值 card0 **80.6%** / card1 **83.3%**（耳机 60% 档）——
  `[audio] cards=3` 覆盖固件 asound 的 speaker(card1)/数字耳机(card2) 别名。
- 应用侧：SoundPlayer 由 guardian 常驻，`YVolumeAdapter` 初始化成功
  （getVolume 80 / setVolume 活跃）；theme=theme-y09p 同批日志确认生效。
- **应用内媒体播放未验**（随身听需要账号/内容上传，单独验证）。

## 复验（2026-10-25，人点名实机测，不测声音）

- 冷启动**首帧比 10-24 记的 60 s 慢**：约 130 s 才见完整首页；期间先出现撕裂残留、随后全黑一帧，
  都属于还在起，别按失败处理。判据用事件：`miniapp` 进程在 + 日志出现 `AddFB2() ret=0`。
- UI 全渲染（1020×240）、触控可用（`tap 130 110` 从首页进「查词翻译」的年级页）、
  Home(102) 回首页（`homeCb action=0/1`）。这一轮无 corefile。
- **二级预置包仍未装，原因已定位到清单缺失**：`/oem/secondary/etc/miniapp/resources/presetpkgs`
  里有 29 个 `.amr`，但 `/oem/secondary/etc/miniapp/local_package.json` **不存在**，
  日志因此是
  `[SecondaryStartup] local_package.json app not found: .../presetpkgs/8080272808953200…`
  → `need second apps install:(0)`。10-24 记的「PMS res=1」这一轮没再出现。
  修法方向：补/生成这份清单，或照 x3s342 的做法用 `miniapp_cli install` 直接装。
- 顺带：日志有 `open file failed:/sys/class/drm/card0-DPI-1/dpms`，VM 里没有这条 DRM 节点，
  和 x3s342 缺背光同源，息屏那条链在这台也落不下去。

## 修「下部分画面被裁」：屏高 240 → 266（2026-10-25）

- 现象：控制中心（下拉面板）第二行 Wi-Fi/蓝牙/亮度 只露出上半截，下半截在屏外；
  查看其它页时底部也常只露半行（`vm/shots/_a7p_cc_full.png`）。
- 取证：`/etc/miniapp/resources/cfg.json` 是 1020×240，`DRMNativeWindow::setSize(1020,240)`
  与之一致；面板高度就是 240。但固件自带的控制中心布局需要约 255~266 高（同代 x7 的逻辑屏是 960×266）。
- 修法：`pack.toml` 的 `yres` 改 **266**，`patches/cfg.json.overrides` 同步加 `height=266`
  （触摸桥按 cfg 算坐标，两边必须一致）。冷启动后 `setSize(1020,266)`，
  控制中心整行在屏内（`vm/shots/_a7p_266_cc2.png`），年级页多出的一行也露全了（`vm/shots/_a7p_266_tap.png`）。
- 代价：与 DTB 声明的 240 高差 26 px；这是让固件 UI 完整显示的最小改动。控制台会按新尺寸重排，
  换屏后要重开控制台窗口。

## 已知问题：存储页是厂商写死的 4GB（2026-10-25 取证）

- 设置 → 空间清理 显示「已使用3.9GB/4GB」（`vm/shots/_a7p_storage.png`），而状态盘实际是
  974 MiB、已用 827 MiB（`/userdisk/secondary` 435 M、`/userdisk/swap` 257 M、
  `/userdata/miniapp` 104 M）。
- 同族里 x6pro/x3s342 是用 preload shim 把 `YSystemInfoAdapter::getStorageInfo` 换成
  `statvfs("/userdata")` 解决的。**这台套不了**：a7p 用户态是 **uClibc armhf**
  （`/lib/ld-uClibc.so.0`），本机没有 uClibc/armhf 交叉工具链，glibc 的 `.so` 进不了它的加载器。
- 可选的下一步（都还没做，等人定）：
  1. 给 a7p 的适配层做**二进制补丁**（把返回里的固定值改成读状态盘，或至少改成合理值）；
  2. 把 `[state].size_mb` 调大（例如 4096），让「总量」这个数与真机一致，已用仍是厂商值；
  3. 维持现状，在文档里标成已知差异。

## 卡顿的测量（2026-10-25）

- 取帧链路不是瓶颈：一条 QMP 连接里连发 10 次 screendump，平均 **5 ms/帧**（730 KB/帧）。
- 客机里连读 `/dev/fb0` 指纹：内容每 ~200~250 ms 换一次（约 **4~5 fps**），是应用自己的刷新节奏；
  1020×266 = 271k 像素，是同族 a6pro（560×170 = 95k）的 2.9 倍，RV1106 在 TCG 下按像素算就是这个量级。
- 页面切换：`tap` 后第一帧变化约 **0.5 s**，整页画完约 1.2 s（QMP 逐帧取样）。
- 结论：卡的主因是 271k 像素的软件渲染上限，不是取帧、不是内存（2 G 内存空闲 1 G、swap 0 使用）、
  也不是 store/网络。要再快只能动应用的渲染路径（例如 `dbuffer` 关掉、或追 libfalcon 那条 NOP 的格式循环）。

## 待办（后续完善）

- [ ] libfalcon 0x2249cc 的 NOP 换成 r7 来源的正解（回溯 movw/movt 全配对后的数据流）
- [ ] 应用输入层的 `is_tp_name_supported` 告警（axs_ts_pen 名字匹配；触控实测可用，告警待消）
- [ ] `/oem/secondary` 预置包：补 `local_package.json`（29 个 `.amr` 已在位），或改用
      `miniapp_cli install` 直接装；装完再看页面入口是否齐
- [ ] 应用内媒体播放实测（双卡链路与音量适配已验，见「声音」节）、Home 键码 102 核对
- [ ] 存储页：等人在「适配层二进制补丁 / 放大状态盘 / 维持现状」里选一条（见上节）
- [ ] 卡顿：如果人觉得还需要更快，再从 `dbuffer` 与 libfalcon 的格式循环下手

