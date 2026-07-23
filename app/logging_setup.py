"""Thiết lập logging tập trung. Ghi ra file + console.

Không có log tử tế thì khi yt-dlp hỏng hoặc encode fail lúc 3h sáng sẽ không debug được.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(level: str = "INFO", file: str | None = "logs/app.log") -> logging.Logger:
    logger = logging.getLogger("vrs")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Xoay vòng log 5MB x 5 file để không phình vô hạn.
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("vrs").getChild(name)
