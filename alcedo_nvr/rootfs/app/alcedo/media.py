"""ffmpeg based recording, snapshots and clip assembly.

Recording never re-encodes video (``-c copy``): the CPU load is tiny and the
quality is exactly what the camera delivers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import itertools
import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SEGMENT_FORMAT = "%Y%m%dT%H%M%SZ"
FFMPEG = "ffmpeg"


@dataclass(frozen=True, slots=True, order=True)
class Segment:
    """A recorded segment (start time in UTC epoch seconds)."""

    start: float
    path: Path


def segment_start(path: Path) -> float | None:
    """Parse the UTC start time from a segment file name."""
    try:
        return datetime.strptime(path.stem, SEGMENT_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def list_segments(folder: Path) -> list[Segment]:
    """Return all finished and running segments below *folder*, sorted (blocking)."""
    segments = []
    if not folder.is_dir():
        return segments
    for path in folder.glob("*.mp4"):
        start = segment_start(path)
        if start is not None:
            segments.append(Segment(start, path))
    segments.sort()
    return segments


def select_segments(
    segments: list[Segment], start: float, end: float, *, include_last: bool = False
) -> list[Segment]:
    """Return the segments that overlap [start, end] (segments sorted by start).

    A segment lasts until the next one starts. The last segment is only
    returned with ``include_last`` (recorder already stopped), otherwise
    ffmpeg may still be writing it.
    """
    chosen = []
    for current, following in itertools.pairwise(segments):
        if current.start <= end and following.start > start:
            chosen.append(current)
    if include_last and segments and segments[-1].start <= end:
        chosen.append(segments[-1])
    return chosen


def recording_ranges(
    segments: list[Segment], segment_seconds: float, start: float, end: float
) -> list[tuple[float, float]]:
    """Merge segments into continuous [from, to] ranges within [start, end] (timeline)."""
    ranges: list[list[float]] = []
    gap = segment_seconds * 1.5
    for seg in segments:
        seg_end = seg.start + segment_seconds
        if seg_end < start or seg.start > end:
            continue
        if ranges and seg.start - ranges[-1][1] <= gap:
            ranges[-1][1] = max(ranges[-1][1], seg_end)
        else:
            ranges.append([seg.start, seg_end])
    return [(max(a, start), min(b, end)) for a, b in ranges]


def ffmpeg_redacted(args: list[str]) -> str:
    """Command line for logs without credentials in URLs."""
    out = []
    for arg in args:
        if "://" in arg and "@" in arg:
            scheme, rest = arg.split("://", 1)
            arg = f"{scheme}://***@{rest.split('@', 1)[1]}"
        out.append(arg)
    return " ".join(out)


class Recorder:
    """Keeps an ffmpeg segment recorder running for one camera."""

    def __init__(
        self,
        cam_id: str,
        url: str,
        folder: Path,
        segment_seconds: int,
        on_status: Callable[[str, bool], None],
    ) -> None:
        self._cam_id = cam_id
        self._url = url
        self._folder = folder
        self._segment_seconds = segment_seconds
        self._on_status = on_status
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"recorder-{self._cam_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _args(self) -> list[str]:
        pattern = str(self._folder / f"{SEGMENT_FORMAT}.mp4")
        return [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "10000000",
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            self._url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(self._segment_seconds),
            "-segment_format",
            "mp4",
            "-segment_format_options",
            "movflags=+frag_keyframe+empty_moov+default_base_moof",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            pattern,
        ]

    async def _run(self) -> None:
        backoff = 2.0
        env = {**os.environ, "TZ": "UTC"}  # segment names are UTC
        while True:
            await asyncio.to_thread(self._folder.mkdir, parents=True, exist_ok=True)
            args = self._args()
            _LOGGER.debug("Starting recorder %s: %s", self._cam_id, ffmpeg_redacted(args))
            started = asyncio.get_running_loop().time()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                self._on_status(self._cam_id, True)
                _, stderr = await self._proc.communicate()
            except asyncio.CancelledError:
                if self._proc and self._proc.returncode is None:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), 5)
                    except TimeoutError:
                        self._proc.kill()
                raise
            self._on_status(self._cam_id, False)
            message = (stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or [""]
            _LOGGER.warning(
                "Recorder %s stopped (%s), restarting", self._cam_id, ffmpeg_redacted([message[0]])
            )
            if asyncio.get_running_loop().time() - started > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def grab_frame(url: str, timeout: float = 15) -> bytes:
    """Grab one JPEG frame from an RTSP stream."""
    args = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-i",
        url,
        "-frames:v",
        "1",
        "-q:v",
        "4",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    if not data:
        raise RuntimeError("no frame received")
    return data


async def scale_jpeg(data: bytes, width: int = 768) -> bytes:
    """Downscale a JPEG (smaller input = much faster local LLM)."""
    args = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "image2pipe",
        "-i",
        "pipe:0",
        "-vf",
        f"scale='min({width},iw)':-2",
        "-q:v",
        "5",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(data), 20)
    return out or data


async def build_clip(segments: list[Segment], target: Path) -> bool:
    """Concatenate segments into one MP4 without re-encoding."""
    if not segments:
        return False
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    listing = target.with_suffix(".txt")
    lines = "".join(f"file '{s.path.as_posix()}'\n" for s in segments)
    await asyncio.to_thread(listing.write_text, lines, "utf-8")
    tmp = target.with_suffix(".part.mp4")
    args = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    await asyncio.to_thread(listing.unlink, True)
    if proc.returncode != 0:
        _LOGGER.warning(
            "Clip %s failed: %s", target.name, (err or b"").decode(errors="replace")[-300:]
        )
        await asyncio.to_thread(tmp.unlink, True)
        return False
    await asyncio.to_thread(os.replace, tmp, target)
    return True
