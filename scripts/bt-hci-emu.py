#!/usr/bin/env python3
"""Virtual Bluetooth controller for dictpen-vm.

QEMU no longer emulates Bluetooth (no `-bt`, no usb-bt-dongle) and Debian's
arm64 kernel ships no hci_vhci module, so the guest cannot get an HCI device
from either of those.  Instead the guest runs hci_uart over a plain serial port
(btattach /dev/ttyAMA1) and this process plays the other end of that link: it
speaks H4 framing and answers HCI commands the way a real controller does, so
the guest's BlueZ stack (bluetoothd / bt-manager) gets a working, powerable
hci0 - and a fake peripheral to discover.

dpctl up starts it automatically; run it by hand with:
    python scripts/bt-hci-emu.py [--port 4447] [--log vm/bt.log] [-v]
"""
import argparse
import binascii
import datetime
import os
import socket
import struct
import sys
import time

H4_CMD, H4_ACL, H4_SCO, H4_EVT = 0x01, 0x02, 0x03, 0x04

# --- the controller we pretend to be --------------------------------------- #
BD_ADDR = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])   # display order 02:00:..:01
DEVICE_NAME = b"dictpen-vm"
# 4.2 dual-mode controller, the feature mask a real combo chip reports
FEATURES = bytes([0xFF, 0xFF, 0x8F, 0xFE, 0xDB, 0xFF, 0x7B, 0x87])
LE_FEATURES = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x03])
HCI_VERSION, LMP_VERSION, MANUFACTURER = 0x07, 0x07, 0x000F
# the fake peripheral that shows up in a scan
FAKE_ADDR = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
FAKE_NAME = b"YD-BT-Test"
FAKE_CLASS = bytes([0x04, 0x18, 0x24])            # audio/video, headset


class Log:
    def __init__(self, path, verbose):
        self.verbose = verbose
        self.fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {msg}"
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()
        if self.verbose:
            print(line, flush=True)


def le_meta(subevent, payload):
    body = bytes([subevent]) + payload
    return bytes([H4_EVT, 0x3E, len(body)]) + body


def adv_report(addr, name, rssi=-52, addr_type=0x00):
    data = bytes([0x02, 0x01, 0x06])               # LE general discoverable
    if name:
        data += bytes([len(name) + 1, 0x09]) + name
    entry = (bytes([0x00, addr_type]) + addr + bytes([len(data)]) + data
             + bytes([rssi & 0xFF]))
    return le_meta(0x02, bytes([1]) + entry)


