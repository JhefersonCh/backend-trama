"""Daily cleanup of uploaded files older than app_settings.media_retention_days (0 = keep forever)."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .repo import MediaRepo

log = logging.getLogger("trama.media")
DAY = 86400.0


async def cleanup_once(repo: MediaRepo, root: Path, now: datetime | None = None) -> int:
    days = await repo.retention_days()
    if days <= 0:
        return 0
    now = now or datetime.now(UTC)
    removed = 0
    for rel in await repo.purge_older_than(now - timedelta(days=days)):
        try:
            (root / rel).unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    # files whose row is gone (deleted session -> cascade) and are older than the window
    limit = time.time() - days * DAY
    for p in root.glob("*/*") if root.is_dir() else []:
        try:
            if p.is_file() and p.stat().st_mtime < limit:
                p.unlink()
                removed += 1
        except OSError:
            pass
    for d in root.glob("*") if root.is_dir() else []:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed


class MediaRetention:
    def __init__(self, repo: MediaRepo, root: Path, interval: float = DAY) -> None:
        self.repo, self.root, self.interval = repo, root, interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # let the app settle after boot
        while True:
            try:
                n = await cleanup_once(self.repo, self.root)
                if n:
                    log.info("media retention removed %d file(s)", n)
            except Exception as e:  # DB down etc.: try again tomorrow
                log.warning("media retention failed: %s", type(e).__name__)
            await asyncio.sleep(self.interval)
