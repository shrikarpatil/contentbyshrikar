"""Per-run file and console logging setup."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_run_logger(run_directory: Path) -> logging.Logger:
    """Configure a logger that records this run without duplicating handlers."""
    logger = logging.getLogger(f"influencerai.run.{run_directory.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(run_directory / "logs" / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger