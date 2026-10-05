#!/usr/bin/env python3
"""Scan a stripped .ko for immediate-return (error) instruction patterns and
print candidates with context, to locate a specific function like dumb_create."""
import struct
import sys
import capstone

THUMB = False


def load(path):
    d = open(path, "rb").read()
    assert d[:4] == b"\x7fELF"
    return d


def scan(path):
    d = load(path)
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.detail = False
    out = []
    for k in range(0, len(d) - 4, 4):
        w, = struct.unpack_from("<I", d, k)
        # mov r0, #imm  (cond 001 1101? no: e3a00000 family, rd=0)
        if (w & 0x0FFF0FF0) == 0x03A00000:  # mov r0, #imm (ARM, rd=r0)
            imm = ((w >> 4) & 0xF000) | (w & 0xFFF)
            out.append((k, w, "mov r0,#%d" % imm))
        # mvn r0, #imm
        elif (w & 0x0FFF0FF0) == 0x03E00000:
            imm = ((w >> 4) & 0xF000) | (w & 0xFFF)
            out.append((k, w, "mvn r0,#%d" % (~imm & 0xFFFFFFFF)))
        # movs r0, #imm
        elif (w & 0x0FFF0FF0) == 0x03B00000:
            imm = ((w >> 4) & 0xF000) | (w & 0xFFF)
            out.append((k, w, "movs r0,#%d" % imm))
    return out


def main():
    path = sys.argv[1]
    hits = scan(path)
    d = open(path, "rb").read()
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    interesting = []
    for k, w, desc in hits:
        # print surrounding disasm for manual inspection
        ctx = []
        for j in range(max(0, k - 16), min(len(d) - 4, k + 32), 4):
            for ins in md.disasm(d[j:j + 4], j):
                ctx.append(f"{ins.address:#x} {ins.mnemonic} {ins.op_str}")
        interesting.append((k, desc, ctx))
    for k, desc, ctx in interesting:
        print(f"--- {k:#x} {desc}")
        for line in ctx:
            print("   ", line)


if __name__ == "__main__":
    main()
