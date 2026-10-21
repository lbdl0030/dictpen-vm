# 固件包 a6pro

一台词典笔（A6pro / 设备树代号 `Pear_RV1106` / YoudaoOS 4.7.4）在 dictpen-vm 框架里
运行所需的**全部**内容。框架本身不含任何机型信息，启动时由 `dpctl up --pack a6pro`
挑中本包。

## 内容

| 路径 | 说明 |
|------|------|
| `pack.toml` | 元数据：平台、镜像、面板参数、适配开关、vmgui profile、校验 |
| `images/a6-2-pro-ro-rootfs.img` | 只读 erofs 根文件系统（1.28 GiB） |
| `images/a6-2-pro-ro-boot_b.img` | boot 镜像（DTB + 面板/DRM 参数，参数参考用） |
| `patches/libfalcon.so` | 打过补丁的有道显示 HAL（DRM 格式协商，见 [插件模型 §6](../docs/插件模型.md#6-插件点④显示与输入)） |
| `patches/logo.fb.gz` | 开机 logo（写 `/dev/fb0`） |
| `patches/touchbridge.pl` | 鼠标 → 触摸桥（uinput，把 tablet 变成 `axs_ts` 触摸屏） |
| `patches/cfg.json.overrides` | 只覆盖列出的 `cfg.json` 键（面板尺寸/方向/bpp/触摸路径） |

## 来源与校验

镜像版权归**网易有道**所有，仅供个人学习/研究，禁止商用；请遵守
[dictpen-rootfs](https://github.com/lbdl0030/dictpen-rootfs) 的免责声明。

| 文件 | 上游 | MD5 |
|------|------|-----|
| `a6-2-pro-ro-rootfs.img` | `A6pro_rootfs.img` | `0dfe781b76955f5dfe3d8aedde585c36` |
| `a6-2-pro-ro-boot_b.img` | `A6pro_boot.img` | `6cfa8d8ac37a057999b69a2c5098e201` |

`python scripts/dpctl.py packs a6pro` 会核对上表的 MD5 并打印每个文件的就位情况。

## 分发

`patches/libfalcon.so`、`logo.fb.gz` 与镜像一样是从固件派生的二进制，**不进**可传播的
代码包（`scripts/package.py` 只带 `pack.toml` / `README.md` / `cfg.json.overrides` /
`touchbridge.pl`）。整包搬运用：

```bat
python scripts\dpctl.py pack export a6pro -o dist\a6pro.dpm
python scripts\dpctl.py pack import dist\a6pro.dpm
```

## 实体键（2026-10-20）

- **能力**：Home，码 **102**（`Pear_RV1106_V0.home_key_id = gpio-keys`）；本包固件没有音量键项。
- **做法**：`patches/vm-keys.pl` + `vm-keys-apply` + `vm-keys.env` + `vm-keys.sh` 钩子；
  板型串 `Rockchip RV1106 for Pear V10 Board`。首次 app 启动前换 model 会黑屏，故由
  `S50vm-keys` 等首 app 起来后 apply、停掉 runDictPen 与 app、直接启动一个 miniapp。
- **验证**：冷启动后查词翻译 → 桌面（`vm/_a6_final_after.png`）；单实例；控制台只亮 Home。
- 快速启动可选：`dpctl fast save --pack a6pro`（无音频，见 README 与台账 Q93）。

## 设置存储跟随 VM 状态盘（Q107，2026-10-21）

**症状**：设置 → 空间清理显示 **3.2GB/4GB**，状态盘只有 1GB；扩盘不跟随。

**这台的特殊性**：
- app 是 **ELF32 armhf + uClibc**（`ld-uClibc.so.0`），不是 glibc，s6/s7 的 shim 不能加载。
  shim 做成 **libc-free**：`statfs` 走 raw syscall（ARM EABI 99），字符串用
  **libstdc++ 的 `operator new`（`_Znwj`）**分配——a6 的 app 重载了 new/delete，用 malloc
  分配会被它的 delete 释放时崩（实测打开存储页就 websocket abort）。
- NMT：`libyddal_base_nmt.so` 的 `loadNmtPatch` 是库内绑定，LD_PRELOAD 的 no-op 拦不住；
  包内替换一份把该函数首指令改成 `bx lr` 的库，否则 `/userdisk/uresource/localdict`
  缺失时 app 启动直接 `MiniNmtException` abort。
- 安装：a6 没有本包自己的 `runDictPen`，由 `fixup.sh` 在首靴把 `/usr/bin/miniapp` 换成
  wrapper、原启动器留成 `.real`；shim 放 `/usr/lib/vmstorage/`。

**验证**（状态盘 1GB）：`df /userdata` = 990.7M/152.5M 用。
- 设置（下拉菜单 → 齿轮）→ 空间清理：**已使用 0.3GB/1GB**（`vm/_a6_pack_storage.png`）。
- 设置 → 关于 → 存储容量：**1GB**，点进去同样 0.2GB/1GB
  （`vm/_a6_about_shim2.png`、`vm/_a6_about_storage_shim.png`）。
- 设置列表要用 mouse 形态拖动（同 s6）才滚得动；
  重启后 app 稳定、`/proc/<pid>/maps` 命中 `/usr/lib/vmstorage/libvmstorage-armhf-uclibc.so`。
- 单测：`vmgui.tests.test_units.TestA6StorageShim`。
