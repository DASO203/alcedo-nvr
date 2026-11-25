"""Options of the app (read from /data/options.json written by the Supervisor)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit

OPTIONS_FILE = Path("/data/options.json")
_SLUG = re.compile(r"[^a-z0-9_]+")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

VALID_OBJECTS = ("person", "vehicle", "animal", "motion")


def slugify(name: str) -> str:
    """Return a safe id for a camera name ("Einfahrt Nord" -> "einfahrt_nord")."""
    name = name.strip().lower().translate(_UMLAUTS)
    slug = _SLUG.sub("_", name).strip("_")
    return slug or "camera"


def _cred(value: str) -> str:
    return quote(value, safe="")


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """One camera."""

    id: str
    name: str
    type: str  # reolink | instar | ring | generic
    host: str = ""
    port: int | None = None
    username: str = ""
    password: str = field(default="", repr=False)
    channel: int = 0
    use_https: bool = True
    rtsp_main: str = ""
    rtsp_sub: str = ""
    snapshot_url: str = ""
    record: str = "events"  # events | continuous | on_demand | off
    detect: str = "camera"  # camera | motion | both | off
    objects: tuple[str, ...] = ("person", "vehicle")
    motion_sensitivity: int = 50
    enabled: bool = True
    source: str = "config"  # config (app options) | ha (added from Home Assistant)
    ha_entity: str = ""  # Home Assistant entity / device the camera was taken from

    # ------------------------------------------------------------ stream urls

    def _auth(self) -> str:
        if not self.username:
            return ""
        return f"{_cred(self.username)}:{_cred(self.password)}@"

    @property
    def main_url(self) -> str:
        """RTSP URL of the high resolution stream (recording)."""
        if self.rtsp_main:
            return self.rtsp_main
        if self.type == "reolink":
            ch = f"{self.channel + 1:02d}"
            return f"rtsp://{self._auth()}{self.host}:554/h264Preview_{ch}_main"
        if self.type == "instar":
            return f"rtsp://{self._auth()}{self.host}:554/11"
        return ""

    @property
    def sub_url(self) -> str:
        """RTSP URL of the low resolution stream (motion detection)."""
        if self.rtsp_sub:
            return self.rtsp_sub
        if self.type == "reolink":
            ch = f"{self.channel + 1:02d}"
            return f"rtsp://{self._auth()}{self.host}:554/h264Preview_{ch}_sub"
        if self.type == "instar":
            return f"rtsp://{self._auth()}{self.host}:554/12"
        return self.main_url

    @property
    def http_base(self) -> str:
        """Base URL of the camera web API."""
        scheme = "https" if self.use_https else "http"
        port = f":{self.port}" if self.port else ""
        return f"{scheme}://{self.host}{port}"

    @property
    def uses_camera_events(self) -> bool:
        return self.detect in ("camera", "both")

    @property
    def uses_motion(self) -> bool:
        return self.detect in ("motion", "both") or (
            self.detect == "camera" and self.type == "generic"
        )

    @property
    def stream_id(self) -> str:
        """Main stream without credentials, used to recognise a camera ("host:port/path")."""
        try:
            parts = urlsplit(self.main_url)
            host = (parts.hostname or "").lower()
            port = parts.port or 554
        except ValueError:
            return ""
        return f"{host}:{port}{parts.path}" if host else ""

    def public_dict(self) -> dict[str, Any]:
        """Info without credentials (for API / UI / logs)."""
        return {
            "source": self.source,
            "ha_entity": self.ha_entity,
            "stream_id": self.stream_id,
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "host": self.host,
            "record": self.record,
            "detect": self.detect,
            "objects": list(self.objects),
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class LlmConfig:
    enabled: bool
    provider: str
    url: str
    model: str
    language: str
    prompt: str
    frames: int
    min_confidence: float
    timeout: int


@dataclass(frozen=True, slots=True)
class ExportConfig:
    only_confirmed: bool
    local: bool
    local_path: str
    remote: bool
    protocol: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    path: str
    host_key: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    cameras: tuple[CameraConfig, ...]
    storage_path: str
    retention_days: int
    max_storage_percent: int
    pre_seconds: int
    post_seconds: int
    segment_seconds: int
    llm: LlmConfig
    export: ExportConfig
    log_level: str


def parse_objects(value: str | None) -> tuple[str, ...]:
    """Parse "person, vehicle" into a validated tuple."""
    if not value:
        return ("person", "vehicle")
    items = []
    for raw in re.split(r"[,\s]+", value.lower()):
        if raw in VALID_OBJECTS and raw not in items:
            items.append(raw)
    return tuple(items) or ("person", "vehicle")


def _default_detect(cam_type: str) -> str:
    """Reolink/Instar: camera events. Ring: triggered from HA. Others: own motion."""
    return {"reolink": "camera", "instar": "camera", "ring": "off"}.get(cam_type, "motion")


def parse_options(data: dict[str, Any], managed: list[dict[str, Any]] | None = None) -> AppConfig:
    """Build the typed configuration from the Supervisor options.

    ``managed`` are the cameras added from Home Assistant (/data/cameras.json).
    They come after the option cameras, so existing camera ids never change.
    """
    cameras: list[CameraConfig] = []
    used: set[str] = set()
    raws = [(raw, "config") for raw in data.get("cameras", [])]
    raws += [(raw, "ha") for raw in managed or []]
    for raw, source in raws:
        cam_id = slugify(raw["name"])
        base, n = cam_id, 2
        while cam_id in used:
            cam_id = f"{base}_{n}"
            n += 1
        used.add(cam_id)
        cam_type = raw.get("type", "generic")
        cameras.append(
            CameraConfig(
                id=cam_id,
                name=raw["name"],
                type=cam_type,
                host=(raw.get("host") or "").strip(),
                port=raw.get("port"),
                username=raw.get("username") or "",
                password=raw.get("password") or "",
                channel=int(raw.get("channel") or 0),
                use_https=bool(raw.get("use_https", cam_type == "reolink")),
                rtsp_main=raw.get("rtsp_main") or "",
                rtsp_sub=raw.get("rtsp_sub") or "",
                snapshot_url=raw.get("snapshot_url") or "",
                record=raw.get("record") or ("on_demand" if cam_type == "ring" else "events"),
                detect=raw.get("detect") or _default_detect(cam_type),
                objects=parse_objects(raw.get("objects")),
                motion_sensitivity=int(raw.get("motion_sensitivity") or 50),
                enabled=bool(raw.get("enabled", True)),
                source=source,
                ha_entity=raw.get("ha_entity") or "",
            )
        )
    return AppConfig(
        cameras=tuple(cameras),
        storage_path=data.get("storage_path", "/media/alcedo"),
        retention_days=int(data.get("retention_days", 7)),
        max_storage_percent=int(data.get("max_storage_percent", 85)),
        pre_seconds=int(data.get("pre_seconds", 5)),
        post_seconds=int(data.get("post_seconds", 10)),
        segment_seconds=int(data.get("segment_seconds", 10)),
        llm=LlmConfig(
            enabled=bool(data.get("llm_enabled", False)),
            provider=data.get("llm_provider", "ollama"),
            url=str(data.get("llm_url", "")).rstrip("/"),
            model=data.get("llm_model", ""),
            language=data.get("llm_language", "de"),
            prompt=data.get("llm_prompt", ""),
            frames=int(data.get("llm_frames", 2)),
            min_confidence=float(data.get("llm_min_confidence", 0.6)),
            timeout=int(data.get("llm_timeout", 120)),
        ),
        export=ExportConfig(
            only_confirmed=bool(data.get("export_only_confirmed", True)),
            local=bool(data.get("export_local", True)),
            local_path=data.get("export_local_path", "/media/alcedo/export"),
            remote=bool(data.get("export_remote", False)),
            protocol=data.get("remote_protocol", "sftp"),
            host=data.get("remote_host") or "",
            port=int(data.get("remote_port") or 22),
            username=data.get("remote_username") or "",
            password=data.get("remote_password") or "",
            path=data.get("remote_path") or "/alcedo",
            host_key=data.get("remote_host_key") or "",
        ),
        log_level=data.get("log_level", "info"),
    )


def load_options(
    path: Path = OPTIONS_FILE, managed: list[dict[str, Any]] | None = None
) -> AppConfig:
    """Load options from disk (plus the cameras added from Home Assistant)."""
    return parse_options(json.loads(path.read_text(encoding="utf-8")), managed)


def redact(text: str, cfg: AppConfig) -> str:
    """Remove passwords from a string (ffmpeg errors may echo URLs)."""
    secrets = {c.password for c in cfg.cameras if c.password}
    secrets |= {_cred(s) for s in secrets}
    if cfg.export.password:
        secrets.add(cfg.export.password)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text
