#!/usr/bin/env python3
"""固件包（`packs/<name>/`）—— 发现、校验、导入导出，以及"包 -> initramfs"的合并。

设计原则（见 `.trae/documents/firmware-packs-design.md`）：

  * **框架不含任何机型**：dpctl / vmgui / initramfs 里不出现具体机器的镜像名、
    面板分辨率、HAL 补丁或平台假设；一台机器的全部会变的东西都放进包里。
  * **包 = 一个目录** `packs/<name>/`：`pack.toml` + `images/` + `patches/`（+ `README.md`）。
  * **启动时选包**：`dpctl up --pack <name>`；本模块把 `patches/**` 与生成的
    `pack.env` 并进这一次启动用的 initrd（框架通用 cpio + 包内容，按哈希缓存）。

包不依赖框架的具体实现：`[platform].runtime` 只是选框架里哪个 boot backend，
框架反过来也不认识任何包。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import tarfile
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKS_DIR = os.path.join(ROOT, "packs")

# 包目录里"可传播"（对外分发）的描述性文件。默认这四项 —— 其余的是固件派生物，
# 版权归有道，不外传。包可以用 `pack.toml` 的 `[dist].include` **追加**自己要外传的
# 解释性文件（路径相对包目录），这样就不必回来改 `scripts/package.py`。
# 顺序纪律：`*.img` / `*.so` / `*.pem` / `id_*` 的硬拦截**优先于声明**（见 package.py）。
DIST_DEFAULT = ("pack.toml", "README.md", "cfg.json.overrides", "touchbridge.pl")


class PackError(Exception):
    """包缺失 / 解析失败 / 校验不过。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _resolve(path, base):
    """相对路径相对 base 解析；绝对路径直接用。"""
    if not path:
        return ""
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


