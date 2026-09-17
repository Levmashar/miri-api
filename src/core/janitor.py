"""
Background janitor — deletes generated images and uploaded attachments after
they have been served.

Generated images are multi-MB PNGs. Every /v1/images/generations call writes
one to IMAGES_DIR (bind-mounted to ./downloads on the host), and nothing ever
removed them, so the directory grew without bound for the life of the deploy.

Once a response has been returned the file is dead weight:
  - response_format="b64_json" (the default) embeds the bytes in the response,
    so the file on disk was never needed by the caller at all.
  - response_format="url" hands back a path the caller is expected to read
    promptly; IMAGE_RETENTION_MINUTES is how long that path stays valid.

Deletion is by mtime age, so a file is only ever removed long after the request
that created it returned. Set IMAGE_RETENTION_MINUTES=0 to keep files forever.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("janitor")

# Only ever delete files we plausibly created. A misconfigured IMAGES_DIR
# pointing at something important should not become a file shredder.
_SWEEP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf",
                   ".txt", ".csv", ".json", ".docx", ".xlsx", ".bin",
                   ".mp4", ".webm", ".mov"}  # generated videos (Seedream/Dreamina)


def sweep_once(directories: list[Path], ttl_seconds: float) -> int:
    """Delete files older than ttl_seconds. Returns the number removed.

    Synchronous and cheap (a stat per file); callers run it off the event loop.
    """
    if ttl_seconds <= 0:
        return 0

    cutoff = time.time() - ttl_seconds
    removed = 0
    freed = 0

    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except OSError as e:
            log.warning(f"Cannot list {directory}: {e}")
            continue

        for path in entries:
            try:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in _SWEEP_SUFFIXES:
                    continue
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue  # Still within its retention window.
                size = stat.st_size
                path.unlink()
                removed += 1
                freed += size
                log.debug(f"Deleted expired file: {path.name}")
            except FileNotFoundError:
                continue  # Raced with another delete — fine.
            except OSError as e:
                log.warning(f"Could not delete {path}: {e}")

    if removed:
        log.info(f"Janitor removed {removed} expired file(s), freed {freed / 1024 / 1024:.1f} MB")
    return removed


async def janitor_loop(directories: list[Path], ttl_seconds: float, interval_seconds: float) -> None:
    """Sweep `directories` every `interval_seconds`, forever.

    Runs as a background task for the life of the server. Never raises: a
    janitor failure must not take down the API.
    """
    log.info(
        f"Janitor started — deleting files older than {ttl_seconds / 60:.0f} min "
        f"from {', '.join(str(d) for d in directories)} (every {interval_seconds:.0f}s)"
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            # stat/unlink are blocking syscalls. A large directory would stall
            # the event loop (and every in-flight request) if swept inline.
            await asyncio.to_thread(sweep_once, directories, ttl_seconds)
        except asyncio.CancelledError:
            log.info("Janitor stopped")
            raise
        except Exception as e:
            log.error(f"Janitor sweep failed (continuing): {e}", exc_info=True)


def swept_directories() -> list[Path]:
    """Directories the janitor is responsible for."""
    return [
        Config.IMAGES_DIR,           # generated images (bind-mounted to ./downloads)
        Config.VIDEOS_DIR,           # generated videos (Seedream/Dreamina)
        Path("/tmp/miri_files"),   # attachments downloaded/decoded per request
    ]
