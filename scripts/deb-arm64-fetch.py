#!/usr/bin/env python3
"""Fetch arm64 (aarch64) Debian packages and extract them into a tree.

The pen's own rootfs is 32-bit (uClibc) and ships no hostapd, but its kernel is
aarch64, so 64-bit Debian binaries run there as-is.  This pulls hostapd and the
libraries it needs so the VM can publish a real (connectable) access point.

Usage: python scripts/deb-arm64-fetch.py <pkg> [<pkg> ...]
Env:   DEB_SUITE (default bookworm), DEB_MIRROR
"""
import gzip
import io
import os
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DL = os.path.join(ROOT, "downloads", "arm64")
OUT = os.path.join(ROOT, "tools", "arm64-rootfs")
MIRROR = os.environ.get("DEB_MIRROR", "https://mirrors.tuna.tsinghua.edu.cn/debian")
SUITE = os.environ.get("DEB_SUITE", "bookworm")
ARCH = "arm64"
COMPONENTS = ("main", "non-free", "non-free-firmware")


def fetch(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"cached  {os.path.basename(dest)}")
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"get     {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def packages_index():
    """{name: filename} from every configured component's arm64 index."""
    idx = {}
    for comp in COMPONENTS:
        url = f"{MIRROR}/dists/{SUITE}/{comp}/binary-{ARCH}/Packages.gz"
        try:
            raw = urllib.request.urlopen(url, timeout=120).read()
        except Exception as exc:                       # component may not exist
            print(f"skip    {comp}: {exc}")
            continue
        name = None
        for line in gzip.decompress(raw).decode("utf-8", "replace").splitlines():
            if line.startswith("Package: "):
                name = line.split(": ", 1)[1].strip()
            elif line.startswith("Filename: ") and name:
                idx[name] = line.split(": ", 1)[1].strip()
    return idx


def ar_members(path):
    blob = open(path, "rb").read()
    assert blob[:8] == b"!<arch>\n", f"not an ar archive: {path}"
    pos = 8
    while pos < len(blob):
        hdr = blob[pos:pos + 60]
        if len(hdr) < 60:
            break
        name = hdr[0:16].decode("ascii").strip()
        size = int(hdr[48:58].decode("ascii").strip())
        start = pos + 60
        yield name, blob[start:start + size]
        pos = start + size + (size & 1)


def main():
    want = sys.argv[1:]
    if not want:
        print(__doc__)
        return 1
    idx = packages_index()
    os.makedirs(OUT, exist_ok=True)
    missing = []
    for pkg in want:
        rel = idx.get(pkg)
        if not rel:
            missing.append(pkg)
            continue
        deb = fetch(f"{MIRROR}/{rel}", os.path.join(DL, os.path.basename(rel)))
        for member, data in ar_members(deb):
            if member.startswith("data.tar"):
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    tf.extractall(OUT, filter="data")
                print(f"extract {pkg}")
    if missing:
        print(f"not found: {' '.join(missing)}", file=sys.stderr)
        return 2
    print(f"-> {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())