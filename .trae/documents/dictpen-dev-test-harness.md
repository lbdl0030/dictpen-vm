# dictpen-vm 开发/真机测试工具（dpctl）实施计划

## Context（为什么做这个）

当前 `dictpen-vm` 已经能让**词典笔真实固件**（A6pro / RV1106 / YoudaoOS 4.7.4）在 Windows 上引导、SSH 登录、串口交互，但它一次性、手工、无状态：

- 没有任何**文件同步/下发**通道（只有 `ssh-vm.cmd` 裸 SSH，无 scp 封装）；
- 没有**命令执行结果语义**——`scripts/console.py` 的 `run_cmd` 虽然打印了 `rc=$?`，但正则未捕获、退出码不回传、无超时；
- 没有**测试运行器、日志采集、报告**；
- 最致命：改动与数据全落在 tmpfs（overlay upper + `/data /userdata /userdisk /misc`），**一重启全丢**，无法作为开发环境反复迭代。

用户目标（原话）："我想写成能够用来开发的真机测试工具"。

产出目标：一个**零依赖、免管理员**的宿主机 CLI（`dpctl`），把"改代码 → 下发 → 重启 → 执行 → 断言 → 看结果"变成一个可重复、可自动化的闭环；同一套 CLI 通过 `--target` 也能直接对**局域网里的真实词典笔**操作。

## 已实测确认的前提（决定了方案选型）

| 事实 | 结论 |
|------|------|
| 便携 QEMU `-fsdev help` → `fsdev support is disabled` | **virtio-9p/virtfs 不可用**，共享目录方案出局 |
| 宿主 OpenSSH_for_Windows_9.5p2，`scp` 默认 SFTP | `scp` 需显式 `-O` 走 legacy；客机 dropbear 是否有 `sftp-server` 未知 |
| 宿主 Python 3.15（含 `tomllib`）、有 `qemu-img.exe`、无 `paramiko` | 用标准库实现，test 用例可用 TOML |
| dropbear 通道（端口 2222）已验证可用 | **SSH 作为主传输/执行通道，串口作为早期引导兜底** |
| 项目**不是 git 仓库**，无 CI/Makefile/测试 | 从零搭建，但要保持最小文件数 |

## 范围假设（若不符请按"待确认"改）

默认按**通用设备开发/测试 harness**设计：既能测系统/原生层（库、启动项、脚本），也能下发文件与采集 app 日志；对有道 miniapp（JS/资源）走"推送 + 日志/marker 断言"的**逻辑级**验证（像素级渲染在 Windows 上无解，见下）。三种解读（原生层 / miniapp 应用层 / 固件回归）在本架构下只是**用例集合不同**，不改骨架。

## 推荐架构：单文件 Python CLI `scripts/dpctl.py`

- 仅用标准库（`socket` / `subprocess` / `argparse` / `tomllib` / `json`），零安装、免管理员。
- 复用现有 QEMU 启动参数（`run-vm.cmd`），但由 CLI 统一编排，避免两处漂移。
- 两个内部类：`Qemu`（生命周期、端口探测、日志）与 `Guest`（transport 可插拔，VM 与真机共用）。
- **`--target` 抽象**：`--target vm`（默认）或 `--target root@192.168.x.x`；后者直连真机，复用同一套 put/get/sh/test。

子命令：

| 子命令 | 作用 |
|--------|------|
| `up [--fresh] [--headless] [--dev]` | 拉起 QEMU（追加状态盘），日志 → `vm/qemu.log`，pid → `vm/qemu.pid` |
| `down` / `status` | 先 `ssh poweroff`，超时回落 `taskkill`（兼容 `stop-vm.cmd`） |
| `wait-boot [--timeout 180] [--app]` | 先等 SSH 22 通，再等 `/tmp/MiniappCreateFinishFlag` |
| `sh -c CMD [--timeout N]` | **修复 rc 缺陷**（见下），整套 harness 的地基 |
| `put` / `get` / `ls` / `sync` | 文件传输 |
| `logs [--follow] [--grep RE] [--save]` | tail `vm/serial.log` + 拉取设备 `/data/applog` |
| `test [names] [--json ...] [--junit]` | 跑用例、出报告 |
| `devmode on\|off` | 开关 miniapp launcher |
| `screenshot` | `dd if=/dev/fb0` 拉回 PPM（仅对 fbcon/logo 有效，过渡手段） |

### rc 修复（P0 核心）
改 `scripts/console.py` 的 `read_until_re` 使其**返回捕获组**（并把 `run_cmd` 返回 `(out, rc)`）；超时以 `124`、无标记以 `125` 表示。同时给 `Serial` 增加显式 `--timeout`。`console.py` 保留为**纯串口兜底**（SSH 未起时用）。

## 关键设计一：持久化（解决"重启全丢"）

9p 已不可用，采用**第二块 virtio-blk 磁盘**：

1. `dpctl up` 首次自动 `qemu-img create -f raw vm/state.img 512M`。
2. `run-vm.cmd` / `scripts/run-vm.sh` 追加 `-drive file=vm/state.img,if=virtio,format=raw,cache=writeback` → 客机 `/dev/vdb`。
3. 改 `scripts/initramfs-init.sh`：新增 `DICTPEN_STATE=/dev/vdb`；
   - ext4 挂载成功即用；失败则 `chroot $TARGET` 调客机 `mkfs.ext4`（不存在则 busybox `mkfs.vfat`）后重挂；
   - **overlay 的 `upperdir/workdir` 从 tmpfs 改到 `/state/upper`**，并把 `/data /userdata /userdisk /misc` 改为 `/state/*` 的 **bind mount**；
   - ⚠️ **安全网**：状态盘任何环节失败 → 完整回落到现有 tmpfs 分支，保证"能开机"这一底线不被破坏。
