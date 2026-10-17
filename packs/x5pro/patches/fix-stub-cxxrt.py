#!/usr/bin/env python3
"""把「全导出桩化」生成的桩库里被一起抹掉的 C++ 运行期符号（std::*/__gnu_cxx::/
operator new/delete/__cxa_*/_Unwind_*）的原始指令恢复回来。

为什么需要：libyocr.so / libYoudaoStitch.so 是用旧版 GCC 编的，里面带着
libstdc++ 模板实例的 **弱定义**（如 std::__detail::_Map_base<...>::operator[]）。
别的 .so 对这些符号只有 UND 引用 —— 全进程就靠这两个库提供实现。桩化时把所有
函数体统一换成 `mov r0,#0; bx lr`，于是全应用的
`unordered_map<string,string>::operator[]` 都返回 0，
调用方紧接着对 0 做 std::string::assign → SEGV（x5pro 点首页卡片必崩）。

用法：python fix-stub-cxxrt.py <原始.so> <桩.so> <输出.so>
只改 .text 内的字节，ELF 头/符号表/init_array 的桩化改造一概不动，
所以"库存在但不工作"的语义保持不变（OCR 入口仍返回 -1/0）。
"""
import re
import struct
import sys

# libstdc++ / C++ ABI 的符号（不是本库的业务 API）
RUNTIME = re.compile(r"^("
    r"_ZNK?(St|Ss|Sa|__cxx11)"        # std:: 成员函数
    r"|_ZNK?9__gnu_cxx"               # __gnu_cxx::
    r"|_ZSt"                          # std:: 自由函数
    r"|_Znwm|_Znwj|_Znaj|_Znam|_ZdlPv|_ZdaPv|_ZdlPvj|_ZdaPvj"  # new/delete
    r"|__cxa_|_Unwind_|_ZdlPv"        # ABI 运行时
    r")")


def parse(path):
    d = open(path, "rb").read()
    e_shoff, = struct.unpack_from("<I", d, 0x20)
    esh, enum, shstrndx = struct.unpack_from("<HHH", d, 0x2e)
    raw = [struct.unpack_from("<IIIIIIIIII", d, e_shoff + k * esh) for k in range(enum)]
    shstr = raw[shstrndx]
    named = []
    for s in raw:
        nm = d[shstr[4] + s[0]:d.index(b"\0", shstr[4] + s[0])].decode()
        named.append((nm, s))
    ds = [s for nm, s in named if nm == ".dynsym"][0]
    stroff = [s for nm, s in named if nm == ".dynstr"][0][4]
    text = [s for nm, s in named if nm == ".text"][0]
    syms = []
    for k in range(ds[5] // 16):
        st_name, st_value, st_size, st_info, st_other, st_shndx = \
            struct.unpack_from("<IIIBBH", d, ds[4] + k * 16)
        if not st_name:
            continue
        nm = d[stroff + st_name:d.index(b"\0", stroff + st_name)].decode()
        syms.append(dict(name=nm, value=st_value, size=st_size,
                         bind=st_info >> 4, typ=st_info & 3, shndx=st_shndx))
    return d, syms, dict(off=text[4], addr=text[3], size=text[5])


def main():
    orig_p, stub_p, out_p = sys.argv[1:4]
    od, osym, otext = parse(orig_p)
    sd, ssym, stext = parse(stub_p)
    assert len(od) == len(sd), "桩库大小与原库不一致，无法就地回填"
    assert (otext["addr"], otext["size"]) == (stext["addr"], stext["size"]), ".text 布局变了"
    byname = {s["name"]: s for s in ssym}
    restored = hits = skipped = 0
    for s in osym:
        if s["typ"] != 2 or s["shndx"] == 0 or not RUNTIME.match(s["name"]):
            continue
        t = byname.get(s["name"])
        if not t or t["value"] != s["value"]:
            skipped += 1
            continue
        lo, hi = s["value"], s["value"] + max(s["size"], 0)
        if hi <= lo:
            continue
        f0 = otext["off"] + (lo - otext["addr"])
        if not (otext["off"] <= f0 and f0 + (hi - lo) <= otext["off"] + otext["size"]):
            skipped += 1          # 不在 .text（比如 PLT/ veneer），不动
            continue
        if od[f0:hi - lo + f0] == sd[f0:hi - lo + f0]:
            continue              # 本来就没被改写
        hits += 1
        sd = sd[:f0] + od[f0:f0 + (hi - lo)] + sd[f0 + (hi - lo):]
        restored += hi - lo
    out = bytearray(sd)
    open(out_p, "wb").write(out)
    print(f"{stub_p}: 回填 {hits} 个运行期符号 / {restored} 字节，"
          f"跳过 {skipped}（布局不符）")


if __name__ == "__main__":
    main()
