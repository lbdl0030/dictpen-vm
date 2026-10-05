#!/usr/bin/env python3
"""Disassemble a symbol from an ARM32 shared object (for falcon DRM analysis)."""
import struct
import sys
import capstone


def load_elf(path):
    d = open(path, "rb").read()
    assert d[:4] == b"\x7fELF"
    e_shoff, = struct.unpack_from("<I", d, 0x20)   # ELF32
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x2e)
    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name, typ, flags, addr, offset, size, link, info, align, entsize = struct.unpack_from("<IIIIIIIIII", d, off)
        secs.append(dict(name_off=name, type=typ, addr=addr, offset=offset, size=size,
                         link=link, entsize=entsize, flags=flags))
    shstr = secs[e_shstrndx]
    for s in secs:
        end = d.index(b"\0", shstr["offset"] + s["name_off"])
        s["name"] = d[shstr["offset"] + s["name_off"]:end].decode()
    # dynsym symbols
    syms = {}
    for s in secs:
        if s["type"] == 11:  # SHT_DYNSYM
            strtab = secs[s["link"]]
            n = s["size"] // 16
            for i in range(n):
                off = s["offset"] + i * 16
                st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from("<IIIBBH", d, off)
                if not st_name:
                    continue
                end = d.index(b"\0", strtab["offset"] + st_name)
                nm = d[strtab["offset"] + st_name:end].decode(errors="replace")
                if st_value:
                    syms[nm] = (st_value, st_size)
    return d, secs, syms


def find_str_vaddr(d, secs, needle):
    for s in secs:
        if s["name"] == ".rodata":
            i = d.find(needle, s["offset"], s["offset"] + s["size"])
            if i != -1:
                return s["addr"] + (i - s["offset"])
    return None


def main():
    path, symbol = sys.argv[1], sys.argv[2]
    d, secs, syms = load_elf(path)
    addr, size = syms[symbol]
    thumb = addr & 1
    addr &= ~1
    print(f"{symbol}: vaddr={addr:#x} size={size} thumb={bool(thumb)}")

    text = next(s for s in secs if s["name"] == ".text")
    file_off = text["offset"] + (addr - text["addr"])
    code = d[file_off:file_off + max(size, 0x400)]

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM)
    md.detail = False
    for ins in md.disasm(code, addr):
        print(f"{ins.address:#08x}  {ins.mnemonic:10s} {ins.op_str}")


if __name__ == "__main__":
    main()
