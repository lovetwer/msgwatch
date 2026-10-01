"""日志初始化：控制台 + 滚动文件。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO", log_file: str = "", max_mb: int = 5, backup_count: int = 3) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    # 避免重复初始化
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            fh = RotatingFileHandler(
                log_file, maxBytes=int(max_mb) * 1024 * 1024, backupCount=int(backup_count), encoding="utf-8"
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning("日志文件不可用(%s)：仅输出到控制台", e)

    # 降低第三方库的噪声
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
