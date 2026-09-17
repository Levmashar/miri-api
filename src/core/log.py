"""
Logging setup — per-logger file + a combined file + optional console handlers.

Three things here exist because logs kept looking incomplete:

  * The console followed a hardcoded INFO while LOG_LEVEL said DEBUG, so the
    DEBUG lines that explain a failed UI interaction reached the files and
    never `docker logs`.
  * Loggers that never call setup_logging (asyncio's "Task exception was never
    retrieved", patchright) had nowhere to write and were dropped entirely.
  * One request writes to SEVERAL per-logger files — a Qwen video touches
    qwen_client, qwen_detector, qwen_media_mode, worker_pool and openai_routes
    — so tailing any one of them shows a fraction of the story. Everything is
    now ALSO written to a single chronological logs/miri.log.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from src.core.config import Config

# Global flag: when True, suppress console log handlers (for TUI mode)
_suppress_console = False

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are pinned to INFO on the root handler. At DEBUG
# these bury everything else (patchright logs every protocol message).
_NOISY = ("patchright", "playwright", "websockets", "urllib3", "PIL", "asyncio")

_combined: "logging.Handler | None" = None
_root_attached = False


def _level() -> int:
    """LOG_LEVEL as a level. An unknown/typo'd value fails SAFE toward INFO."""
    return getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)


def _formatter() -> logging.Formatter:
    return logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)


def _combined_file_handler() -> "logging.Handler | None":
    """One chronological file holding every logger's records: logs/miri.log."""
    global _combined
    if _combined is None:
        try:
            _combined = logging.handlers.TimedRotatingFileHandler(
                Config.LOG_DIR / "miri.log",
                when="midnight",
                backupCount=Config.LOG_FILE_RETENTION_DAYS,
                encoding="utf-8",
            )
            _combined.setLevel(logging.DEBUG)
            _combined.setFormatter(_formatter())
        except OSError:
            # A read-only or missing log dir must not take the process down.
            return None
    return _combined


def _attach_root() -> None:
    """Route records from loggers that never call setup_logging into miri.log."""
    global _root_attached
    if _root_attached:
        return
    _root_attached = True
    root = logging.getLogger()
    combined = _combined_file_handler()
    if combined is not None and combined not in root.handlers:
        root.addHandler(combined)
    root.setLevel(min(_level(), logging.INFO))
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.INFO)


def suppress_console_logs() -> None:
    """Disable all console log handlers (call before any setup_logging)."""
    global _suppress_console
    _suppress_console = True
    # Also silence already-created loggers' console handlers
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.stderr):
                logger.removeHandler(handler)


def setup_logging(name: str = "chatgpt_scraper", log_file: str | None = None) -> logging.Logger:
    """
    Configure and return a logger that writes to file (and optionally console).

    Args:
        name: Logger name.
        log_file: Optional filename override. Defaults to '{name}_{date}.log'.
    """
    Config.ensure_dirs()
    _attach_root()

    logger = logging.getLogger(name)
    logger.setLevel(_level())

    # Prevent duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    formatter = _formatter()

    # ── Combined file (logs/miri.log) ───────────────────────────
    # Everything, in one chronological stream. propagate is then turned OFF so
    # the root's copy of this same handler doesn't record each line twice.
    combined = _combined_file_handler()
    if combined is not None:
        logger.addHandler(combined)
    logger.propagate = False

    # ── File handler (daily rotation, bounded retention) ────────
    # The date is no longer in the base name — TimedRotatingFileHandler appends
    # a dated suffix on rollover and deletes files older than backupCount days,
    # so the logs dir can't grow without bound (nothing else prunes it).
    if log_file is None:
        log_file = f"{name}.log"

    fh = logging.handlers.TimedRotatingFileHandler(
        Config.LOG_DIR / log_file,
        when="midnight",
        backupCount=Config.LOG_FILE_RETENTION_DAYS,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)  # Always capture everything in file
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # ── Console handler (disabled in TUI mode) ──────────────────
    # Follows LOG_LEVEL. It used to be pinned to INFO, so LOG_LEVEL=DEBUG wrote
    # the interesting lines to the files and showed none of them in the console
    # or `docker logs` — which is what "not all the logs are there" looked like.
    if Config.VERBOSE and not _suppress_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(_level())
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger
