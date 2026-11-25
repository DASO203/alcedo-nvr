"""Alarm sources: Reolink AI/motion polling, Instar alarm server hook, snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import secrets
import ssl
from typing import Any

import aiohttp

from .config import CameraConfig

_LOGGER = logging.getLogger(__name__)

# Callback signature: (camera_id, trigger, label)
TriggerCallback = Callable[[str, str, str], Awaitable[None]]

# Reolink GetAiState keys -> Alcedo object labels
REOLINK_AI_MAP = {
    "people": "person",
    "face": "person",
    "vehicle": "vehicle",
    "dog_cat": "animal",
}

# Instar alarm server "active" parameter (as documented by Instar for the
# Full HD / 2K+ / WQHD series). Unknown values are reported as "alarm".
INSTAR_ACTIVE_MAP = {
    "1": "motion",  # alarm area 1
    "2": "motion",
    "3": "motion",
    "4": "motion",
    "5": "motion",  # PIR sensor
    "6": "alarm_input",
    "7": "audio",
    "8": "motion",  # area 1 + PIR (linked)
    "9": "motion",
    "10": "motion",
    "11": "motion",
    "12": "person",  # object detection (WQHD series, person)
    "13": "vehicle",
    "14": "animal",
}

_NO_VERIFY = ssl.create_default_context()
_NO_VERIFY.check_hostname = False
_NO_VERIFY.verify_mode = ssl.CERT_NONE


def parse_reolink_state(response: Any) -> set[str]:
    """Return the active labels from a Reolink GetAiState + GetMdState reply."""
    active: set[str] = set()
    if not isinstance(response, list):
        return active
    for item in response:
        if not isinstance(item, dict) or item.get("code") != 0:
            continue
        value = item.get("value") or {}
        if item.get("cmd") == "GetMdState" and value.get("state") == 1:
            active.add("motion")
        if item.get("cmd") == "GetAiState":
            for key, label in REOLINK_AI_MAP.items():
                entry = value.get(key)
                if isinstance(entry, dict) and entry.get("alarm_state") == 1:
                    active.add(label)
    return active


def instar_label(active: str | None) -> str:
    """Map the Instar ``active`` value to a label."""
    return INSTAR_ACTIVE_MAP.get(str(active or "").strip(), "alarm")


class ReolinkPoller:
    """Polls a Reolink camera for AI / motion state (1 request per second).

    Uses the documented HTTP API (``/cgi-bin/api.cgi``). Credentials are sent
    as query parameters over HTTPS (Reolink's default). Reolink uses a
    self-signed certificate, therefore the certificate is not verified -
    restrict the camera to your local network.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        camera: CameraConfig,
        callback: TriggerCallback,
        interval: float = 1.0,
    ) -> None:
        self._session = session
        self._cam = camera
        self._callback = callback
        self._interval = interval
        self._task: asyncio.Task | None = None
        self.online = False
        self.last_error: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"reolink-{self._cam.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _poll_once(self) -> set[str]:
        cam = self._cam
        url = f"{cam.http_base}/cgi-bin/api.cgi"
        params = {"cmd": "GetAiState", "user": cam.username, "password": cam.password}
        body = [
            {"cmd": "GetAiState", "action": 0, "param": {"channel": cam.channel}},
            {"cmd": "GetMdState", "action": 0, "param": {"channel": cam.channel}},
        ]
        async with self._session.post(
            url,
            params=params,
            json=body,
            ssl=_NO_VERIFY if cam.use_https else None,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json(content_type=None)
        return parse_reolink_state(data)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                labels = await self._poll_once()
                if not self.online:
                    _LOGGER.info("Reolink %s online", self._cam.name)
                self.online, self.last_error, backoff = True, None, 1.0
                for label in labels:
                    if label in self._cam.objects:
                        await self._callback(self._cam.id, "camera", label)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if self.online or self.last_error is None:
                    _LOGGER.warning(
                        "Reolink %s not reachable: %s", self._cam.name, type(err).__name__
                    )
                self.online, self.last_error = False, type(err).__name__
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


async def fetch_snapshot(
    session: aiohttp.ClientSession,
    camera: CameraConfig,
    ffmpeg_grab: Callable[[str], Awaitable[bytes]],
) -> bytes:
    """Return a JPEG from the camera's HTTP API, falling back to an RTSP frame."""
    url: str | None = None
    params: dict[str, str] | None = None
    auth = None
    use_ssl: Any = None
    if camera.snapshot_url:
        url = camera.snapshot_url
    elif camera.type == "reolink":
        url = (
            f"{camera.http_base}/cgi-bin/api.cgi?cmd=Snap&channel={camera.channel}"
            f"&rs={secrets.token_hex(4)}"
        )
        params = {"user": camera.username, "password": camera.password}
        use_ssl = _NO_VERIFY if camera.use_https else None
    elif camera.type == "instar":
        url = f"{camera.http_base}/tmpfs/snap.jpg"  # noqa: S108 - camera URL path
        auth = aiohttp.BasicAuth(camera.username, camera.password) if camera.username else None
        use_ssl = _NO_VERIFY if camera.use_https else None
    if url:
        try:
            async with session.get(
                url,
                params=params,
                auth=auth,
                ssl=use_ssl,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.read()
            if resp.status == 200 and data[:2] == b"\xff\xd8":
                return data
            _LOGGER.debug("Snapshot of %s via HTTP failed (status %s)", camera.name, resp.status)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Snapshot of %s via HTTP failed: %s", camera.name, type(err).__name__)
    return await ffmpeg_grab(camera.main_url)
