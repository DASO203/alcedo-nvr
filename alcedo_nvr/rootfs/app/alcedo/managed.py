"""Cameras added from Home Assistant (stored in /data/cameras.json).

The Alcedo NVR integration can take over cameras that already exist in Home
Assistant (Reolink integration, Generic camera / RTSP, ring-mqtt, ...). They
are stored here, next to the cameras from the app options, and survive app
updates. The file contains camera passwords and is only readable by the app.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from .config import VALID_OBJECTS

MANAGED_FILE = Path("/data/cameras.json")
MAX_CAMERAS = 64

TYPES = ("reolink", "instar", "ring", "generic")
RECORD = ("events", "continuous", "on_demand", "off")
DETECT = ("camera", "motion", "both", "off")
_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")


def _text(raw: dict[str, Any], key: str, limit: int) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str | int):
        raise ValueError(f"{key}: must be text")
    value = str(value).strip()
    if len(value) > limit:
        raise ValueError(f"{key}: too long")
    return value


def _url(raw: dict[str, Any], key: str, schemes: tuple[str, ...]) -> str:
    value = _text(raw, key, 2048)
    if value and urlsplit(value).scheme.lower() not in schemes:
        raise ValueError(f"{key}: must start with {' or '.join(s + '://' for s in schemes)}")
    return value


def _choice(raw: dict[str, Any], key: str, choices: tuple[str, ...], default: str) -> str:
    value = raw.get(key) or default
    if value not in choices:
        raise ValueError(f"{key}: must be one of {', '.join(choices)}")
    return value


def _int(raw: dict[str, Any], key: str, low: int, high: int, default: int | None) -> int | None:
    value = raw.get(key)
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: must be a number") from None
    if not low <= number <= high:
        raise ValueError(f"{key}: must be between {low} and {high}")
    return number


def validate_camera(raw: Any) -> dict[str, Any]:
    """Return a clean camera dict (same keys as the app options) or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("camera must be an object")
    name = _text(raw, "name", 64)
    if not name:
        raise ValueError("name is required")
    cam_type = _choice(raw, "type", TYPES, "generic")
    host = _text(raw, "host", 253)
    if host and not _HOST.match(host):
        raise ValueError("host: invalid")
    cam: dict[str, Any] = {
        "name": name,
        "type": cam_type,
        "host": host,
        "port": _int(raw, "port", 1, 65535, None),
        "username": _text(raw, "username", 128),
        "password": _text(raw, "password", 256),
        "channel": _int(raw, "channel", 0, 63, 0),
        "use_https": bool(raw.get("use_https", cam_type == "reolink")),
        "rtsp_main": _url(raw, "rtsp_main", ("rtsp", "rtsps")),
        "rtsp_sub": _url(raw, "rtsp_sub", ("rtsp", "rtsps")),
        "snapshot_url": _url(raw, "snapshot_url", ("http", "https")),
        "record": _choice(raw, "record", RECORD, "on_demand" if cam_type == "ring" else "events"),
        "detect": _choice(
            raw,
            "detect",
            DETECT,
            {"reolink": "camera", "instar": "camera", "ring": "off"}.get(cam_type, "motion"),
        ),
        "objects": ", ".join(
            o
            for o in re.split(r"[,\s]+", str(raw.get("objects") or "person, vehicle"))
            if o in VALID_OBJECTS
        )
        or "person, vehicle",
        "motion_sensitivity": _int(raw, "motion_sensitivity", 1, 100, 50),
        "ha_entity": _text(raw, "ha_entity", 255),
    }
    if not (cam["rtsp_main"] or (cam_type in ("reolink", "instar") and host)):
        raise ValueError("a stream URL (rtsp_main) or a host is required")
    return cam


class ManagedCameras:
    """Load / change the cameras that were added from Home Assistant."""

    def __init__(self, path: Path = MANAGED_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        cams = []
        for raw in data.get("cameras", []) if isinstance(data, dict) else []:
            try:
                cams.append(validate_camera(raw))
            except ValueError:
                continue
        return cams

    def _save(self, cams: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "cameras": cams}, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def add(self, raws: list[Any], taken_names: set[str]) -> list[dict[str, Any]]:
        """Validate and append cameras. Returns the cameras that were added.

        Cameras with an ``ha_entity`` that is already managed are skipped, so
        a repeated request (e.g. a double click) never creates duplicates.
        """
        cams = self.load()
        known = {c["ha_entity"] for c in cams if c["ha_entity"]}
        names = {n.lower() for n in taken_names} | {c["name"].lower() for c in cams}
        added = []
        for raw in raws:
            cam = validate_camera(raw)
            if cam["ha_entity"] and cam["ha_entity"] in known:
                continue
            base, n = cam["name"], 2
            while cam["name"].lower() in names:
                cam["name"] = f"{base} {n}"
                n += 1
            names.add(cam["name"].lower())
            known.add(cam["ha_entity"])
            added.append(cam)
        if len(cams) + len(added) > MAX_CAMERAS:
            raise ValueError(f"at most {MAX_CAMERAS} cameras")
        if added:
            self._save(cams + added)
        return added

    def remove(self, name: str) -> bool:
        cams = self.load()
        left = [c for c in cams if c["name"] != name]
        if len(left) == len(cams):
            return False
        self._save(left)
        return True
