#!/usr/bin/env python3
"""Resolve and download MSYS2 mingw64 packages with dependencies (no admin needed).

Usage:
  python scripts/msys2-fetch.py <pkg1> [pkg2 ...]
  python scripts/msys2-fetch.py --only-extract   # extract already-downloaded zst files

Reads tools/mingw64.db (extracted under tools/db), resolves %DEPENDS% transitively,
downloads each package from the mirror and extracts it into tools/msys2/.
"""
import os
import sys
import subprocess
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
DB = os.environ.get("DB_DIR", os.path.join(TOOLS, "db"))
PKGDIR = os.path.join(TOOLS, "pkgs")
MSYS2 = os.path.join(TOOLS, "msys2")
ZSTD = os.path.join(TOOLS, "zstd", "zstd-v1.5.7-win64", "zstd.exe")
MIRROR = os.environ.get(
    "MSYS2_MIRROR",
    "https://mirrors.tuna.tsinghua.edu.cn/msys2/mingw/ucrt64",
)


def parse_db():
    """Return {name: {filename, depends:[...]}} (name from %NAME%, no version)."""
    pkgs = {}
    for dir_ in os.listdir(DB):
        desc = os.path.join(DB, dir_, "desc")
        if not os.path.isfile(desc):
            continue
        info = {"filename": None, "depends": []}
        key, name = None, dir_
        with open(desc, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("%") and line.endswith("%"):
                    key = line[1:-1]
                elif line and key == "NAME":
                    name = line
                elif line and key == "FILENAME":
                    info["filename"] = line
                elif line and key == "DEPENDS":
                    info["depends"].append(line)
                elif not line:
                    key = None
        if info["filename"]:
            pkgs[name] = info
    return pkgs


def dep_name(dep):
    for sep in (">=", "<=", "=", ">", "<"):
        i = dep.find(sep)
        if i > 0:
            dep = dep[:i]
    return dep.strip()


def resolve(pkgs, roots):
    seen, order, missing = set(), [], []
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        if name not in pkgs:
            missing.append(name)
            continue
        seen.add(name)
        order.append(name)
        for dep in pkgs[name]["depends"]:
            stack.append(dep_name(dep))
    return order, missing


def download(url, dest):
    print(f"  GET {os.path.basename(dest)}")
    req = urllib.request.Request(url, headers={"User-Agent": "dictpen-vm/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def extract(pkg_path):
    tmp = pkg_path[:-4]  # strip .zst
    with open(tmp, "wb") as out:
        subprocess.run([ZSTD, "-dc", pkg_path], stdout=out, check=True)
    r = subprocess.run(
        ["tar", "-xf", tmp, "-C", MSYS2,
         "--exclude=share/locale/*", "--exclude=share/man/*",
         "--exclude=share/doc/*", "--exclude=share/gtk-doc/*"],
        capture_output=True, text=True)
    os.remove(tmp)
    if r.returncode != 0:
        # hardlink-incompatible files etc. are tolerable; report briefly
        bad = [l for l in (r.stderr or "").splitlines() if "Can't create" not in l]
        if bad:
            print(f"  warn {os.path.basename(pkg_path)}: {bad[:2]}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    os.makedirs(PKGDIR, exist_ok=True)
    os.makedirs(MSYS2, exist_ok=True)
    pkgs = parse_db()
    print(f"db entries: {len(pkgs)}")
    roots = [f"mingw-w64-x86_64-{a}" if not a.startswith("mingw-") else a for a in args]
    order, missing = resolve(pkgs, roots)
    print(f"resolved {len(order)} packages; missing: {missing or 'none'}")
    todo = []
    for name in order:
        fn = pkgs[name]["filename"]
        dest = os.path.join(PKGDIR, fn)
        if not os.path.exists(dest):
            todo.append((name, fn, dest))
    print(f"need to download {len(todo)}")
    for i, (name, fn, dest) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {name}")
        download(f"{MIRROR}/{fn}", dest)
    print("extracting all packages ...")
    for name in order:
        fn = pkgs[name]["filename"]
        dest = os.path.join(PKGDIR, fn)
        if os.path.exists(dest):
            extract(dest)
    print(f"done -> {MSYS2}")


if __name__ == "__main__":
    main()
