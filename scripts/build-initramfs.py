#!/usr/bin/env python3
"""Build the dictpen-vm initramfs (cpio newc, gzipped).

This is the **framework's** initramfs: busybox, /init, the kernel modules the VM
needs, and the vendor-neutral extras that virtualise absent hardware (the arm64
hostapd/iptables used by the virtual access point).  It contains **no firmware**
- no device image, no panel size, no display HAL.  Those come from the firmware
pack chosen at boot time: `dpctl up --pack <name>` merges `packs/<name>/patches/**`
and the pack-declared panel EDID into a per-pack initrd (see scripts/packlib.py),
so this file is built once and never changes when you switch devices.

Usage: python scripts/build-initramfs.py
"""
import gzip
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
BUSYBOX = os.path.join(TOOLS, "busybox-root", "usr", "bin", "busybox")
MODDIR = os.environ.get("KERNEL_ROOT", os.path.join(TOOLS, "kernel-root-gui"))
MODDIR = os.path.join(MODDIR, "lib", "modules")
INIT = os.path.join(ROOT, "scripts", "initramfs-init.sh")
OUT = os.path.join(ROOT, "images", "initramfs.cpio.gz")

MODULES = [
    "virtio_ring.ko", "virtio.ko", "virtio_pci_modern_dev.ko",
    "virtio_pci_legacy_dev.ko", "virtio_pci.ko", "virtio_blk.ko",
    "failover.ko", "net_failover.ko", "virtio_net.ko",
    "evdev.ko", "drm.ko", "drm_kms_helper.ko", "drm_shmem_helper.ko",
    "virtio_input.ko",
    # bochs-display: a linear-VRAM display whose scanout reads guest memory, so
    # drawing into the framebuffer is visible immediately (like the pen's panel)
    "ttm.ko", "drm_ttm_helper.ko", "drm_vram_helper.ko", "bochs.ko",
    # uinput: the pen UI wants a touchscreen, QEMU's mouse is not one
    "uinput.ko",
    "libcrc32c.ko", "crc32c_generic.ko", "crc32c_arm64.ko", "erofs.ko",
    "overlay.ko",
    # persistent state disk (ext4) and the vfat fallback
    "crc16.ko", "mbcache.ko", "jbd2.ko", "ext4.ko", "fat.ko",
    "nls_cp437.ko", "nls_iso8859-1.ko", "vfat.ko",
    "virtio-rng.ko",
    # Wi-Fi: the pen's stack (wpa_supplicant -Dnl80211 / wifi-manager) is
    # nl80211 based, so a mac80211_hwsim radio is enough to give it a real
    # wlan0 - the SDIO chip (atbm603x/bl_fdrv) needs hardware the VM lacks.
    "rfkill.ko", "cfg80211.ko", "libarc4.ko", "mac80211.ko", "mac80211_hwsim.ko",
    # Bluetooth: the guest stack wants hci0; with no QEMU BT emulation and no
    # hci_vhci module, hci_uart over a serial port (driven from the host) is
    # the way to publish one.
    "ecc.ko", "ecdh_generic.ko", "bluetooth.ko",
    "btintel.ko", "btbcm.ko", "btrtl.ko", "btqca.ko", "hci_uart.ko",
    # netfilter: the virtual AP is inside the guest, so forwarding its subnet
    # out through eth0 needs MASQUERADE (see the S00wifi-ap hook)
    "x_tables.ko", "ip_tables.ko",
    "nf_defrag_ipv4.ko", "nf_defrag_ipv6.ko", "nf_conntrack.ko", "nf_nat.ko",
    "iptable_filter.ko", "iptable_nat.ko", "xt_MASQUERADE.ko",
]

entries = []  # (name, mode, data, nlink)


def add_dir(name, mode=0o755):
    entries.append((name, mode | 0o40000, b"", 2))


def add_file(name, data, mode=0o755):
    entries.append((name, mode | 0o100000, data, 1))


def build_cpio(entries):
    out = bytearray()
    ino = 100
    for name, mode, data, nlink in entries:
        ino += 1
        nfield = struct.pack(">13I", ino, mode, 0, 0, nlink, 0, len(data),
                             0, 0, 0, 0, len(name) + 1, 0)
        out += b"070701" + b"".join(f"{v:08X}".encode() for v in struct.unpack(">13I", nfield))
        out += name.encode() + b"\0"
        out += b"\0" * ((4 - len(out) % 4) % 4)
        out += data
        out += b"\0" * ((4 - len(data) % 4) % 4)
    trailer = b"TRAILER!!!"
    out += b"070701" + b"0" * 104 + trailer + b"\0"
    out += b"\0" * ((4 - len(out) % 4) % 4)
    return bytes(out)


