"""Centralized logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from storage import data_file


def get_logger(name: str, filename: str | None = None) -> logging.Logger:
    """Return a logger, optionally writing to DATA_DIR/logs."""
    logger = logging.getLogger(name)
    if filename:
        path = data_file("logs") / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if not any(isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == str(path) for handler in logger.handlers):
            handler = logging.FileHandler(Path(path), encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
            logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger
