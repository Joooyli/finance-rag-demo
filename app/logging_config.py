from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import config

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once: console plus an optional rotating file.

    Logging must never be the reason a request fails, so every failure here is
    swallowed after the console handler is attached.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = (level or config.LOG_LEVEL or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if config.LOG_TO_FILE:
        try:
            log_dir: Path = config.LOG_DIR
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:  # pragma: no cover - logging must not break startup
            root.warning("无法写入日志文件，已仅使用控制台日志", exc_info=True)

    _CONFIGURED = True
