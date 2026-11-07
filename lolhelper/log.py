"""Shared logger.

AstrBot plugins must log through `astrbot.api.logger`; every module receives it from
main.py and falls back to the same object. Outside AstrBot (plain scripts) a minimal
console logger is used instead.
"""
from __future__ import annotations

import sys
import time
import traceback


class ConsoleLogger:
    """Tiny stand-in used only when astrbot is not importable."""

    def _out(self, level: str, msg, *args) -> None:
        text = str(msg) % args if args else str(msg)
        print(f"{time.strftime('%H:%M:%S')} {level} {text}", file=sys.stderr)

    def debug(self, msg, *args, **kwargs) -> None:
        pass

    def info(self, msg, *args, **kwargs) -> None:
        self._out("INFO", msg, *args)

    def warning(self, msg, *args, **kwargs) -> None:
        self._out("WARNING", msg, *args)

    def error(self, msg, *args, **kwargs) -> None:
        self._out("ERROR", msg, *args)

    def exception(self, msg, *args, **kwargs) -> None:
        self._out("ERROR", msg, *args)
        traceback.print_exc()


try:
    from astrbot.api import logger as default_logger
except ImportError:  # running outside AstrBot
    default_logger = ConsoleLogger()

