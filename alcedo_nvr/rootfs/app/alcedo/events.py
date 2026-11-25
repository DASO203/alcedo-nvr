"""Event lifecycle: trigger -> snapshot -> LLM review -> clip -> export."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any

from .config import AppConfig, CameraConfig
from .export import Exporter
from .llm import LlmReviewer
from .media import Recorder, build_clip, list_segments, scale_jpeg, select_segments

_LOGGER = logging.getLogger(__name__)

MAX_EVENT_SECONDS = 300
SnapshotFn = Callable[[CameraConfig], Awaitable[bytes]]
Publisher = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class Event:
    id: str
    camera: str
    trigger: str
    start: float
    last_seen: float
    labels: set[str] = field(default_factory=set)
    end: float | None = None
    state: str = "active"  # active | done
    confirmed: bool | None = None
    verdict: dict[str, Any] | None = None
    snapshot: str | None = None
    clip: str | None = None
    exported: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera": self.camera,
            "trigger": self.trigger,
            "labels": sorted(self.labels),
            "start": self.start,
            "end": self.end,
            "state": self.state,
            "confirmed": self.confirmed,
            "verdict": self.verdict,
            "snapshot_path": self.snapshot,
            "clip_path": self.clip,
            "exported": self.exported,
        }


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone().strftime("%Y-%m-%d")


class EventStore:
    """Tiny SQLite store (in /data, survives restarts and is part of app backups)."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, camera TEXT, start REAL,"
            " data TEXT)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ev_start ON events(start)")
        self._db.commit()
        self._lock = asyncio.Lock()

    async def save(self, event: Event) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save, event)

    def _save(self, event: Event) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?)",
            (event.id, event.camera, event.start, json.dumps(event.as_dict())),
        )
        self._db.commit()

    async def recent(self, limit: int = 50, camera: str | None = None) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            sql = "SELECT data FROM events"
            args: tuple[Any, ...] = ()
            if camera:
                sql += " WHERE camera = ?"
                args = (camera,)
            sql += " ORDER BY start DESC LIMIT ?"
            return [json.loads(r[0]) for r in self._db.execute(sql, (*args, limit))]

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def get(self, event_id: str) -> dict[str, Any] | None:
        def _q() -> dict[str, Any] | None:
            row = self._db.execute("SELECT data FROM events WHERE id = ?", (event_id,)).fetchone()
            return json.loads(row[0]) if row else None

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def between(self, start: float, end: float) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT data FROM events WHERE start >= ? AND start < ? ORDER BY start",
                (start, end),
            )
            return [json.loads(r[0]) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def older_than(self, ts: float) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            rows = self._db.execute("SELECT data FROM events WHERE start < ?", (ts,))
            return [json.loads(r[0]) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def oldest(self, n: int) -> list[dict[str, Any]]:
        def _q() -> list[dict[str, Any]]:
            rows = self._db.execute("SELECT data FROM events ORDER BY start ASC LIMIT ?", (n,))
            return [json.loads(r[0]) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_q)

    async def delete(self, event_id: str) -> None:
        def _q() -> None:
            self._db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self._db.commit()

        async with self._lock:
            await asyncio.to_thread(_q)

    async def stats_since(self, ts: float) -> dict[str, dict[str, int]]:
        def _q() -> dict[str, dict[str, int]]:
            out: dict[str, dict[str, int]] = {}
            for (data,) in self._db.execute("SELECT data FROM events WHERE start >= ?", (ts,)):
                ev = json.loads(data)
                s = out.setdefault(ev["camera"], {"events": 0, "confirmed": 0})
                s["events"] += 1
                s["confirmed"] += 1 if ev.get("confirmed") else 0
            return out

        async with self._lock:
            return await asyncio.to_thread(_q)


class EventManager:
    """Creates and finishes events for all cameras."""

    def __init__(
        self,
        cfg: AppConfig,
        store: EventStore,
        reviewer: LlmReviewer,
        exporter: Exporter,
        snapshot: SnapshotFn,
        publish: Publisher,
        recorder_status: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._cams = {c.id: c for c in cfg.cameras}
        self._store = store
        self._reviewer = reviewer
        self._exporter = exporter
        self._snapshot = snapshot
        self._publish = publish
        self._active: dict[str, Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self.paused: set[str] = set()
        self.storage = Path(cfg.storage_path)
        self._recorder_status = recorder_status or (lambda _cam, _on: None)
        self._on_demand: dict[str, Recorder] = {}

    def active(self, camera_id: str) -> Event | None:
        return self._active.get(camera_id)

    def segments_dir(self, camera_id: str) -> Path:
        return self.storage / "segments" / camera_id

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def trigger(self, camera_id: str, trigger: str, label: str) -> None:
        """Called by every alarm source."""
        cam = self._cams.get(camera_id)
        if cam is None or not cam.enabled or camera_id in self.paused:
            return
        now = time.time()
        event = self._active.get(camera_id)
        if event is not None:
            event.last_seen = now
            if label not in event.labels:
                event.labels.add(label)
                self._publish({"type": "event_update", "event": event.as_dict()})
            return
        stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%S")
        event = Event(
            id=f"{camera_id}-{stamp}-{secrets.token_hex(2)}",
            camera=camera_id,
            trigger=trigger,
            start=now,
            last_seen=now,
            labels={label},
        )
        self._active[camera_id] = event
        _LOGGER.info("Event %s started (%s: %s)", event.id, trigger, label)
        if cam.record == "on_demand" and cam.main_url and camera_id not in self._on_demand:
            # e.g. Ring: only stream while something happens (battery, rate limits)
            rec = Recorder(
                camera_id,
                cam.main_url,
                self.segments_dir(camera_id),
                self._cfg.segment_seconds,
                self._recorder_status,
            )
            rec.start()
            self._on_demand[camera_id] = rec
        await self._store.save(event)
        self._publish({"type": "event_start", "event": event.as_dict()})
        self._spawn(self._review(cam, event), f"review-{event.id}")
        self._spawn(self._finish(cam, event), f"finish-{event.id}")

    def _event_dir(self, event: Event) -> Path:
        return self.storage / "events" / event.camera / day_of(event.start)

    async def _review(self, cam: CameraConfig, event: Event) -> None:
        """Take frames, store the snapshot and ask the LLM."""
        frames: list[bytes] = []
        count = self._cfg.llm.frames if self._reviewer.enabled else 1
        for index in range(count):
            if index:
                await asyncio.sleep(1.5)
            try:
                frames.append(await self._snapshot(cam))
            except Exception as err:
                _LOGGER.warning("Snapshot for %s failed: %s", event.id, type(err).__name__)
        if frames:
            target = self._event_dir(event) / f"{event.id}.jpg"
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, frames[0])
            event.snapshot = str(target)
        if self._reviewer.enabled and frames:
            small = [await scale_jpeg(f) for f in frames]
            verdict = await self._reviewer.review(cam.name, ", ".join(sorted(event.labels)), small)
            event.verdict = verdict.as_dict()
            event.confirmed = self._reviewer.is_confirmed(verdict)
            _LOGGER.info(
                "Event %s reviewed in %.1fs: %s (%.0f%%) %s",
                event.id,
                verdict.seconds,
                "ALARM" if event.confirmed else "no alarm",
                verdict.confidence * 100,
                verdict.description,
            )
        else:
            event.confirmed = True  # no LLM -> every camera alarm counts
        await self._store.save(event)
        self._publish({"type": "event_verdict", "event": event.as_dict()})

    async def _finish(self, cam: CameraConfig, event: Event) -> None:
        """Wait for the event to end, build the clip and export."""
        post = self._cfg.post_seconds
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if now - event.last_seen >= post or now - event.start >= MAX_EVENT_SECONDS:
                break
        event.end = event.last_seen
        self._active.pop(cam.id, None)
        # wait until the segment that contains end+post is closed
        await asyncio.sleep(post + self._cfg.segment_seconds + 2)
        on_demand = self._on_demand.pop(cam.id, None) if cam.id not in self._active else None
        if on_demand is not None:
            await on_demand.stop()  # all segments are complete now
        while event.confirmed is None:  # review still running
            await asyncio.sleep(1)
        if cam.record != "off":
            segments = await asyncio.to_thread(list_segments, self.segments_dir(cam.id))
            chosen = select_segments(
                segments,
                event.start - self._cfg.pre_seconds,
                event.end + post,
                include_last=cam.record == "on_demand",
            )
            target = self._event_dir(event) / f"{event.id}.mp4"
            if await build_clip(chosen, target):
                event.clip = str(target)
        event.state = "done"
        if event.confirmed or not self._cfg.export.only_confirmed:
            files = [Path(p) for p in (event.clip, event.snapshot) if p]
            event.exported = await self._exporter.export(cam.id, day_of(event.start), files)
        await self._store.save(event)
        _LOGGER.info("Event %s finished (clip: %s)", event.id, bool(event.clip))
        self._publish({"type": "event_end", "event": event.as_dict()})

    async def stop(self) -> None:
        for rec in self._on_demand.values():
            await rec.stop()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
