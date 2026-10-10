# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared rotating diagnostic log under %APPDATA%\\CS2Screen\\logs.

Background monitors used to swallow failures with ``except: pass``. They now
report through this logger so field problems can be diagnosed without a
debugger. Logging must never break the caller, therefore every failure in the
logging path itself is ignored.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen" / "logs"
LOG_PATH = LOG_DIR / "sabasakal.log"
_MAX_BYTES = 1024 * 1024
_BACKUPS = 3
_lock = threading.Lock()
_configured = False
_last_messages: dict[str, float] = {}


def get_logger(name: str = "sabasakal") -> logging.Logger:
    global _configured
    logger = logging.getLogger("sabasakal")
    with _lock:
        if not _configured:
            _configured = True
            logger.setLevel(logging.INFO)
            logger.propagate = False
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
                    encoding="utf-8", delay=True,
                )
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                ))
                logger.addHandler(handler)
            except OSError:
                logger.addHandler(logging.NullHandler())
    return logger if name == "sabasakal" else logger.getChild(name)


def log_exception(component: str, message: str, exc: BaseException | None = None,
                  throttle_seconds: float = 30.0) -> None:
    """Log one failure, throttling identical messages from tight loops."""
    try:
        key = f"{component}|{message}|{type(exc).__name__ if exc else ''}"
        now = time.monotonic()
        with _lock:
            last = _last_messages.get(key)
            if last is not None and now - last < throttle_seconds:
                return
            _last_messages[key] = now
            if len(_last_messages) > 512:
                _last_messages.clear()
        logger = get_logger(component)
        if exc is None:
            logger.warning(message)
        else:
            logger.warning("%s: %s: %s", message, type(exc).__name__, exc)
    except Exception:
        pass


def log_info(component: str, message: str) -> None:
    try:
        get_logger(component).info(message)
    except Exception:
        pass


__all__ = ["LOG_PATH", "get_logger", "log_exception", "log_info"]
