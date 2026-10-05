#!/usr/bin/env python3
"""Locate the virgl_3d gate inside virtio_gpu_dumb_create in the aarch64 .ko.

The stripped module has no symbol table; we find the gating code by pattern:
dumb_create is the mode_config .dumb_create member.  We search for the
immediate -19 (movn w0, #19) return in a small function that also calls
drm_gem_dumb_create or references the has_virgl_3d flag.
"""
import re
import struct
import sys
import capstone

PATH = r"tools\kernel-root-gui\lib\modules\6.1.0-50-arm64\kernel\drivers\gpu\drm\virtio\virtio-gpu.ko"


def main():
    d = open(PATH, "rb").read()
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.detail = True

    # Find the string "has_virgl_3d" and its file offset
    sv = d.find(b"has_virgl_3d")
    print("has_virgl_3d string at:", hex(sv))

    # The module is ET_REL; vaddrs are per-section.  Section headers exist?
    e_shoff = struct.unpack_from("<Q", d, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", d, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", d, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", d, 0x3E)[0]
    print(f"shoff={e_shoff:#x} shentsize={e_shentsize} shnum={e_shnum}")
    if not e_shoff:
        print("no section table; falling back to raw scan")
        return

    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        vals = struct.unpack_from("<IIQQQQIIQQ", d, off)
        secs.append(dict(name_off=vals[0], typ=vals[1], addr=vals[3],
                         offset=vals[4], size=vals[5], link=vals[6]))
    shstr = secs[e_shstrndx]
    for s in secs:
        end = d.index(b"\0", shstr["offset"] + s["name_off"])
        s["name"] = d[shstr["offset"] + s["name_off"]:end].decode(errors="replace")

    text = next(s for s in secs if s["name"] == ".text")
    print(f".text: vaddr {text['addr']:#x} off {text['offset']:#x} size {text['size']:#x}")

    # Find references to the has_virgl_3d string inside .text (adrp+add pairs
    # resolving to the string's vaddr).
    ro = next((s for s in secs if s["name"].startswith(".rodata") and
               s["offset"] <= sv < s["offset"] + s["size"]), None)
    sva = (ro["addr"] + (sv - ro["offset"])) if ro else sv
    print("has_virgl_3d vaddr:", hex(sva))

    # adrp xN, page ; add xN, xN, #:off — compute candidates
    refs = []
    for off in range(text["offset"], text["offset"] + text["size"] - 4, 4):
        insns = list(md.disasm(d[off:off + 8], text["addr"] + (off - text["offset"])))
        if len(insns) >= 2 and insns[0].mnemonic == "adrp":
            # page of target
            page = (insns[0].op_str.split("#")[1].split(",")[0].strip()
                    if "," in insns[0].op_str else None)
    # Simpler: just disassemble everything, print mnemonics mentioning x regs
    # and look for the sequence around a movn w0, #19.
    for off in range(text["offset"], text["offset"] + text["size"] - 4, 4):
        ins = next(md.disasm(d[off:off + 4], text["addr"] + (off - text["offset"])), None)
        if ins and ins.mnemonic == "movn" and "w0" in ins.op_str and "#19" in ins.op_str:
            # print function-ish context
            print(f"movn w0,#19 at vaddr {ins.address:#x} (file {off:#x})")
            for j in range(max(text["offset"], off - 0x20),
                           min(text["offset"] + text["size"], off + 0x20), 4):
                i2 = next(md.disasm(d[j:j + 4], text["addr"] + (j - text["offset"])), None)
                if i2:
                    print(f"   {i2.address:#x}  {i2.mnemonic} {i2.op_str}")


if __name__ == "__main__":
    main()
