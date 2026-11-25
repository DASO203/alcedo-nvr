"""Lightweight motion detection on the low resolution sub stream.

ffmpeg decodes the sub stream at 3 fps, scales it to 160x90 grey pixels and
pipes raw frames to Python. A running background average is compared with
each frame - a few thousand pixels per frame, negligible CPU.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

import numpy as np

from .media import FFMPEG, ffmpeg_redacted

_LOGGER = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 160, 90, 3
FRAME_BYTES = WIDTH * HEIGHT


def thresholds(sensitivity: int) -> tuple[int, float]:
    """Map sensitivity 1..100 to (pixel delta, changed-area ratio)."""
    s = max(1, min(100, sensitivity)) / 100
    pixel_delta = int(45 - 30 * s)  # 45 (insensitive) .. 15 (very sensitive)
    area = 0.03 - 0.027 * s  # 3 % .. 0.3 % of the image
    return pixel_delta, area


class MotionAnalyzer:
    """Pure frame analysis (testable without ffmpeg)."""

    def __init__(self, sensitivity: int, alpha: float = 0.1) -> None:
        self.pixel_delta, self.area = thresholds(sensitivity)
        self.alpha = alpha
        self._background: np.ndarray | None = None
        self._lit: np.ndarray | None = None

    def feed(self, frame: np.ndarray) -> float:
        """Feed a grey frame; return the ratio of changed pixels (0..1)."""
        frame = frame.astype(np.float32)
        if self._background is None:
            self._background = frame
            return 0.0
        mean_now, mean_bg = float(frame.mean()), float(self._background.mean())
        # Sudden global brightness change (IR switch, light on) is not motion.
        if abs(mean_now - mean_bg) > 40:
            self._background = frame
            return 0.0
        diff = np.abs(frame - self._background) > self.pixel_delta
        ratio = float(diff.mean())
        self._background = (1 - self.alpha) * self._background + self.alpha * frame
        return ratio

    def is_motion(self, ratio: float) -> bool:
        return ratio >= self.area


class MotionDetector:
    """Runs ffmpeg on a stream and reports motion."""

    def __init__(
        self,
        cam_id: str,
        url: str,
        sensitivity: int,
        callback: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        self._cam_id = cam_id
        self._url = url
        self._analyzer = MotionAnalyzer(sensitivity)
        self._callback = callback
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"motion-{self._cam_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        args = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-i",
            self._url,
            "-an",
            "-vf",
            f"fps={FPS},scale={WIDTH}:{HEIGHT},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        backoff = 2.0
        while True:
            _LOGGER.debug("Motion detector %s: %s", self._cam_id, ffmpeg_redacted(args))
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            consecutive = 0
            try:
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readexactly(FRAME_BYTES)
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH)
                    ratio = self._analyzer.feed(frame)
                    # Two consecutive frames with motion -> fewer false triggers.
                    consecutive = consecutive + 1 if self._analyzer.is_motion(ratio) else 0
                    if consecutive >= 2:
                        await self._callback(self._cam_id, "motion", "motion")
                    backoff = 2.0
            except asyncio.IncompleteReadError:
                _LOGGER.warning("Motion stream of %s ended, restarting", self._cam_id)
            except asyncio.CancelledError:
                proc.kill()
                raise
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
