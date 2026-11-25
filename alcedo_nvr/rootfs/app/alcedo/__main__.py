"""Alcedo NVR app entry point: wires everything together."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import time
from typing import Any

import aiohttp

from . import __version__
from .api import Api
from .config import AppConfig, CameraConfig, load_options, redact
from .events import EventManager, EventStore
from .export import Exporter
from .llm import LlmReviewer
from .media import (
    Recorder,
    build_clip,
    grab_frame,
    list_segments,
    recording_ranges,
    select_segments,
)
from .motion import MotionDetector
from .sources import ReolinkPoller, fetch_snapshot
from .storage import Retention, disk_percent
from .timelapse import TimelapseBuilder

_LOGGER = logging.getLogger("alcedo")
DATA = Path("/data")


class RedactingFilter(logging.Filter):
    """Never let a camera password reach the log."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._cfg)
        record.args = ()
        return True


def load_api_key() -> str:
    """Random API key, created once, readable only by the app."""
    path = DATA / "api_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(32)
    path.write_text(key, encoding="utf-8")
    path.chmod(0o600)
    return key


class AlcedoApp:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.cameras: dict[str, CameraConfig] = {c.id: c for c in cfg.cameras}
        self.api_key = load_api_key()
        self.hostname = socket.gethostname()
        self.session = aiohttp.ClientSession()
        self.store = EventStore(DATA / "events.db")
        self.reviewer = LlmReviewer(self.session, cfg.llm)
        self.exporter = Exporter(cfg.export)
        self.api = Api(self)
        self.events = EventManager(
            cfg,
            self.store,
            self.reviewer,
            self.exporter,
            self.snapshot,
            self.api.publish,
            self._recorder_status,
        )
        self.retention = Retention(cfg, self.store)
        self.timelapse = TimelapseBuilder(cfg, self.store)
        self.recording: dict[str, bool] = {}
        self.last_images: dict[str, bytes] = {}
        self._workers: list[Any] = []
        self.started = time.time()

    async def snapshot(self, cam: CameraConfig) -> bytes:
        """Current image of a camera.

        Ring (via ring-mqtt): opening the live stream interrupts Ring's own
        motion detection and drains batteries. Without a snapshot_url the
        stream is therefore only used while an event is active; otherwise the
        last image is returned.
        """
        if cam.type == "ring" and not cam.snapshot_url and self.events.active(cam.id) is None:
            if cam.id in self.last_images:
                return self.last_images[cam.id]
            raise LookupError("Ring: no image until the first event (stream stays closed)")
        data = await fetch_snapshot(self.session, cam, grab_frame)
        self.last_images[cam.id] = data
        return data

    def _recorder_status(self, cam_id: str, running: bool) -> None:
        if self.recording.get(cam_id) != running:
            self.recording[cam_id] = running
            self.api.publish({"type": "camera_update", "camera": cam_id, "recording": running})

    async def timeline(
        self, day: str | None, start: float | None = None, end: float | None = None
    ) -> dict[str, Any]:
        """All cameras for one local day (or a start/end range up to 31 days)."""
        if start is None or end is None:
            try:
                base = datetime.strptime(day, "%Y-%m-%d") if day else datetime.now()
            except ValueError as err:
                raise ValueError("day must be YYYY-MM-DD") from err
            start = base.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            end = start + 86400
        if not 0 < end - start <= 31 * 86400:
            raise ValueError("range must be between 0 and 31 days")
        events = await self.store.between(start, end)
        cams = []
        for cam in self.cfg.cameras:
            segments = await asyncio.to_thread(list_segments, self.events.segments_dir(cam.id))
            cams.append(
                {
                    "id": cam.id,
                    "name": cam.name,
                    "record": cam.record,
                    "recordings": recording_ranges(segments, self.cfg.segment_seconds, start, end),
                    "events": [
                        {
                            "id": e["id"],
                            "start": e["start"],
                            "end": e.get("end") or e["start"],
                            "confirmed": e.get("confirmed"),
                            "labels": e.get("labels", []),
                            "description": (e.get("verdict") or {}).get("description"),
                            "has_clip": bool(e.get("clip_path")),
                        }
                        for e in events
                        if e["camera"] == cam.id
                    ],
                }
            )
        return {
            "day": datetime.fromtimestamp(start).strftime("%Y-%m-%d"),
            "start": start,
            "end": end,
            "cameras": cams,
        }

    async def recording_clip(self, cam: CameraConfig, start: float, duration: int) -> Path:
        """Build (or reuse) a clip of the recording at an arbitrary time (timeline playback)."""
        duration = max(10, min(600, duration))
        target = Path(self.cfg.storage_path) / "tmp" / f"{cam.id}-{int(start)}-{duration}.mp4"
        if await asyncio.to_thread(target.exists):
            return target
        segments = await asyncio.to_thread(list_segments, self.events.segments_dir(cam.id))
        chosen = select_segments(segments, start, start + duration)
        if not await build_clip(chosen, target):
            raise FileNotFoundError("no recording at this time")
        return target

    async def info(self) -> dict[str, Any]:
        today = time.time() - (time.time() % 86400)
        stats = await self.store.stats_since(today)
        storage = Path(self.cfg.storage_path)
        cams = []
        for cam in self.cfg.cameras:
            poller = next(
                (w for w in self._workers if isinstance(w, ReolinkPoller) and w._cam.id == cam.id),
                None,
            )
            active = self.events.active(cam.id)
            cams.append(
                {
                    **cam.public_dict(),
                    "recording": self.recording.get(cam.id, False),
                    "online": poller.online if poller else self.recording.get(cam.id),
                    "detection": cam.id not in self.events.paused,
                    "active_event": active.as_dict() if active else None,
                    "today": stats.get(cam.id, {"events": 0, "confirmed": 0}),
                }
            )
        return {
            "version": __version__,
            "uptime": int(time.time() - self.started),
            "cameras": cams,
            "storage": {
                "path": str(storage),
                "used_percent": round(disk_percent(str(storage)), 1) if storage.exists() else None,
                "max_percent": self.cfg.max_storage_percent,
                "retention_days": self.cfg.retention_days,
            },
            "llm": {
                "enabled": self.reviewer.enabled,
                "model": self.cfg.llm.model if self.reviewer.enabled else None,
                "reviews": self.reviewer.reviews,
                "last_error": self.reviewer.last_error,
            },
            "export": {
                "local": self.cfg.export.local,
                "local_path": self.cfg.export.local_path,
                "remote": self.cfg.export.protocol if self.cfg.export.remote else None,
                "only_confirmed": self.cfg.export.only_confirmed,
                "last_error": self.exporter.last_error,
            },
        }

    async def start(self) -> None:
        Path(self.cfg.storage_path).mkdir(parents=True, exist_ok=True)
        await self.api.start()
        for cam in self.cfg.cameras:
            if not cam.enabled:
                continue
            if not cam.main_url:
                _LOGGER.error("Camera %s: no stream URL (set host or rtsp_main)", cam.name)
                continue
            if cam.record in ("events", "continuous"):
                rec = Recorder(
                    cam.id,
                    cam.main_url,
                    self.events.segments_dir(cam.id),
                    self.cfg.segment_seconds,
                    self._recorder_status,
                )
                rec.start()
                self._workers.append(rec)
            if cam.type == "reolink" and cam.uses_camera_events and cam.host:
                poller = ReolinkPoller(self.session, cam, self.events.trigger)
                poller.start()
                self._workers.append(poller)
            if cam.uses_motion and cam.sub_url:
                det = MotionDetector(
                    cam.id, cam.sub_url, cam.motion_sensitivity, self.events.trigger
                )
                det.start()
                self._workers.append(det)
            _LOGGER.info(
                "Camera %s (%s): record=%s detect=%s objects=%s",
                cam.name,
                cam.type,
                cam.record,
                cam.detect,
                ",".join(cam.objects),
            )
        self.retention.start()
        await self._announce()

    async def _announce(self) -> None:
        """Tell Home Assistant (via Supervisor discovery) where the API is."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        try:
            async with self.session.post(
                "http://supervisor/discovery",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "service": "alcedo_nvr",
                    "config": {"host": self.hostname, "port": 8765, "api_key": self.api_key},
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                _LOGGER.info("Discovery sent to Home Assistant (status %s)", resp.status)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Discovery failed: %s", err)

    async def stop(self) -> None:
        for worker in self._workers:
            await worker.stop()
        await self.events.stop()
        await self.timelapse.stop()
        await self.retention.stop()
        await self.api.stop()
        await self.session.close()


async def main() -> None:
    cfg = load_options()
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(cfg))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
    _LOGGER.info("Alcedo NVR %s starting with %d camera(s)", __version__, len(cfg.cameras))
    app = AlcedoApp(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await app.start()
    await stop.wait()
    _LOGGER.info("Stopping")
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