def main():
    add_dir("proc"), add_dir("sys"), add_dir("dev")
    add_dir("tmp"), add_dir("mnt"), add_dir("run"), add_dir("ro")
    add_dir("bin")
    pubkey = os.path.join(TOOLS, "ssh", "id_devvm.pub")
    init_txt = open(INIT, "r", encoding="utf-8").read()
    if os.path.exists(pubkey):
        init_txt = init_txt.replace("__DICTPEN_PUBKEY__",
                                    open(pubkey).read().strip())
    add_file("init", init_txt.encode("utf-8"))
    add_file("bin/busybox", open(BUSYBOX, "rb").read())

    ver = os.listdir(MODDIR)[0]
    add_dir("lib")
    add_dir("lib/modules")
    # The panel EDID lives here (the kernel loads it via
    # drm_kms_helper.edid_firmware): the framework only creates the directory,
    # the pack supplies lib/firmware/edid/pen.bin at boot (packlib.build_edid).
    add_dir("lib/firmware")
    add_dir("lib/firmware/edid")

    # Virtual access point: the pen's Wi-Fi chip runs AP mode in its own
    # firmware and the rootfs ships no hostapd, but the guest kernel is aarch64
    # and runs 64-bit binaries natively - so bundle Debian's arm64 hostapd and
    # the libraries it links against.  Vendor-neutral: whether a given pack wants
    # the AP is up to that pack ([adapt].ap).
    ap_src = os.path.join(TOOLS, "arm64-rootfs")
    ap_bins = [("usr/sbin/hostapd", "hostapd"),
               ("lib/ld-linux-aarch64.so.1", "ld-linux-aarch64.so.1")]
    ap_libs = ["libc.so.6", "libm.so.6", "libcrypto.so.3", "libssl.so.3",
               "libnl-3.so.200", "libnl-genl-3.so.200", "libnl-route-3.so.200"]
    if os.path.isdir(ap_src):
        add_dir("patches")
        add_dir("patches/ap")
        add_dir("patches/ap/lib")
        for rel, dst in ap_bins:
            src = os.path.join(ap_src, *rel.split("/"))
            if os.path.exists(src):
                add_file(f"patches/ap/{dst}", open(src, "rb").read())
        for lib in ap_libs:
            for sub in ("lib/aarch64-linux-gnu", "usr/lib/aarch64-linux-gnu"):
                src = os.path.join(ap_src, *sub.split("/"), lib)
                if os.path.exists(src):
                    add_file(f"patches/ap/lib/{lib}", open(src, "rb").read(),
                             0o644)
                    break
            else:
                print(f"warn: ap lib {lib} not found", file=sys.stderr)
        print("packed hostapd (aarch64) for the virtual access point")
    else:
        print("warn: tools/arm64-rootfs missing - no virtual AP",
              file=sys.stderr)

    # Giving the virtual AP an uplink needs NAT inside the guest, so bundle the
    # aarch64 iptables-legacy (libip*tc + libxtables + the few xtables plugins
    # the rules use) next to hostapd, plus an empty plugin directory marker.
    ipt_bins = [("usr/sbin/iptables-legacy", "iptables-legacy")]
    ipt_libs = ["libip4tc.so.2", "libip6tc.so.2", "libxtables.so.12"]
    ipt_plugins = ["libxt_standard.so", "libxt_MASQUERADE.so", "libxt_conntrack.so",
                   "libxt_state.so", "libxt_comment.so", "libxt_tcp.so",
                   "libxt_udp.so"]
    if os.path.isdir(ap_src):
        for rel, dst in ipt_bins:
            src = os.path.join(ap_src, *rel.split("/"))
            if os.path.exists(src):
                add_file(f"patches/ap/{dst}", open(src, "rb").read())
        for lib in ipt_libs:
            src = os.path.join(ap_src, "usr", "lib", "aarch64-linux-gnu", lib)
            if os.path.exists(src):
                add_file(f"patches/ap/lib/{lib}", open(src, "rb").read(), 0o644)
        plugins_src = os.path.join(ap_src, "usr", "lib", "aarch64-linux-gnu",
                                   "xtables")
        if os.path.isdir(plugins_src):
            add_dir("patches/ap/xtables")
            for plug in ipt_plugins:
                src = os.path.join(plugins_src, plug)
                if os.path.exists(src):
                    add_file(f"patches/ap/xtables/{plug}",
                             open(src, "rb").read(), 0o644)
        print("packed iptables-legacy (aarch64) for the AP uplink")

    for m in MODULES:
        hits = []
        for dirpath, _, files in os.walk(MODDIR):
            if m in files:
                hits.append(os.path.join(dirpath, m))
        if not hits:
            print(f"warn: module {m} not found", file=sys.stderr)
            continue
        rel = os.path.relpath(hits[0], MODDIR)  # <ver>/kernel/.../<m>.ko
        acc = "lib/modules"
        for p in rel.split(os.sep)[:-1]:
            acc = f"{acc}/{p}"
            add_dir(acc)
        target = "lib/modules/" + rel.replace(os.sep, "/")
        # prefer patched build of virtio-gpu (dumb_create bpp guard removed)
        if m == "virtio-gpu.ko":
            patched_ko = os.path.join(TOOLS, "kernel-root-gui", "lib", "modules",
                                      "6.1.0-50-arm64", "kernel", "drivers",
                                      "gpu", "drm", "virtio", "virtio-gpu.ko.patched")
            if os.path.exists(patched_ko):
                print(f"using patched {target}")
                add_file(target, open(patched_ko, "rb").read())
                continue
        add_file(target, open(hits[0], "rb").read())

    raw = build_cpio(entries)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(gzip.compress(raw, 6))
    print(f"{OUT}: {len(entries)} entries, {os.path.getsize(OUT)} bytes gz")


if __name__ == "__main__":
    main()