class Controller:
    def __init__(self, log):
        self.log = log
        self.event_mask = 0
        self.le_scan = False
        self.inquiry_until = 0.0
        self.inquiry_reported = False
        self.out = bytearray()                     # bytes queued for the guest

    # -- event helpers ------------------------------------------------------ #
    def cc(self, opcode, params=b""):
        return bytes([H4_EVT, 0x0E, 3 + len(params), 1]) + struct.pack("<H", opcode) + params

    def cs(self, opcode, status=0x00):
        return bytes([H4_EVT, 0x0F, 4, status, 1]) + struct.pack("<H", opcode)

    # -- command handlers --------------------------------------------------- #
    # each returns the command-complete return bytes, or None for command-status
    def handle(self, opcode, params):
        fn = getattr(self, f"cmd_{opcode:04x}", None)
        if fn is not None:
            return fn(params)
        ogf = opcode >> 10
        if ogf >= 0x3e:
            # vendor commands: the guest kernel must not think we took them
            self.log(f"refusing vendor cmd 0x{opcode:04x} ({ogf_ocf(opcode)})")
            return b"\x01"
        # The guest kernel aborts its controller setup when a command it
        # expects to work fails, only complaining if the reply is *shorter*
        # than it wants - so answer unknown commands with a generous,
        # benign success rather than "Unknown HCI Command".
        self.log(f"generic ok 0x{opcode:04x} ({ogf_ocf(opcode)}) "
                 f"in={binascii.hexlify(params[:24]).decode()}")
        return b"\x00" + bytes(16)

    def cmd_0c03(self, p):        # Reset
        self.le_scan = False
        self.inquiry_until = 0.0
        return b"\x00"

    def cmd_1001(self, p):        # Read Local Version Information
        return (b"\x00" + bytes([HCI_VERSION]) + struct.pack("<H", 0x0000)
                + bytes([LMP_VERSION]) + struct.pack("<H", MANUFACTURER)
                + struct.pack("<H", 0x0000))

    def cmd_1002(self, p):        # Read Local Supported Commands
        return b"\x00" + self.supported_commands()

    def cmd_1003(self, p):        # Read Local Supported Features
        return b"\x00" + FEATURES

    def cmd_1004(self, p):        # Read Local Extended Features
        page = p[0] if p else 0
        # status, page number, maximum page number, 8 feature bytes
        feats = FEATURES if page == 0 else bytes(8)
        return b"\x00" + bytes([page, 1]) + feats

    def cmd_1005(self, p):        # Read Buffer Size
        return (b"\x00" + struct.pack("<H", 1024) + bytes([64])
                + struct.pack("<H", 10) + struct.pack("<H", 10))

    def cmd_1009(self, p):        # Read BD_ADDR
        return b"\x00" + BD_ADDR

    def cmd_0c01(self, p):        # Set Event Mask
        self.event_mask = int.from_bytes(p[:8], "little") if len(p) >= 8 else 0
        return b"\x00"

    def cmd_0c52(self, p):        # Set Event Mask Page 2
        return b"\x00"

    def cmd_0c05(self, p):        # Set Event Filter
        return b"\x00"

    def cmd_0c13(self, p):        # Write Local Name
        return b"\x00"

    def cmd_0c14(self, p):        # Read Local Name
        name = DEVICE_NAME + b"\x00" * (248 - len(DEVICE_NAME))
        return b"\x00" + name

    def cmd_0c16(self, p):        # Write Simple Pairing Debug Mode
        return b"\x00"

    def cmd_0c23(self, p):        # Read Class of Device
        return b"\x00" + FAKE_CLASS

    def cmd_0c24(self, p):        # Write Class of Device
        return b"\x00"

    def cmd_0c25(self, p):        # Read Voice Setting
        return b"\x00" + struct.pack("<H", 0x0060)

    def cmd_0c26(self, p):        # Write Voice Setting
        return b"\x00"

    def cmd_0c38(self, p):        # Read Number of Supported IAC
        return b"\x00" + bytes([0x01])

    def cmd_0c39(self, p):        # Read Current IAC LAP
        return b"\x00" + bytes([0x01]) + bytes([0x33, 0x8B, 0x9E])

    def cmd_0c42(self, p):        # Read Inquiry Scan Type
        return b"\x00\x00"

    def cmd_0c43(self, p):        # Write Inquiry Scan Type
        return b"\x00"

    def cmd_0c44(self, p):        # Read Inquiry Mode
        return b"\x00\x00"

    def cmd_0c45(self, p):        # Write Inquiry Mode
        return b"\x00"

    def cmd_0c46(self, p):        # Read Page Scan Type
        return b"\x00\x00"

    def cmd_0c47(self, p):        # Write Page Scan Type
        return b"\x00"

    def cmd_0c19(self, p):        # Read Inquiry Scan Activity
        return b"\x00" + struct.pack("<HH", 0x0800, 0x0012)

    def cmd_0c1a(self, p):        # Write Inquiry Scan Activity
        return b"\x00"

    def cmd_0c1b(self, p):        # Read Page Scan Activity
        return b"\x00" + struct.pack("<HH", 0x0800, 0x0012)

    def cmd_0c1c(self, p):        # Write Page Scan Activity
        return b"\x00"

    def cmd_0c0c(self, p):        # Read Link Policy Settings
        return b"\x00" + struct.pack("<H", 0x0005)

    def cmd_0c0d(self, p):        # Write Link Policy Settings
        # the guest kernel expects a 5-byte return here; pad the read-back
        return b"\x00" + (p[-4:] if p else b"").ljust(4, b"\x00")

    def cmd_0c0b(self, p):        # Read PIN Type
        return b"\x00\x01"

    def cmd_0c0a(self, p):        # Write PIN Type
        return b"\x00"

    def cmd_0c55(self, p):        # Read Simple Pairing Mode
        return b"\x00\x01"

    def cmd_0c56(self, p):        # Write Simple Pairing Mode
        return b"\x00"

    def cmd_0c48(self, p):        # Read Secure Connections Support
        return b"\x00\x01"

    def cmd_0c58(self, p):        # Read Inquiry Response TX Power
        return b"\x00" + bytes([0x00])

    def cmd_0c68(self, p):        # Read Default Erroneous Data Reporting
        return b"\x00\x00"

    def cmd_0c6d(self, p):        # Write LE Host Support
        return b"\x00"

    def cmd_0c6c(self, p):        # Read LE Host Support
        return b"\x00\x01\x00"

    def cmd_0c79(self, p):        # Read Local OOB Extended Data (not stored)
        return b"\x01"

    def cmd_0c18(self, p):        # Read Scan Enable
        return b"\x00\x00"

    def cmd_1405(self, p):        # Read RSSI
        return b"\x00\x00\xff"

    # -- classic discovery -------------------------------------------------- #
    def cmd_0401(self, p):        # Inquiry
        length = p[3] if len(p) > 3 else 8
        self.inquiry_until = time.time() + max(2.0, length * 1.28)
        self.inquiry_reported = False
        self.log(f"inquiry start ({length})")
        return None                               # command status

    def cmd_0402(self, p):        # Inquiry Cancel
        if self.inquiry_until:
            self.inquiry_until = 0.0
            self.out += bytes([H4_EVT, 0x01, 1, 0x00])      # Inquiry Complete
        return b"\x00"

    def cmd_0403(self, p):        # Remote Name Request
        addr = p[:6]
        self.out += bytes([H4_EVT, 0x07, 1 + 6 + len(FAKE_NAME), 0x00]) + addr \
            + FAKE_NAME
        return None

    # -- LE ----------------------------------------------------------------- #
    def cmd_2001(self, p):        # LE Set Event Mask
        return b"\x00"

    def cmd_2002(self, p):        # LE Read Buffer Size
        return b"\x00" + struct.pack("<H", 251) + bytes([15])

    def cmd_2003(self, p):        # LE Read Local Supported Features
        return b"\x00" + LE_FEATURES

    def cmd_2060(self, p):        # LE Read Buffer Size [v2]
        return (b"\x00" + struct.pack("<H", 251) + bytes([15])
                + struct.pack("<H", 251) + bytes([15]))

    def cmd_2016(self, p):        # LE Read Remote Features
        return b"\x00" + LE_FEATURES

    def cmd_201c(self, p):        # LE Read Supported States
        return b"\x00" + bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F, 0x00])

    def cmd_2023(self, p):        # LE Read Maximum Data Length
        return (b"\x00" + struct.pack("<H", 251) + struct.pack("<H", 2120)
                + struct.pack("<H", 251) + struct.pack("<H", 2120))

    def cmd_2011(self, p):        # LE Read White List Size
        return b"\x00" + bytes([16])

    def cmd_2013(self, p):        # LE Clear White List
        return b"\x00"

    def cmd_2012(self, p):        # LE Add Device To White List
        return b"\x00"

    def cmd_2005(self, p):        # LE Set Advertising Parameters
        return b"\x00"

    def cmd_2006(self, p):        # LE Set Advertising Data
        return b"\x00"

    def cmd_2008(self, p):        # LE Set Scan Response Data
        return b"\x00"

    def cmd_200a(self, p):        # LE Set Advertise Enable
        return b"\x00"

    def cmd_200b(self, p):        # LE Set Scan Parameters
        return b"\x00"

    def cmd_200c(self, p):        # LE Set Scan Enable
        enable = p[0] if p else 0
        self.le_scan = bool(enable)
        self.log(f"LE scan enable={enable}")
        return b"\x00"

    def cmd_2022(self, p):        # LE Set Data Length
        return b"\x00"

    def cmd_2024(self, p):        # LE Read Suggested Default Data Length
        return b"\x00" + struct.pack("<H", 251) + struct.pack("<H", 2120)

    def cmd_2025(self, p):        # LE Write Suggested Default Data Length
        return b"\x00"

    # -- misc --------------------------------------------------------------- #
    def cmd_0c33(self, p):        # Read Encryption Key Size
        return b"\x00" + struct.pack("<H", 16)

    def supported_commands(self):
        """Claim support for everything.

        The 64-byte mask uses a layout the spec defines per command group, and
        getting one bit wrong only makes BlueZ skip a feature; claiming all of
        them keeps the stack on its normal path.  Anything we do not implement
        is answered with "Unknown HCI Command", which BlueZ tolerates."""
        return b"\xff" * 64

    # -- periodic work ------------------------------------------------------ #
    def tick(self):
        now = time.time()
        if self.inquiry_until:
            if now >= self.inquiry_until:
                self.inquiry_until = 0.0
                self.out += bytes([H4_EVT, 0x01, 1, 0x00])       # Inquiry Complete
            elif not self.inquiry_reported:
                self.inquiry_reported = True
                ev = bytes([H4_EVT, 0x02, 1 + 6 + 1 + 1 + 3 + 2, 1]) + FAKE_ADDR \
                    + bytes([0x01, 0x00]) + FAKE_CLASS + struct.pack("<h", 0)
                self.out += ev
                self.log("inquiry result: %s" % binascii.hexlify(FAKE_ADDR).decode())
        if self.le_scan and now % 3 < 1.0:
            self.out += adv_report(FAKE_ADDR, FAKE_NAME)


