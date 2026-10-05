#!/usr/bin/env python3
"""Minimal flattened device tree (DTB) dumper - prints model, chosen, memory,
UART/console and a few other interesting nodes from a .dtb blob."""
import struct
import sys

FDT_BEGIN_NODE = 0x1
FDT_END_NODE = 0x2
FDT_PROP = 0x3
FDT_NOP = 0x4
FDT_END = 0x9


def parse(path):
    blob = open(path, "rb").read()
    magic, totalsize, off_struct, off_strings, off_memrsv, version = struct.unpack(
        ">IIIIII", blob[:24])
    assert magic == 0xD00DFEED, f"not a DTB: {magic:#x}"
    strings = blob[off_strings:]
    pos = off_struct
    path_stack = []
    out = []  # (node_path, prop_name, value_bytes)

    def read_str(b):
        return b.split(b"\0")[0].decode("utf-8", "replace")

    while True:
        tok = struct.unpack(">I", blob[pos:pos + 4])[0]
        pos += 4
        if tok == FDT_BEGIN_NODE:
            end = blob.index(b"\0", pos)
            name = blob[pos:end].decode("utf-8", "replace")
            path_stack.append(name)
            pos = (end + 1 + 3) & ~3
        elif tok == FDT_END_NODE:
            if path_stack:
                path_stack.pop()
        elif tok == FDT_PROP:
            plen, nameoff = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            val = blob[pos:pos + plen]
            pos = (pos + plen + 3) & ~3
            pname = read_str(strings[nameoff:])
            node = "/" + "/".join(p for p in path_stack if p)
            out.append((node, pname, val))
        elif tok in (FDT_NOP,):
            continue
        elif tok == FDT_END:
            break
    return out, totalsize, version


def main():
    path = sys.argv[1]
    props, totalsize, version = parse(path)
    print(f"# {path}: {totalsize} bytes, dtb v{version}")
    interesting = {
        "model", "compatible", "stdout-path", "bootargs", "serial-number",
        "reg", "status", "clock-frequency", "current-speed",
    }
    for node, name, val in props:
        if name in ("model", "compatible", "stdout-path", "bootargs"):
            v = val.split(b"\0")[0].decode("utf-8", "replace")
            print(f"{node}  {name} = {v!r}")
        elif name == "memory" or node.endswith("/memory"):
            if name == "reg":
                words = struct.unpack(f">{len(val)//4}I", val)
                print(f"{node}  reg = {[hex(w) for w in words]}")
    # serial/console + chosen + memory nodes fully
    shown = set()
    for node, name, val in props:
        if name in interesting and (node.endswith("serial@") or
                                    "serial@" in node or node == "/chosen"):
            key = (node, name)
            if key in shown:
                continue
            shown.add(key)
            if name in ("reg", "clock-frequency", "current-speed"):
                words = struct.unpack(f">{len(val)//4}I", val)
                print(f"{node}  {name} = {[hex(w) for w in words]}")
            else:
                v = val.split(b"\0")[0].decode("utf-8", "replace")
                print(f"{node}  {name} = {v!r}")


if __name__ == "__main__":
    main()
