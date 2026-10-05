#!/usr/bin/env python3
"""Host-side fake youdao activation `verify` endpoint (MITM helper).

Serves HTTPS on 0.0.0.0:443 using a self-signed cert that covers
fanyidan.youdao.com / fanyiegg-test.youdao.com, records every request to
a log file, and answers with a synthetic {"code":0,"license":"1:<172>"}
payload so the pen app's activation state machine advances.

Usage: python scripts/mitm-verify.py [--port 443] [--log vm/mitm/requests.log]
"""
import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CERT = os.path.join(ROOT, "vm", "mitm", "cert.pem")
KEY = os.path.join(ROOT, "vm", "mitm", "key.pem")

# license == "1:" + 172 base64 chars == 174 bytes, matching EXPECT_LEN of
# restore_license.sh. Content is irrelevant: checkResponse() only tests the
# carrier field for zero.
_BODY = base64.b64encode(hashlib.sha256(b"dictpen-vm-mitm").digest() * 5).decode()
LICENSE = "1:" + _BODY[:172]

RESPONSE = json.dumps({"code": 0, "msg": "ok", "license": LICENSE})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.server.logpath, "a", encoding="utf-8") as fh:
            fh.write(f"=== {stamp} {self.command} {self.path}\n")
            for k, v in self.headers.items():
                fh.write(f"    {k}: {v}\n")
            if body:
                fh.write("    BODY: " + body.decode("utf-8", "replace") + "\n")
            fh.write("--- -> " + RESPONSE + "\n")
        payload = RESPONSE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--log", default=os.path.join(ROOT, "vm", "mitm", "requests.log"))
    args = ap.parse_args()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    ctx.set_alpn_protocols(["http/1.1"])
    # Guest libcurl may be old; relax the floor rather than fail the handshake.
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.logpath = args.log
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"[mitm] https://0.0.0.0:{args.port}  cert={CERT}")
    print(f"[mitm] license={LICENSE} ({len(LICENSE)}B)  log={args.log}")
    srv.serve_forever()


if __name__ == "__main__":
    main()