#!/bin/sh
# 一键启动（等价于 python3 scripts/dpctl.py up）。QEMU 参数只在 dpctl.py 里，
# 固件是 packs/<名字>/ 下可拆卸的包，启动时用 --pack 选（框架不含任何机型）。
cd "$(dirname "$0")/.." || exit 1
exec python3 scripts/dpctl.py up "$@"