# --------------------------------------------------------------------------- #
# 包
# --------------------------------------------------------------------------- #
class Pack:
    """一份 `packs/<name>/pack.toml` 解析后的实际路径与开关。"""

    def __init__(self, name, data, directory):
        self.name = name
        self.dir = directory
        self.data = data
        plat = data.get("platform", {})
        img = data.get("image", {})
        disp = data.get("display", {})
        self.note = data.get("note", "")
        self.version = str(data.get("version", ""))
        self.soc = plat.get("soc", "")
        self.runtime = plat.get("runtime", "")
        self.rootfs = _resolve(img.get("rootfs"), self.dir)
        self.boot = _resolve(img.get("boot"), self.dir)
        # 留空 = 用 runtime 的默认内核 / 框架通用 initramfs
        self.kernel = _resolve(plat.get("kernel"), self.dir)
        self.initrd = _resolve(plat.get("initrd"), self.dir)
        self.xres = int(disp.get("xres") or 0)
        self.yres = int(disp.get("yres") or 0)
        self.rotation = int(disp.get("rotation") or 0)
        self.bpp = int(disp.get("bpp") or 0)
        self.adapt = data.get("adapt", {})
        self.client_profile = (data.get("client", {}) or {}).get("profile", "")
        self.checks = data.get("checks", {}) or {}
        # 状态盘大小（MiB）。留空 = 框架默认。声明存在的理由：overlay 的 upper 层就住在
        # 这块盘上，盘满时 overlay 会退化成只读、所有 guest 适配全部失败。各机器的
        # /userdata 占用差别很大，所以大小该由包自己说。
        self.state_size_mb = int((data.get("state", {}) or {}).get("size_mb") or 0)
        # 可传播清单：包自己声明"允许外传"的**包内相对路径**（见 [dist].include）。
        # 只用于 `scripts/package.py`（打可传播代码包），不影响启动。
        self.dist_include = [str(x).replace("\\", "/")
                             for x in ((data.get("dist", {}) or {}).get("include") or [])]

    # ---- 供 dpctl 使用的派生信息 --------------------------------------- #
    def patch_sources(self):
        """包要放进 initrd `/patches/` 的文件：[(guest 路径, 宿主路径, 权限)]。

        只放包**声明了**的东西 —— 没声明的键不会碰 guest 的任何文件。
        框架认识的只是几种"语义"（logo / touchbridge / cfg 覆盖表 / 凭证脚本 /
        覆盖 guest 文件 / 自带钩子），具体内容与目标路径全部由包给出。
        """
        out = []
        for key, dst, mode in (("logo", "logo.fb.gz", 0o644),
                               ("touchbridge", "touchbridge.pl", 0o644),
                               ("cfg_overlay", "cfg.json.overrides", 0o644)):
            rel = self.adapt.get(key)
            if rel:
                out.append(("/patches/" + dst, _resolve(rel, self.dir), mode))
        # 通用"覆盖 guest 文件"：键 = 包内路径，值 = guest 绝对路径。文件在 initrd
        # 里按 basename 平铺（replace.list 也用 basename 指它）。
        for rel in (self.adapt.get("replace") or {}):
            src = _resolve(rel, self.dir)
            out.append(("/patches/" + os.path.basename(src), src, 0o755))
        # 激活凭证脚本是框架自带的通用脚本（所有有道设备共用一份），包只声明开关
        if self.adapt.get("license"):
            out.append(("/patches/restore_license.sh",
                        os.path.join(ROOT, "scripts", "restore_license.sh"), 0o755))
            # SN/UUID 写入器：同样通用，跟 license 挂钩一起装
            out.append(("/patches/write_system_info.sh",
                        os.path.join(ROOT, "scripts", "write_system_info.sh"), 0o755))
        for rel in self.adapt.get("hooks", []) or []:
            src = _resolve(rel, self.dir)
            out.append(("/patches/hooks/" + os.path.basename(src), src, 0o755))
        # 包自带的额外文件树：原样进 /patches/<相对路径>。给 hooks 当"素材"用
        # （如 s6pro 的 dropbear-tree：fixup 钩子把整棵树拷进 guest）。目录递归
        # 展开，树内保持相对结构；单文件按包内文件名平铺（与 replace 的键一致）。
        # 声明式：没写 extra 的包不受影响，框架依旧不含任何机型内容。
        for rel in self.adapt.get("extra", []) or []:
            base = _resolve(rel, self.dir)
            parent = os.path.dirname(base)
            if os.path.isdir(base):
                for root, dirs, files in os.walk(base):
                    dirs.sort()
                    for fn in sorted(files):
                        p = os.path.join(root, fn)
                        relp = os.path.relpath(p, parent).replace("\\", "/")
                        mode = os.stat(p).st_mode & 0o7777
                        out.append(("/patches/" + relp, p, mode))
            else:
                mode = os.stat(base).st_mode & 0o7777
                out.append(("/patches/" + os.path.basename(base), base, mode))
        return out

    def replace_list(self):
        """`[adapt].replace` -> "patches 下的文件名 guest 绝对路径" 的文本。

        guest 侧通用 init 只按这份表搬运，不关心文件是什么。
        """
        lines = []
        for rel, dest in (self.adapt.get("replace") or {}).items():
            lines.append(f"{os.path.basename(_resolve(rel, self.dir))} {dest}")
        return ("\n".join(lines) + "\n").encode() if lines else b""

    def env(self):
        """guest 侧通用 init 读的 `/pack/pack.env`（键=值）。"""
        lines = [f"PACK={self.name}",
                 f"RUNTIME={self.runtime}",
                 f"VERSION={self.version}",
                 f"PANEL={self.xres}x{self.yres}"]
        for key in ("battery", "license", "ap"):
            if self.adapt.get(key):
                lines.append(f"ADAPT_{key.upper()}=1")
        return ("\n".join(lines) + "\n").encode()

    def required_files(self):
        """启动必需、且必须存在的文件：[(用途, 路径)]。

        内核 / initramfs 留空时由 runtime 兜底，这里不算缺失。
        """
        req = [("rootfs", self.rootfs)]
        for dst, src, _ in self.patch_sources():
            req.append((os.path.basename(dst), src))
        return req

    def missing(self):
        return [(what, p) for what, p in self.required_files()
                if not p or not os.path.exists(p)]

    def verify_md5(self):
        """按 [checks] 核对；返回 [(路径, 期望, 实际)] 的差异列表。"""
        bad = []
        for rel, spec in self.checks.items():
            want = spec.get("md5") if isinstance(spec, dict) else spec
            if not want:
                continue
            path = _resolve(rel, self.dir)
            if not os.path.exists(path):
                bad.append((rel, want, "(缺文件)"))
            else:
                got = md5_of(path)
                if got != want:
                    bad.append((rel, want, got))
        return bad

    def summary(self):
        imgroot = os.path.basename(self.rootfs) or "-"
        panel = f"{self.xres}x{self.yres}" if self.xres and self.yres else "-"
        return (f"{self.name:<12} {self.runtime or '-':<20} {self.soc or '-':<8}"
                f" {panel:<9} {imgroot}")


