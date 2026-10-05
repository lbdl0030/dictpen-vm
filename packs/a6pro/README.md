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
| `patches/libfalcon.so` | 打过补丁的有道显示 HAL（DRM 格式协商，见框架 README《深度分析》） |
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