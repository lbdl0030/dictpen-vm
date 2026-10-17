#!/usr/bin/env python3
"""打一个"可传播"的包 —— 只包含代码/文档/固件包描述，不含固件、工具链、私钥、状态盘。

    python scripts/package.py                 # 列清单 + 自检（不写文件）
    python scripts/package.py --out dist/x.zip
    python scripts/package.py --check         # 只自检，违规就非零退出

为什么要这样做：整个工作目录 >4GB，其中绝大多数是可再获取的外部依赖
（QEMU 3GB、固件 1.3GB、状态盘 512MB）和**不该外传**的东西（SSH 私钥、
自签证书）。收到包的人跑 `python scripts/bootstrap.py` 就能把这些补齐。
"""
import argparse
import fnmatch
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packlib                                                # noqa: E402

# 只带这些（白名单比黑名单安全：新增大文件不会被误打进包里）
INCLUDE_DIRS = ["scripts", "vmgui", "docs", "deps", ".trae/documents"]
INCLUDE_FILES = ["README.md", ".gitignore", "run-vm.cmd", "stop-vm.cmd",
                 "ssh-vm.cmd", "console.cmd", "downloads/releases.json"]
# 固件包只带"描述"，不带固件派生的二进制（那些和镜像一样归有道，且很大）。
# 具体带哪些由**包自己**说了算：`pack.toml` 的 `[dist].include` 追加，缺省是
# packlib.DIST_DEFAULT 那四项。见 packlib.dist_plan()。
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".pyo", ".img", ".deb", ".pem", ".key", ".iso", ".zip"}

# 自检：这些东西出现在包里就是事故
FORBIDDEN = [("id_*", "SSH 私钥"), ("*.pem", "证书/密钥"), ("*.key", "私钥"),
             ("*.img", "固件镜像"), ("*.deb", "外部软件包"),
             ("*.cpio.gz", "构建产物"), ("state.img", "状态盘"),
             ("*.so", "固件派生的二进制")]
MAX_TOTAL = 8 << 20          # 代码+文档应当只有几百 KB；超过就说明混进了大文件


def dist_plan():
    """`{包名: {"default": {文件名}, "paths": {包内相对路径}}}`；解析器坏了也照样打包。"""
    try:
        return packlib.dist_plan()
    except Exception:                    # noqa: BLE001 打包自检不该因包解析失败而崩掉
        return {}


def wanted(relpath, dp):
    parts = relpath.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts):
        return False
    if os.path.splitext(relpath)[1].lower() in SKIP_EXT:
        return False
    if parts[0] == "packs":                      # 只带包描述，见 packlib.dist_plan()
        if len(parts) < 2:
            return False
        rules = dp.get(parts[1])
        if rules is None:                        # 包没解析出来：退回默认四项
            return os.path.basename(relpath) in packlib.DIST_DEFAULT
        if os.path.basename(relpath) in rules["default"]:
            return True
        return "/".join(parts[2:]) in rules["paths"]
    if relpath.replace("\\", "/") in INCLUDE_FILES:
        return True
    return parts[0] in INCLUDE_DIRS


def collect(dp):
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT)
            if wanted(rel, dp):
                files.append(rel)
    return sorted(files)


def dist_problems(dp):
    """`[dist].include` 的自检：必须是包内相对路径、必须存在、不能是禁止外传的类型。

    这三条是**优先于声明**的硬纪律 —— 包声明了也不放行 `*.img/*.so/*.pem/id_*`。
    """
    problems = []
    for name, rules in sorted(dp.items()):
        for rel in sorted(rules["paths"]):
            norm = rel.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                problems.append(f"packs/{name}/pack.toml 的 [dist].include 里有"
                                f"非包内相对路径：{rel}")
                continue
            if not os.path.isfile(os.path.join(ROOT, "packs", name, norm)):
                problems.append(f"packs/{name}/pack.toml 声明的可传播文件不存在：{rel}")
                continue
            base = os.path.basename(norm)
            if os.path.splitext(norm)[1].lower() in SKIP_EXT or any(
                    fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(norm, pat)
                    for pat, _ in FORBIDDEN):
                problems.append(f"packs/{name}/pack.toml 声明了禁止外传的 {rel}"
                                f"（*.img/*.so/*.pem/id_* 一律不进可传播包）")
    return problems


def self_check(files, dp):
    problems = dist_problems(dp)
    total = 0
    for rel in files:
        size = os.path.getsize(os.path.join(ROOT, rel))
        total += size
        base = os.path.basename(rel)
        for pattern, why in FORBIDDEN:
            if fnmatch.fnmatch(base, pattern) or fnmatch.fnmatch(rel.replace("\\", "/"), pattern):
                problems.append(f"{rel} 不该打包（{why}）")
    if total > MAX_TOTAL:
        problems.append(f"总大小 {total / (1 << 20):.1f}MB 超过上限 "
                        f"{MAX_TOTAL / (1 << 20):.0f}MB —— 多半混进了大文件")
    return problems, total


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", metavar="ZIP", help="写出 zip（默认只列清单）")
    ap.add_argument("--check", action="store_true", help="只自检，有问题就返回 1")
    args = ap.parse_args()

    dp = dist_plan()
    files = collect(dp)
    problems, total = self_check(files, dp)
    print(f"将打包 {len(files)} 个文件，共 {total / 1024:.0f} KB")
    for rel in files:
        print("  " + rel)
    if problems:
        print("\n自检未通过：")
        for p in problems:
            print("  [!] " + p)
        return 1
    print("\n自检通过：没有固件/外部依赖/私钥/状态盘。")
    if args.check or not args.out:
        print("收包的人：python scripts/bootstrap.py（按 deps/manifest.toml 补齐依赖）")
        return 0

    out = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            z.write(os.path.join(ROOT, rel), arcname=rel.replace("\\", "/"))
    size = os.path.getsize(out)
    sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
    print(f"\n{out}\n  {size / 1024:.0f} KB  sha256={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())