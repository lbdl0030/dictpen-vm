#!/usr/bin/env python3
"""Minimal TCP serial console client for the dictpen VM.

Usage:
  python scripts/console.py                 # interactive passthrough (Ctrl+] quits)
  python scripts/console.py -c "command"    # run one command, print output, exit
  python scripts/console.py -f cmds.txt     # run commands from file (one per line)
  python scripts/console.py --wait-boot     # just wait for the shell prompt and exit

Exit code is that of the last remote command (124 on timeout, 125 if the
device shell never answered).  This is what makes the console usable as a
fallback transport for scripts/dpctl.py when SSH is not up yet.

Env: DICTPEN_SERIAL_HOST/PORT (default 127.0.0.1:4444).
"""
import os
import re
import socket
import sys
import time

HOST = os.environ.get("DICTPEN_SERIAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("DICTPEN_SERIAL_PORT", "4444"))
RC_RX = re.compile(rb"DICTPEN_PROMPT_END rc=(\d+)")
RC_TIMEOUT = 124


class Serial:
    def __init__(self, host=HOST, port=PORT, timeout=120):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(2.0)
        self.buf = b""
        self.timeout = timeout

    def read_some(self):
        try:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("serial closed")
            self.buf += data
            if b"\x1b[6n" in data:  # terminal cursor query - answer it
                self.sock.sendall(b"\x1b[1;1R")
            return data
        except socket.timeout:
            return b""

    def read_until_re(self, pattern, timeout=None):
        deadline = time.time() + (timeout or self.timeout)
        rx = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        while time.time() < deadline:
            m = rx.search(self.buf)
            if m:
                out = self.buf[:m.start()]
                self.buf = self.buf[m.end():]
                return out
            self.read_some()
        out, self.buf = self.buf, b""
        return out

    def send(self, text):
        self.sock.sendall(text.encode())

    @staticmethod
    def _clean(raw):
        txt = raw.decode("utf-8", "replace")
        lines = [l for l in txt.splitlines()
                 if "DICTPEN_PROMPT" not in l and "\x1b" not in l]
        return "\n".join(lines)

    def run_cmd(self, cmd, timeout=None):
        """Run a command, return (output, exit_code) - 124 on timeout."""
        self.send(cmd + "\n")
        time.sleep(0.15)
        self.send('M=DICTPEN_PROMPT; echo "${M}_END rc=$?"\n')
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            m = RC_RX.search(self.buf)
            if m:
                out = self.buf[:m.start()]
                self.buf = self.buf[m.end():]
                return self._clean(out), int(m.group(1))
            self.read_some()
        out, self.buf = self.buf, b""
        return self._clean(out), RC_TIMEOUT


def main():
    args = sys.argv[1:]
    timeout = None
    if "--timeout" in args:
        i = args.index("--timeout")
        timeout = float(args[i + 1])
        del args[i:i + 2]

    s = Serial()
    time.sleep(1)
    # drain whatever banner/prompt state the console is in
    s.send("\n")
    deadline = time.time() + 3
    while time.time() < deadline:
        s.read_some()

    if not args or args[0] == "--wait-boot":
        out, rc = s.run_cmd("echo BOOT_SHELL_READY", timeout)
        print(out)
        return rc

    if args[0] == "-c":
        cmds = [args[1]]
    elif args[0] == "-f":
        cmds = [l.rstrip("\n") for l in open(args[1], encoding="utf-8") if l.strip()]
    else:
        # interactive passthrough
        import threading
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                data = s.read_some()
                if data:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        print(f"connected to {HOST}:{PORT} (Ctrl+] to quit)")
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch == "]":
                break
            s.send(ch.replace("\r", "\n") if ch == "\r" else ch)
        stop.set()
        return 0

    rc = 0
    for cmd in cmds:
        print(f"$ {cmd}")
        out, rc = s.run_cmd(cmd, timeout)
        print(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())