def pack_names():
    """`packs/` 下的包名（= 含 pack.toml 的目录名）。"""
    if not os.path.isdir(PACKS_DIR):
        return []
    return sorted(d for d in os.listdir(PACKS_DIR)
                  if os.path.isfile(os.path.join(PACKS_DIR, d, "pack.toml")))


def load_pack(name):
    path = os.path.join(PACKS_DIR, name, "pack.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.loads(f.read().decode("utf-8-sig"))
    except FileNotFoundError:
        raise PackError(f"没有固件包 {name!r}：{path} 不存在")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise PackError(f"固件包 {name} 的 pack.toml 读取失败: {e}")
    return Pack(name, data, os.path.dirname(path))


EMPTY_ADVICE = ("没有固件包。放一个到 packs/<名字>/（含 pack.toml + images/ + patches/），"
                "或用 `dpctl pack import <文件>` 导入；\n"
                "也可以复制一个现成的 pack.toml 改路径自建一个。")


def pick_pack(name=None):
    """按名字选包；不指定时：只有一个包就用它，否则要求显式指定。"""
    names = pack_names()
    if not names:
        raise PackError(EMPTY_ADVICE)
    if name:
        if name not in names:
            raise PackError(f"未知固件包 {name!r}；可选：{', '.join(names)}")
        return load_pack(name)
    if len(names) == 1:
        return load_pack(names[0])
    raise PackError(f"有多个固件包，请用 --pack 指定：{', '.join(names)}")


def dist_plan():
    """`packs/` 下每个包的"可传播"规则：{包名: {"default": {文件名}, "paths": {包内相对路径}}}。

    `scripts/package.py` 用它决定可传播代码包里带哪些包内文件：

    * `default` —— 历史默认四项，按 **basename** 匹配（任意深度）；
    * `paths`   —— 该包 `[dist].include` 声明的**包内相对路径**。

    包没写 `[dist]` 时只有默认项，产物与引入本机制之前逐项一致；包读不出来
    （pack.toml 缺失/坏掉）时也只给默认项，不额外放行任何文件。
    """
    plan = {}
    for name in pack_names():
        paths = set()
        try:
            paths = set(load_pack(name).dist_include)
        except PackError:
            pass
        plan[name] = {"default": set(DIST_DEFAULT), "paths": paths}
    return plan


# --------------------------------------------------------------------------- #
# 导入 / 导出（单文件 .dpm = tar.gz）
# --------------------------------------------------------------------------- #
def export_pack(name, out):
    pack = load_pack(name)
    out = out if os.path.isabs(out) else os.path.join(ROOT, out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(pack.dir, arcname=pack.name)
    return out


def import_pack(archive, force=False):
    archive = archive if os.path.isabs(archive) else os.path.join(ROOT, archive)
    if not os.path.exists(archive):
        raise PackError(f"没有这个包文件：{archive}")
    os.makedirs(PACKS_DIR, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        tops = {m.name.split("/")[0] for m in tf.getmembers() if m.name.strip("/")}
        if len(tops) != 1:
            raise PackError(f"{os.path.basename(archive)} 顶层不是单独一个包目录：{sorted(tops)}")
        name = tops.pop()
        dest = os.path.join(PACKS_DIR, name)
        if os.path.exists(dest) and not force:
            raise PackError(f"包 {name} 已存在（加 --force 覆盖）")
        tf.extractall(PACKS_DIR, filter="data")
    return name


# --------------------------------------------------------------------------- #
# 面板 EDID（由包的面板参数生成，框架里没有分辨率常量）
# --------------------------------------------------------------------------- #
def _dtd(ha, va, vb=None):
    """一个 ~60Hz 的 18 字节 detailed timing descriptor。"""
    hb, hf, hs = 100, 16, 40
    if vb is None:
        vb = max(10, va // 6)
    vf, vs = 3, 2
    clock = max(1, (ha + hb) * (va + vb) * 60 // 10000)
    return bytes([
        clock & 0xFF, clock >> 8,
        ha & 0xFF, hb & 0xFF, ((ha >> 8) << 4) | (hb >> 8),
        va & 0xFF, vb & 0xFF, ((va >> 8) << 4) | (vb >> 8),
        hf & 0xFF, hs & 0xFF,
        ((vf & 0xF) << 4) | (vs & 0xF), 0,
        90, 27, 0,                         # 显示尺寸（mm）
        0, 0, 0x1E,                        # digital separate sync, +H +V
    ])


def build_edid(w, h):
    """EDID 1.4，首选模式 = 包声明的面板尺寸。

    guest 的 DRM 只提供通用模式表，falcon 会按 640x480 建 fb（UI 缩在角落、
    窗口尺寸也不对），所以要把面板尺寸喂给内核。异形尺寸的单个 timing 会被内核
    当 BAD 剔除，于是给三个 vblank 不同的等价 timing，让其中一个活下来。
    """
    modes = ((w, h, 40), (w, h, 56), (w, h, 72))
    edid = bytearray(b"\x00\xff\xff\xff\xff\xff\xff\x00")
    edid += bytes([0x64, 0x8F])           # manufacturer
    edid += (1).to_bytes(2, "little")
    edid += (1).to_bytes(4, "little")
    edid += bytes([1, 2021 - 1990, 1, 4])  # week, year, EDID 1.4
    edid += bytes([0xA0, 9, 3, 0x78, 0x0A])
    edid += bytes([0xEE, 0x91, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])
    edid += bytes(3)
    edid += bytes([0x01, 0x01] * 8)
    n = 0
    for mode in modes[:3]:
        edid += _dtd(*mode)
        n += 1
    edid += bytes([0x00, 0x00, 0x00, 0xFD, 0x00, 50, 75, 30, 80, 8,
                   0x00, 0x0A] + [0x20] * 6)
    n += 1
    edid += bytes([0, 0, 0, 0x10] + [0] * 14) * (4 - n)
    edid += bytes([0])
    edid += bytes([(-sum(edid)) & 0xFF])
    assert len(edid) == 128, len(edid)
    return bytes(edid)


# --------------------------------------------------------------------------- #
# cpio（newc）：读、写、合并 —— 把包并进框架的通用 initramfs
# --------------------------------------------------------------------------- #
def read_cpio(raw):
    """newc cpio -> [(name, mode, data, nlink)]（到 TRAILER 为止）。"""
    entries, i, n = [], 0, len(raw)
    while i + 110 <= n:
        if raw[i:i + 6] != b"070701":
            break
        f = [int(raw[i + 6 + 8 * k:i + 14 + 8 * k], 16) for k in range(13)]
        (_, mode, _, _, nlink, _, size, _, _, _, _, namelen, _) = f
        i += 110
        name = raw[i:i + namelen - 1].decode("utf-8", "replace")
        i += namelen
        i += (4 - i % 4) % 4
        data = raw[i:i + size]
        i += size
        i += (4 - i % 4) % 4
        if name == "TRAILER!!!":
            break
        entries.append((name, mode, data, nlink))
    return entries


def write_cpio(entries):
    out = bytearray()
    ino = 100
    for name, mode, data, nlink in entries:
        ino += 1
        fields = struct.pack(">13I", ino, mode, 0, 0, nlink, 0, len(data),
                             0, 0, 0, 0, len(name) + 1, 0)
        out += b"070701" + b"".join(f"{v:08X}".encode()
                                    for v in struct.unpack(">13I", fields))
        out += name.encode() + b"\0"
        out += b"\0" * ((4 - len(out) % 4) % 4)
        out += data
        out += b"\0" * ((4 - len(data) % 4) % 4)
    out += b"070701" + b"0" * 104 + b"TRAILER!!!\0"
    out += b"\0" * ((4 - len(out) % 4) % 4)
    return bytes(out)


def _merge_entries(base, extra):
    """extra 覆盖 base 里的同名项，并自动补齐父目录。"""
    out = {name: (mode, data, nlink) for name, mode, data, nlink in base}
    for name, mode, data, nlink in extra:
        parts = name.split("/")
        for k in range(1, len(parts)):
            parent = "/".join(parts[:k])
            if parent and parent not in out:
                out[parent] = (0o40755, b"", 2)
        out[name] = (mode, data, nlink)
    return [(name, mode, data, nlink) for name, (mode, data, nlink) in out.items()]


def merge_initramfs(base_gz_path, extra_entries):
    """框架通用 initramfs + 包条目 -> 新的 gzip'd cpio（返回 bytes）。"""
    with open(base_gz_path, "rb") as f:
        raw = gzip.decompress(f.read())
    merged = _merge_entries(read_cpio(raw), extra_entries)
    return gzip.compress(write_cpio(merged), 6)


def pack_signature(pack):
    """包内容的指纹（换包/改包就该重建 initrd）。"""
    h = hashlib.sha256()
    h.update(open(os.path.join(pack.dir, "pack.toml"), "rb").read())
    for dst, src, _ in pack.patch_sources():
        h.update(dst.encode())
        if os.path.exists(src):
            h.update(md5_of(src).encode())
    return h.hexdigest()


def ensure_initrd(pack, base_initrd, runtime_extra, vm_dir):
    """生成/复用 `vm/initrd-<pack>.cpio.gz`（框架通用 + 包 patches + pack.env + EDID）。

    哈希缓存：只有框架 initramfs、包内容或 runtime 附加项变了才重建（约 1 秒）。
    """
    os.makedirs(vm_dir, exist_ok=True)
    out = os.path.join(vm_dir, f"initrd-{pack.name}.cpio.gz")
    meta_path = out + ".meta.json"

    extra = []
    for dst, src, mode in pack.patch_sources():
        if not os.path.exists(src):
            raise PackError(f"包 {pack.name} 缺文件：{src}")
        extra.append((dst.lstrip("/"), mode | 0o100000, open(src, "rb").read(), 1))
    if pack.xres and pack.yres:
        extra.append(("lib/firmware/edid/pen.bin", 0o100644, build_edid(pack.xres, pack.yres), 1))
    extra.append(("pack/pack.env", 0o100644, pack.env(), 1))
    rl = pack.replace_list()
    if rl:
        extra.append(("patches/replace.list", 0o100644, rl, 1))
    extra.extend(runtime_extra or [])

    sig = {"base": md5_of(base_initrd), "pack": pack_signature(pack),
           # runtime 附加项按"名字 + 内容"进指纹：同一个名字换了内容也要重建
           "extra": hashlib.sha256(b"".join(
               name.encode() + hashlib.sha256(data).digest()
               for name, _, data, _ in (runtime_extra or []))).hexdigest()}
    if os.path.exists(out) and os.path.exists(meta_path):
        try:
            if json.load(open(meta_path)) == sig:
                return out
        except (OSError, ValueError):
            pass
    with open(out, "wb") as f:
        f.write(merge_initramfs(base_initrd, extra))
    json.dump(sig, open(meta_path, "w"))
    return out


def copy_into_pack(name, src, dest_rel):
    """把宿主文件放进包里（自建包/迁移时用）。"""
    dest = os.path.join(PACKS_DIR, name, dest_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def open_archive(path):
    """给调用方按需读取包文件（导出校验等）。"""
    return io.open(path, "rb")