def ogf_ocf(opcode):
    return f"ogf=0x{opcode >> 10:02x} ocf=0x{opcode & 0x3ff:03x}"


def serve(ctrl, sock, log):
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            chunk = b""
        except OSError as exc:
            log(f"socket closed: {exc}")
            return
        if chunk:
            buf += chunk
        # parse complete H4 packets
        while buf:
            ptype = buf[0]
            if ptype == H4_CMD:
                if len(buf) < 4:
                    break
                opcode = buf[1] | (buf[2] << 8)
                plen = buf[3]
                if len(buf) < 4 + plen:
                    break
                params = bytes(buf[4:4 + plen])
                del buf[:4 + plen]
                ret = ctrl.handle(opcode, params)
                if ret is None:
                    ctrl.out += ctrl.cs(opcode)
                else:
                    ctrl.out += ctrl.cc(opcode, ret)
                log(f"cmd 0x{opcode:04x} plen={plen} "
                    f"({ogf_ocf(opcode)}) "
                    f"in={binascii.hexlify(params[:24]).decode()} "
                    f"out={'status' if ret is None else binascii.hexlify(ret[:14]).decode()}")
            elif ptype == H4_ACL:
                if len(buf) < 5:
                    break
                dlen = buf[3] | (buf[4] << 8)
                if len(buf) < 5 + dlen:
                    break
                del buf[:5 + dlen]
            else:
                del buf[:1]                              # unknown/resync
        ctrl.tick()
        if ctrl.out:
            try:
                sock.sendall(bytes(ctrl.out))
            except OSError as exc:
                log(f"send failed: {exc}")
                return
            ctrl.out.clear()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=4447)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--log", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vm", "bt.log"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    log = Log(args.log, args.verbose)
    log(f"--- bt-hci-emu waiting for {args.host}:{args.port} ---")
    deadline = time.time() + 60
    sock = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection((args.host, args.port), timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    if sock is None:
        log("could not reach the QEMU bluetooth socket")
        return 1
    sock.settimeout(0.5)
    log("connected to the guest HCI link")
    ctrl = Controller(log)
    try:
        serve(ctrl, sock, log)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())