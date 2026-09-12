"""Logging configuration: rotating text file + optional console."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ColourFormatter(logging.Formatter):
    """Adds ANSI colours on a TTY; falls back to plain text when piped."""

    COLOURS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        colour = self.COLOURS.get(record.levelname)
        return f"{colour}{text}{self.RESET}" if colour else text


def setup_logging(config: LoggingConfig, log_path: Path) -> logging.Logger:
    """Install handlers on the root logger and return the bot logger.

    The file handler rotates at midnight and keeps ``backup_count`` days, so a
    long-running bot never fills the disk.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    level = getattr(logging, config.level.upper(), logging.INFO)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=max(1, config.backup_count), encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    root.addHandler(file_handler)

    if config.console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        formatter_cls = _ColourFormatter if is_tty else logging.Formatter
        console_handler.setFormatter(formatter_cls(_FORMAT, _DATE_FORMAT))
        root.addHandler(console_handler)

    # The websocket library is extremely chatty at DEBUG level.
    logging.getLogger("websocket").setLevel(logging.WARNING)

    logger = logging.getLogger("unsharp_bot")
    logger.info("Logging to %s (level=%s)", log_path, config.level.upper())
    return logger