4. `dpctl up --fresh` 删盘重建；`dpctl status` 显示状态盘是否生效。

## 关键设计二：传输通道（按优先级自动选择）

1. **首选**：`ssh -T root@host "cat > /path"` / `"cat /path"`（Python bytes 直通 stdin/stdout，避免 CRLF 污染）——零依赖、必通。
2. **加速**：`scp -O`（legacy 协议，规避 sftp-server 缺失风险）。
3. **兜底**：串口 base64 分块（SSH 未起时）。
4. **大文件**：客机 `wget http://10.0.2.2:8000/...`（user-net 网关即宿主），宿主 `python -m http.server`。

`dpctl put` 内部按文件大小自动选 1/2。

## 关键设计三：测试运行器

用例目录 `tests/cases/*.test`（TOML，宿主持有 `tomllib`）：

```toml
name = "root 可写且持久"
timeout = 30
script = "echo hi > /state/probe && sync"
expect_rc = 0
expect_re = ""
serial_re = ""        # 对 vm/serial.log 快照做正则（专治 falcon abort 只看串口 marker）
```

执行链：`dpctl up --fresh` → `wait-boot` → 逐用例 `sh` + 读 serial 快照 → 表格输出 + `vm/reports/<ts>.json`（含每例 stdout/rc/serial 片段）+ 可选 JUnit XML。

首批冒烟用例（4 个）：
1. SSH/root 就绪、`id` 为 uid=0；
2. `/state` 与 `/data` 可写，**重启后仍在**（验证状态盘）；
3. `/usr/lib/libfalcon.so` 哈希 == `vm/libfalcon-patched.so`（验证补丁生效）；
4. 串口日志中 falcon **无 `abort`/`Bus error`**（`devmode on` 时用 `timeout 20 /usr/bin/runDictPen` 前台跑一次采集首帧错误）。

## 关键设计四：UI 阻塞与 dev-mode（实事求是）

Windows QEMU 的 virtio-gpu **virgl/blob 双缺**，falcon 的 DRM 路径无法完成，故默认禁用 `S50launcher`。方案：

- `dpctl devmode on` 只做两件事：把 `S50launcher.disabled` 改回可执行、确保 `cfg.json` bpp=32；**不**纳入默认开机（避免守护进程刷屏），由用例前台运行采集。
- app 层迭代：`dpctl sync` 推 `/etc/miniapp/resources`（JS/资源）+ 读 `/data/applog` 与控制台 marker 做**逻辑断言**。
- 像素级验证两条路：`dpctl screenshot`（fbcon/logo 过渡）+ 在 **Linux 宿主 + KVM/virgl** 上复用同一 `dpctl`（仅换 `--target`）。

## 分阶段实施（新增/改动文件控制在 5 个）

| 阶段 | 改动文件 | 内容 | 验收 |
|------|----------|------|------|
| **P0** | `scripts/dpctl.py`（新建）、`scripts/console.py`（改 rc） | CLI 骨架：`up/down/status/wait-boot/sh` | `dpctl sh -c "id"` 返回真实 rc |
| **P1** | `scripts/initramfs-init.sh`、`run-vm.cmd`、`scripts/run-vm.sh` | 状态盘 + overlay/数据持久化 + 回落 | 重启后 `/state` 文件仍在 |
| **P2** | `scripts/dpctl.py` | `put/get/ls/sync`（ssh cat + scp -O） | 下发文件并在设备上读到 |
| **P3** | `scripts/dpctl.py`、`tests/cases/*.test`（新建） | 日志采集 + 用例运行器 + 报告 | `dpctl test` 出 JSON/JUnit |
| **P4** | `scripts/dpctl.py` | `devmode`、`screenshot`、`--target` 真机支持 | 对真机执行 `dpctl sh`/`test` |

保持 `console.py`（做 rc 修复后作串口兜底）、`ssh-vm.cmd`、`stop-vm.cmd` 可用不变。

## 需要你拍板（会显著影响 P0 顺序）

1. **"开发"的层级**：系统/原生层（库、启动项、脚本）？有道 **miniapp 应用层**（JS/资源）？还是**固件回归测试**？
   → 若偏 miniapp：P0–P2 必须优先，且需提供 miniapp 目录结构与重载机制（`S50launcher` 是否支持热重载）。
   → 若偏回归：重点在 P3 的报告/JUnit 与 serial marker 断言。
2. **是否要同一套用例跑真实笔**（局域网 `root@笔IP`）？若是，`--target` 与 SSH 通道复用提前到 P0。
3. **状态盘格式偏好**：ext4（完整语义）还是 vfat（Windows 可原生读）？容量默认 512M 可否？

## 验证方法（端到端）

1. `python scripts/dpctl.py up --fresh` → GTK 窗口出现、`vm/qemu.log` 无 error。
2. `python scripts/dpctl.py wait-boot --timeout 180` → 返回 0。
3. `python scripts/dpctl.py sh -c "id; uname -a"` → 打印 `uid=0` 且**返回真实 rc**。
4. `python scripts/dpctl.py put ./some.txt /state/some.txt` → 设备上 `cat` 校验一致。
5. `python scripts/dpctl.py down && python scripts/dpctl.py up` → `sh -c "cat /state/some.txt"` **仍存在**（持久化达成）。
6. `python scripts/dpctl.py test` → 4 个冒烟用例全绿，`vm/reports/<ts>.json` 生成。
7. 回归：确认原有 `run-vm.cmd`、`ssh-vm.cmd`、`console.cmd` 仍可单独使用。