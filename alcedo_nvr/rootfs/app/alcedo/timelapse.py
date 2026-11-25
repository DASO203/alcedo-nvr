"""Timelapse ("Zeitraffer") of all events in a period, e.g. last 24 h or last week.

Every event becomes a short part (its clip sped up, or its snapshot as a still)
with a caption "camera · time · AI description". Parts are cached per event, so
a weekly timelapse re-uses the parts of the daily ones. Parts are rendered with
low priority (``nice``) at 960x540 so Home Assistant stays responsive.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import logging
from pathlib import Path
import time
from typing import Any

from . import media
from .config import AppConfig
from .events import EventStore
from .media import Segment, build_clip

_LOGGER = logging.getLogger(__name__)

FONT = Path("/usr/share/fonts/dejavu/DejaVuSans.ttf")
WIDTH, HEIGHT, FPS = 960, 540, 15
MAX_EVENTS = 400
MAX_CLIP_SECONDS = 40  # of each source clip
STILL_SECONDS = 1.5
NICE = ["nice", "-n", "15"]  # low CPU priority


def job_key(
    start: float, end: float, cameras: list[str], confirmed: bool, mode: str, speed: int
) -> str:
    raw = f"{int(start)}|{int(end)}|{','.join(sorted(cameras))}|{confirmed}|{mode}|{speed}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]


def caption(camera: str, start: float, labels: list[str], description: str | None) -> str:
    when = datetime.fromtimestamp(start).strftime("%d.%m. %H:%M")
    what = description or ", ".join(labels)
    text = f"{camera}  ·  {when}  ·  {what}"
    return text if len(text) <= 90 else text[:87] + "..."


def _esc(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def part_filter(textfile: Path | None) -> str:
    chain = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p"
    )
    if textfile is not None and FONT.exists():
        chain += (
            f",drawtext=fontfile='{_esc(FONT)}':textfile='{_esc(textfile)}':x=14:y=h-th-14:"
            "expansion=none:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=8"
        )
    return chain


class TimelapseBuilder:
    def __init__(self, cfg: AppConfig, store: EventStore) -> None:
        self._cfg = cfg
        self._store = store
        self._names = {c.id: c.name for c in cfg.cameras}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.root = Path(cfg.storage_path) / "timelapse"

    async def request(
        self,
        start: float,
        end: float,
        cameras: list[str] | None,
        only_confirmed: bool,
        mode: str,
        speed: int,
    ) -> dict[str, Any]:
        """Return the job for these parameters, starting it if needed."""
        cams = [c for c in (cameras or list(self._names)) if c in self._names]
        mode = mode if mode in ("clips", "snapshots") else "clips"
        speed = max(1, min(16, speed))
        key = job_key(start, end, cams, only_confirmed, mode, speed)
        job = self.jobs.get(key)
        if job and (
            job["state"] in ("queued", "building")
            or (job["state"] == "done" and Path(job["path"]).exists())
        ):
            return job
        job = {
            "id": key,
            "state": "queued",
            "start": start,
            "end": end,
            "cameras": cams,
            "only_confirmed": only_confirmed,
            "mode": mode,
            "speed": speed,
            "progress": 0,
            "total": 0,
            "path": str(self.root / f"{key}.mp4"),
            "error": None,
            "created": time.time(),
        }
        self.jobs[key] = job
        task = asyncio.create_task(self._build(job), name=f"timelapse-{key}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _build(self, job: dict[str, Any]) -> None:
        async with self._lock:  # one timelapse at a time
            job["state"] = "building"
            try:
                events = [
                    e
                    for e in await self._store.between(job["start"], job["end"])
                    if e["camera"] in job["cameras"]
                    and (e.get("confirmed") or not job["only_confirmed"])
                ][-MAX_EVENTS:]
                job["total"] = len(events)
                if not events:
                    job["state"] = "empty"
                    return
                parts: list[Segment] = []
                for index, event in enumerate(events):
                    part = await self._part(event, job["mode"], job["speed"])
                    if part is not None:
                        parts.append(Segment(index, part))
                    job["progress"] = index + 1
                target = Path(job["path"])
                if parts and await build_clip(parts, target):
                    job["state"] = "done"
                    _LOGGER.info("Timelapse %s ready: %d events", job["id"], len(parts))
                else:
                    job["state"] = "error"
                    job["error"] = "no usable clips or snapshots"
            except Exception as err:
                _LOGGER.exception("Timelapse %s failed", job["id"])
                job["state"], job["error"] = "error", str(err)[:200]

    async def _part(self, event: dict[str, Any], mode: str, speed: int) -> Path | None:
        clip, snap = event.get("clip_path"), event.get("snapshot_path")
        use_clip = mode == "clips" and clip and Path(clip).exists()
        source = clip if use_clip else snap
        if not source or not Path(source).exists():
            return None
        parts_dir = self.root / "parts"
        await asyncio.to_thread(parts_dir.mkdir, parents=True, exist_ok=True)
        target = parts_dir / f"{event['id']}-{'c' + str(speed) if use_clip else 's'}.mp4"
        if target.exists():
            return target
        textfile = target.with_suffix(".txt")
        verdict = event.get("verdict") or {}
        text = caption(
            self._names.get(event["camera"], event["camera"]),
            event["start"],
            event.get("labels", []),
            verdict.get("description"),
        )
        await asyncio.to_thread(textfile.write_text, text, "utf-8")
        if use_clip:
            inputs = ["-t", str(MAX_CLIP_SECONDS), "-i", source]
            vf = f"setpts=PTS/{speed}," + part_filter(textfile)
        else:
            inputs = ["-loop", "1", "-t", str(STILL_SECONDS), "-i", source]
            vf = part_filter(textfile)
        args = [
            *NICE,
            media.FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            *inputs,
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-g",
            str(FPS * 2),
            str(target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        await asyncio.to_thread(textfile.unlink, True)
        if proc.returncode != 0:
            _LOGGER.warning(
                "Timelapse part for %s failed: %s",
                event["id"],
                (err or b"").decode(errors="replace")[-300:],
            )
            await asyncio.to_thread(target.unlink, True)
            return None
        return target

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
