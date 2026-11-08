"""日志配置。"""

from __future__ import annotations

import logging
import sys


def configure_logging(level_name: str = "INFO") -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger().setLevel(level)
    # 开启 log_search 时 OR-Tools 日志可能非常冗长。
    logging.getLogger("ortools").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
