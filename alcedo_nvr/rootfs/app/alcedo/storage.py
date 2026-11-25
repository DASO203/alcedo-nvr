"""Retention: keeps the disk from filling up.

* Event-only cameras keep just a short ring buffer of segments (pre-roll).
* Continuous cameras keep segments for ``retention_days``.
* Events (snapshot + clip) are kept for ``retention_days``.
* If the disk is fuller than ``max_storage_percent`` the oldest recordings are
  removed first, then the oldest events. The export folder is never touched.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
import time

from .config import AppConfig
from .events import MAX_EVENT_SECONDS, EventStore
from .media import Segment, list_segments

_LOGGER = logging.getLogger(__name__)
INTERVAL = 300


def expired_segments(segments: list[Segment], keep_seconds: float, now: float) -> list[Segment]:
    """Segments older than *keep_seconds*; the newest segment is always kept."""
    return [s for s in segments[:-1] if now - s.start > keep_seconds]


def disk_percent(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100


class Retention:
    def __init__(self, cfg: AppConfig, store: EventStore) -> None:
        self._cfg = cfg
        self._store = store
        self._task: asyncio.Task | None = None
        self.last_run: float | None = None
        self.deleted_segments = 0
        self.deleted_events = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="retention")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                _LOGGER.exception("Retention run failed")
            await asyncio.sleep(INTERVAL)

    def _buffer_seconds(self) -> float:
        c = self._cfg
        return c.pre_seconds + c.post_seconds + MAX_EVENT_SECONDS + 3 * c.segment_seconds + 60

    async def run_once(self) -> None:
        cfg, now = self._cfg, time.time()
        storage = Path(cfg.storage_path)
        keep_events = cfg.retention_days * 86400
        for cam in cfg.cameras:
            keep = keep_events if cam.record == "continuous" else self._buffer_seconds()
            segments = await asyncio.to_thread(list_segments, storage / "segments" / cam.id)
            for seg in expired_segments(segments, keep, now):
                await asyncio.to_thread(seg.path.unlink, True)
                self.deleted_segments += 1
        for folder, max_age in (
            (storage / "timelapse", 2 * 86400),
            (storage / "timelapse" / "parts", keep_events),
        ):
            if folder.is_dir():
                for file in await asyncio.to_thread(lambda f=folder: list(f.glob("*.mp4"))):
                    if now - file.stat().st_mtime > max_age:
                        await asyncio.to_thread(file.unlink, True)
        tmp = storage / "tmp"  # timeline playback clips
        if tmp.is_dir():
            for clip in await asyncio.to_thread(lambda: list(tmp.glob("*.mp4"))):
                if now - clip.stat().st_mtime > 3600:
                    await asyncio.to_thread(clip.unlink, True)
        for event in await self._store.older_than(now - keep_events):
            await self._delete_event(event)
        # disk pressure
        if storage.exists():
            for _ in range(200):
                if await asyncio.to_thread(disk_percent, str(storage)) <= cfg.max_storage_percent:
                    break
                if not await self._free_some(storage):
                    _LOGGER.warning(
                        "Disk above %s%% but nothing left to delete", cfg.max_storage_percent
                    )
                    break
        self.last_run = now

    async def _free_some(self, storage: Path) -> bool:
        segments: list[Segment] = []
        for cam in self._cfg.cameras:
            found = await asyncio.to_thread(list_segments, storage / "segments" / cam.id)
            segments.extend(found[:-1])
        segments.sort()
        if segments:
            for seg in segments[:50]:
                await asyncio.to_thread(seg.path.unlink, True)
                self.deleted_segments += 1
            _LOGGER.info("Disk pressure: removed %d oldest segments", min(50, len(segments)))
            return True
        events = await self._store.oldest(20)
        for event in events:
            await self._delete_event(event)
        if events:
            _LOGGER.info("Disk pressure: removed %d oldest events", len(events))
        return bool(events)

    async def _delete_event(self, event: dict) -> None:
        for key in ("clip_path", "snapshot_path"):
            if event.get(key):
                await asyncio.to_thread(Path(event[key]).unlink, True)
        await self._store.delete(event["id"])
        self.deleted_events += 1
