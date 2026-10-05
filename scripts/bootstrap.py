#!/usr/bin/env python3
"""环境重建 —— 一条命令把"缺什么、去哪拿、怎么校验、怎么构建"跑完。

    python scripts/bootstrap.py            # 检查 + 能自动的自动获取/构建
    python scripts/bootstrap.py --check    # 只检查，不动任何文件
    python scripts/bootstrap.py --only qemu
    python scripts/bootstrap.py --force    # 已就位的项目也重跑一次

清单在 deps/manifest.toml：项目里只带代码，固件/工具链都是外部大文件，
README《分发与重建》讲清了为什么这么分。

退出码：0 = 必需项都就位；1 = 还有必需项缺失（多为需要手工下载的固件）。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

try:
    import tomllib
except ModuleNotFoundError:                      # pragma: no cover
    sys.exit("需要 Python 3.11+（tomllib）")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.environ.get("DICTPEN_MANIFEST",
                          os.path.join(ROOT, "deps", "manifest.toml"))
PY = sys.executable

OK, MISS, DONE, MANUAL, SKIP = "就位", "缺失", "已获取", "需手工", "跳过"


def rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def under(path):
    """清单里的相对路径 -> 绝对路径（也接受绝对路径）。"""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def human(n):
    return f"{n / (1 << 20):.1f}MB" if n else "?"


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"    GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "dictpen-vm/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)
    print(f"    -> {rel(dest)} ({human(os.path.getsize(dest))})")


def unzip_into(zip_path, into):
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(into)
    print(f"    展开到 {rel(into)}")


def run(cmd, **kw):
    print("    $ " + " ".join(os.path.basename(c) if i == 0 else c
                              for i, c in enumerate(cmd)))
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败({r.returncode}): {cmd[0]}")


# --------------------------------------------------------------------------- #
# 每类依赖的获取方式（how 字段）
# --------------------------------------------------------------------------- #
def how_file(item):
    zip_path = os.path.join(ROOT, "vm", os.path.basename(item["url"]))
    download(item["url"], zip_path)
    unzip_into(zip_path, under(item["into"]))
    os.remove(zip_path)


def how_msys2(item):
    mirror = item.get("mirror", "https://mirrors.tuna.tsinghua.edu.cn/msys2/mingw/ucrt64")
    zstd = os.path.join(ROOT, "tools", "zstd", "zstd-v1.5.7-win64", "zstd.exe")
    if not os.path.exists(zstd):
        raise RuntimeError("先装 zstd：python scripts/bootstrap.py --only zstd")
    db = os.path.join(ROOT, "tools", "db")
    if not os.path.isdir(db) or not os.listdir(db):
        # msys2-fetch.py 要先有包索引（每个包一个目录，里面有 desc）
        db_zst = os.path.join(ROOT, "vm", "mingw64.db.tar.zst")
        download(f"{mirror}/mingw64.db.tar.zst", db_zst)
        db_tar = os.path.join(ROOT, "vm", "mingw64.db.tar")
        with open(db_tar, "wb") as f:
            run([zstd, "-dc", db_zst], stdout=f)
        os.makedirs(db, exist_ok=True)
        with tarfile.open(db_tar) as tf:
            tf.extractall(db)
        for tmp in (db_zst, db_tar):
            os.remove(tmp)
        print(f"    软件包索引 -> {rel(db)}")
    run([PY, os.path.join(ROOT, "scripts", "msys2-fetch.py")] + list(item["packages"]))


def how_deb(item):
    debs = [under(p) for p in item["debs"]]
    have = [p for p in debs if os.path.exists(p)]
    if not have:
        raise Manual(f"把 {', '.join(os.path.basename(p) for p in debs)} 下载到 "
                     f"{rel(os.path.dirname(debs[0]))}/ 后重跑（{item.get('where', '')}）")
    for deb in have:
        run([PY, os.path.join(ROOT, "scripts", "deb-extract.py"), deb, under(item["into"])])


def how_deb_arm64(item):
    run([PY, os.path.join(ROOT, "scripts", "deb-arm64-fetch.py")] + list(item["packages"]))


def how_ssh_key(item):
    key = under(item["check"])
    os.makedirs(os.path.dirname(key), exist_ok=True)
    run(["ssh-keygen", "-t", "ed25519", "-f", key, "-N", "", "-C", "dictpen-vm"])
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if os.name == "nt" and user:
        # Windows OpenSSH 拒绝权限过宽的私钥，必须收紧
        subprocess.run(["icacls", key, "/inheritance:r",
                        "/grant:r", f"{user}:R"], capture_output=True, text=True)
        print(f"    已收紧权限: {rel(key)}")


def _openssl():
    local = os.path.join(ROOT, "tools", "msys2", "ucrt64", "bin", "openssl.exe")
    return local if os.path.exists(local) else shutil.which("openssl")


def how_cert(item):
    bad = _openssl()
    if not bad:
        raise Manual("没有 openssl（装完 QEMU 后 tools/msys2/ucrt64/bin/openssl.exe 就有）")
    out = under(item["check"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run([bad, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
         "-keyout", os.path.join(os.path.dirname(out), "key.pem"),
         "-out", out, "-subj", "/CN=fanyidan.youdao.com",
         "-addext", "subjectAltName=DNS:fanyidan.youdao.com,"
                    "DNS:fanyiegg-test.youdao.com"])


def how_initramfs(item):
    run([PY, os.path.join(ROOT, "scripts", "build-initramfs.py")])


def how_firmware(item):
    into = item.get("into", "downloads")
    missing, bad = [], []
    for f in item["files"]:
        path = under(os.path.join(into, f["local"]))
        if not os.path.exists(path):
            missing.append(f)
        elif md5_of(path) != f["md5"]:
            bad.append((f, path))
    if bad:
        raise Manual("文件在但校验不过，请重新下载：\n" + "\n".join(
            f"      {rel(p)}  MD5 期望 {f['md5']}，实际 {md5_of(p)}" for f, p in bad))
    if missing:
        raise Manual(
            f"固件需自行下载（{item.get('note', '')}）：\n"
            f"      {item.get('page', '')}\n" + "\n".join(
                f"      {f['upstream']:<24} -> {into}/{f['local']}\n"
                f"      MD5 {f['md5']}" for f in missing))


def how_manual(item):
    raise Manual(item.get("where", "") + ("  " + item.get("note", "")
                                         if item.get("note") else ""))


HANDLERS = {"file": how_file, "msys2": how_msys2, "deb": how_deb,
            "deb-arm64": how_deb_arm64, "ssh-key": how_ssh_key, "cert": how_cert,
            "initramfs": how_initramfs, "firmware": how_firmware, "manual": how_manual}


class Manual(Exception):
    """需要人来做（下载版权文件等），bootstrap 只能给出指引。"""


# --------------------------------------------------------------------------- #
def inspect(item):
    """(状态, 说明)。已有文件时顺带校验 md5。"""
    paths = [item["check"]] + list(item.get("also", []))
    for p in paths:
        check = under(p)
        if not os.path.exists(check):
            return MISS, f"缺 {rel(check)}"
    want = item.get("md5")
    if want and os.path.isfile(under(item["check"])):
        got = md5_of(under(item["check"]))
        if got != want:
            return MISS, f"{item['check']} MD5 不符（期望 {want}，实际 {got}）"
        return OK, f"{rel(under(item['check']))}  (MD5 已核对)"
    return OK, rel(under(item["check"]))


def hint(item):
    """只给下载指引、不碰网络（用于 --check）。"""
    how = item["how"]
    if how == "firmware":
        into = item.get("into", "downloads")
        todo = [f for f in item["files"]
                if not os.path.exists(under(os.path.join(into, f["local"])))]
        if not todo:
            return None
        return (f"      去 {item.get('page', '')} 拿（{item.get('note', '')}）：\n"
                + "\n".join(f"      {f['upstream']:<22} -> {into}/{f['local']}"
                            f"   MD5 {f['md5']}" for f in todo))
    if how == "manual":
        return f"      {item.get('where', '')}" + (
            f"\n      {item['note']}" if item.get("note") else "")
    if how == "deb":
        names = ", ".join(os.path.basename(p) for p in item["debs"])
        return f"      下载 {names} 放到 downloads/ 后重跑\n      {item.get('where', '')}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只检查，不改动文件")
    ap.add_argument("--only", metavar="ID", help="只处理清单里的某一项")
    ap.add_argument("--force", action="store_true", help="已就位的项目也重跑")
    args = ap.parse_args()

    try:
        with open(MANIFEST, "rb") as f:
            # utf-8-sig：Windows 编辑器常给 TOML 加 BOM，tomllib 会因为 BOM 报
            # "Invalid statement (line 1, column 1)"，这里直接吃掉
            manifest = tomllib.loads(f.read().decode("utf-8-sig"))
    except OSError as e:
        print(f"读不到清单 {MANIFEST}: {e}")
        return 2
    except tomllib.TOMLDecodeError as e:
        print(f"清单 {rel(MANIFEST)} 语法错误: {e}")
        return 2
    items = manifest["item"]
    if args.only:
        items = [i for i in items if i["id"] == args.only]
        if not items:
            print(f"清单里没有 {args.only}")
            return 2

    state, failed = {}, False
    for item in items:
        st, detail = inspect(item)
        print(f"[{st}] {item['id']:<14} {item['what']}")
        if st == MISS:
            print(f"        {detail}")
            if args.check:
                advice = hint(item)
                if advice:
                    print(advice)
        if st == OK and not args.force:
            state[item["id"]] = OK
            continue
        if args.check:
            state[item["id"]] = st
            if st == MISS and not item.get("optional"):
                failed = True
            continue

        needs = item.get("needs") or []
        blocked = [n for n in needs if state.get(n) not in (OK,)]
        if blocked:
            print(f"        跳过：依赖未就位 {', '.join(blocked)}")
            state[item["id"]] = SKIP
            if not item.get("optional"):
                failed = True
            continue
        try:
            HANDLERS[item["how"]](item)
            st2, _ = inspect(item)
            print(f"        -> {DONE if st2 == OK else st2}")
            state[item["id"]] = st2
            if st2 != OK and not item.get("optional"):
                failed = True
        except Manual as e:
            print(f"        -> {MANUAL}: {e}")
            state[item["id"]] = MANUAL
            if not item.get("optional"):
                failed = True
        except Exception as e:                      # 网络/命令失败都归到这里
            print(f"        -> 失败: {e}")
            state[item["id"]] = "失败"
            if not item.get("optional"):
                failed = True

    print()
    bad = [k for k, v in state.items() if v not in (OK, DONE, SKIP)]
    if bad:
        print("还需处理：" + ", ".join(f"{k}({state[k]})" for k in bad))
        print("再跑一次 `python scripts/bootstrap.py` 校验；"
              "之后 `python scripts/dpctl.py packs` 看固件包是否就位。")
        return 1
    print("全部就位。下一步：python scripts/dpctl.py up --pack <包名>")
    return 0


if __name__ == "__main__":
    sys.exit(main())