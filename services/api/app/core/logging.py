"""日志配置。

Agent 项目的日志比普通服务更重要：一次失败请求涉及「模型调用 → 工具执行 → 再调用」
多跳，没有结构化日志根本无法定位是哪一跳出了问题。
"""

from __future__ import annotations

import logging
import sys
from typing import ClassVar

_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    """本地开发用彩色日志，生产环境（json 模式）不带颜色。"""

    COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        record.name = f"\033[35m{record.name}\033[0m"
        return super().format(record)


def setup_logging(level: str = "INFO", *, colorful: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(asctime)s %(levelname)s %(name)s | %(message)s"
    handler.setFormatter(
        _ColorFormatter(fmt, datefmt="%H:%M:%S") if colorful else logging.Formatter(fmt)
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 第三方库太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
