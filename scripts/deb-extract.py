#!/usr/bin/env python3
"""Extract .deb (ar archive) into a directory: debian-binary, control.tar.*, data.tar.*."""
import io
import os
import sys
import tarfile


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
        pos = start + size + (size & 1)  # 2-byte alignment


def main():
    deb, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    for name, data in ar_members(deb):
        if name.startswith("data.tar"):
            name = name.rstrip("/")  # trailing slash in some debs
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(outdir, filter="data")
            print(f"extracted {name} ({len(data)} bytes) -> {outdir}")
        else:
            print(f"skipped {name}")


if __name__ == "__main__":
    main()
