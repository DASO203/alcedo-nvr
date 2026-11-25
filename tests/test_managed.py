"""Cameras taken over from Home Assistant (managed cameras + API)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "alcedo_nvr" / "rootfs" / "app"))

from alcedo.api import Api
from alcedo.config import parse_options, redact
from alcedo.managed import ManagedCameras, validate_camera

REOLINK = {
    "name": "Einfahrt",
    "type": "reolink",
    "host": "192.168.1.20",
    "username": "admin",
    "password": "geh@im",
    "channel": 0,
    "rtsp_main": "rtsp://admin:geh%40im@192.168.1.20:554/Preview_01_main",
    "rtsp_sub": "rtsp://admin:geh%40im@192.168.1.20:554/Preview_01_sub",
    "objects": "person",
    "ha_entity": "reolink:abc:0",
}
RING = {
    "name": "Haustür",
    "type": "ring",
    "rtsp_main": "rtsp://03cabcc9-ring-mqtt:8554/0123_live",
    "ha_entity": "ring:dev1",
}


def test_validate_defaults_and_errors() -> None:
    ring = validate_camera(RING)
    assert ring["record"] == "on_demand" and ring["detect"] == "off"
    reo = validate_camera(REOLINK)
    assert reo["detect"] == "camera" and reo["objects"] == "person" and reo["use_https"]
    with pytest.raises(ValueError, match="name"):
        validate_camera({"type": "generic", "rtsp_main": "rtsp://x/1"})
    with pytest.raises(ValueError, match="rtsp_main"):
        validate_camera({"name": "x", "rtsp_main": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="host"):
        validate_camera({"name": "x", "type": "reolink", "host": "a b;rm"})
    with pytest.raises(ValueError, match="stream"):
        validate_camera({"name": "x", "type": "generic"})
    with pytest.raises(ValueError, match="record"):
        validate_camera({**RING, "record": "always"})


def test_store_dedupes_and_renames(tmp_path: Path) -> None:
    store = ManagedCameras(tmp_path / "cameras.json")
    added = store.add([REOLINK, RING], taken_names={"einfahrt"})
    assert [c["name"] for c in added] == ["Einfahrt 2", "Haustür"]
    assert store.add([REOLINK], set()) == []  # same ha_entity again -> skipped
    assert len(store.load()) == 2
    assert store.remove("Haustür") and not store.remove("Haustür")
    assert [c["name"] for c in store.load()] == ["Einfahrt 2"]


def test_merge_with_options_keeps_ids() -> None:
    options = {"cameras": [{"name": "Einfahrt", "type": "reolink", "host": "192.168.1.5"}]}
    cfg = parse_options(options, [validate_camera(REOLINK), validate_camera(RING)])
    assert [c.id for c in cfg.cameras] == ["einfahrt", "einfahrt_2", "haustuer"]
    assert [c.source for c in cfg.cameras] == ["config", "ha", "ha"]
    reo = cfg.cameras[1]
    assert reo.main_url.endswith("/Preview_01_main") and reo.ha_entity == "reolink:abc:0"
    pub = reo.public_dict()
    assert pub["stream_id"] == "192.168.1.20:554/Preview_01_main"
    assert "geh" not in str(pub)
    assert redact("rtsp://admin:geh%40im@x", cfg) == "rtsp://admin:***@x"


class FakeApp:
    def __init__(self, tmp: Path) -> None:
        self.api_key = "k"
        self.managed = ManagedCameras(tmp / "cameras.json")
        self.cfg = parse_options({"cameras": [{"name": "Alt", "rtsp_main": "rtsp://a/1"}]})
        self.cameras = {c.id: c for c in self.cfg.cameras}
        self.reloads = 0

    def request_reload(self, delay: float = 1.0) -> None:
        self.reloads += 1

    def refresh(self) -> None:
        self.cfg = parse_options(
            {"cameras": [{"name": "Alt", "rtsp_main": "rtsp://a/1"}]}, self.managed.load()
        )
        self.cameras = {c.id: c for c in self.cfg.cameras}


def test_camera_api(tmp_path: Path) -> None:
    async def run() -> None:
        app = FakeApp(tmp_path)
        api = Api(app)
        web_app = web.Application(middlewares=[api._token_middleware])
        api._routes(web_app, "/api")
        client = TestClient(TestServer(web_app))
        await client.start_server()
        try:
            h = {"Authorization": "Bearer k"}
            assert (await client.post("/api/cameras", json={"cameras": [RING]})).status == 401
            r = await client.post("/api/cameras", json={"cameras": [RING]}, headers=h)
            assert r.status == 200 and (await r.json())["added"] == ["Haustür"]
            assert app.reloads == 1
            bad = await client.post("/api/cameras", json={"cameras": [{"name": "x"}]}, headers=h)
            assert bad.status == 400 and "stream" in await bad.text()
            app.refresh()
            cams = await (await client.get("/api/cameras", headers=h)).json()
            assert [c["source"] for c in cams] == ["config", "ha"]
            assert (await client.delete("/api/cameras/alt", headers=h)).status == 409
            r = await client.delete(f"/api/cameras/{cams[1]['id']}", headers=h)
            assert r.status == 200 and app.reloads == 2 and app.managed.load() == []
        finally:
            await client.close()

    asyncio.run(run())
