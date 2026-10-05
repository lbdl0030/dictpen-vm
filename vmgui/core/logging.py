"""日志：统一入口，可同时输出到 stderr 与文件。"""
from __future__ import annotations

import logging
import sys

_LOGGER = None


def setup(level: str = "info", path: str = "") -> logging.Logger:
    global _LOGGER
    logger = logging.getLogger("vmgui")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if path:
        try:
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as e:
            logger.warning("无法打开日志文件 %s: %s", path, e)
    logger.propagate = False
    _LOGGER = logger
    return logger


def get(name: str = "") -> logging.Logger:
    base = _LOGGER or setup()
    return base.getChild(name) if name else base