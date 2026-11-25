"""Event lifecycle test with fake camera and fake LLM (no ffmpeg needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "alcedo_nvr" / "rootfs" / "app"))

from alcedo import events as events_mod
from alcedo.config import parse_options
from alcedo.events import EventManager, EventStore
from alcedo.export import Exporter
from alcedo.llm import Verdict

JPEG = b"\xff\xd8fakejpeg\xff\xd9"


class FakeReviewer:
    def __init__(self, alarm: bool) -> None:
        self.alarm = alarm
        self.enabled = True
        self.calls = 0

    async def review(self, camera, trigger, images):
        self.calls += 1
        return Verdict(
            alarm=self.alarm,
            confidence=0.9,
            objects=["person"],
            description="Person am Tor",
            model="fake",
            seconds=0.1,
        )

    def is_confirmed(self, verdict):
        return verdict.alarm


async def _run(tmp: Path, alarm: bool):
    opts = {
        "cameras": [{"name": "Tor", "type": "generic", "rtsp_main": "rtsp://x", "record": "off"}],
        "storage_path": str(tmp / "store"),
        "post_seconds": 2,
        "segment_seconds": 2,
        "llm_enabled": True,
        "llm_url": "http://x",
        "llm_model": "m",
        "llm_frames": 2,
        "export_local": True,
        "export_local_path": str(tmp / "export"),
        "export_only_confirmed": True,
    }
    cfg = parse_options(opts)
    store = EventStore(tmp / "events.db")
    published = []

    async def snapshot(cam):
        return JPEG

    async def no_scale(data, width=768):
        return data

    events_mod.scale_jpeg = no_scale
    reviewer = FakeReviewer(alarm)
    mgr = EventManager(cfg, store, reviewer, Exporter(cfg.export), snapshot, published.append)
    await mgr.trigger("tor", "motion", "motion")
    await asyncio.sleep(0.5)
    await mgr.trigger("tor", "motion", "person")  # same event, new label
    for _ in range(40):
        await asyncio.sleep(0.5)
        if any(m["type"] == "event_end" for m in published):
            break
    rows = await store.recent()
    return rows, published, reviewer


def test_confirmed_event_is_exported(tmp_path: Path):
    rows, published, reviewer = asyncio.run(_run(tmp_path, alarm=True))
    assert len(rows) == 1
    ev = rows[0]
    assert ev["confirmed"] is True and ev["state"] == "done"
    assert ev["labels"] == ["motion", "person"]
    assert ev["verdict"]["description"] == "Person am Tor"
    assert ev["exported"] == {"local": "ok"}
    exported = list((tmp_path / "export" / "tor").rglob("*.jpg"))
    assert len(exported) == 1 and exported[0].read_bytes() == JPEG
    assert reviewer.calls == 1
    types = [m["type"] for m in published]
    assert types[0] == "event_start" and "event_verdict" in types and types[-1] == "event_end"


def test_rejected_event_is_not_exported(tmp_path: Path):
    rows, _, _ = asyncio.run(_run(tmp_path, alarm=False))
    assert rows[0]["confirmed"] is False
    assert rows[0]["exported"] == {}
    assert not (tmp_path / "export").exists()
