"""Unit tests for the pure logic of the Alcedo NVR app."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "alcedo_nvr" / "rootfs" / "app"))

from alcedo.config import parse_objects, parse_options, redact, slugify
from alcedo.export import remote_dir
from alcedo.llm import parse_verdict
from alcedo.media import Segment, ffmpeg_redacted, segment_start, select_segments
from alcedo.motion import HEIGHT, WIDTH, MotionAnalyzer, thresholds
from alcedo.sources import instar_label, parse_reolink_state
from alcedo.storage import expired_segments

OPTIONS = {
    "cameras": [
        {
            "name": "Einfahrt",
            "type": "reolink",
            "host": "192.168.1.20",
            "username": "admin",
            "password": "p@ss/w:rd",
        },
        {
            "name": "Garten",
            "type": "instar",
            "host": "192.168.1.21",
            "username": "admin",
            "password": "instar",
            "objects": "person, animal, bogus",
        },
        {"name": "Garten", "type": "generic", "rtsp_main": "rtsp://x/main"},
    ],
    "remote_password": "ftp-secret",
}


def test_parse_options_and_urls():
    cfg = parse_options(OPTIONS)
    reo, ins, gen = cfg.cameras
    assert [c.id for c in cfg.cameras] == ["einfahrt", "garten", "garten_2"]
    assert reo.main_url == "rtsp://admin:p%40ss%2Fw%3Ard@192.168.1.20:554/h264Preview_01_main"
    assert reo.sub_url.endswith("/h264Preview_01_sub")
    assert reo.use_https and reo.http_base == "https://192.168.1.20"
    assert reo.uses_camera_events and not reo.uses_motion
    assert ins.main_url.endswith("@192.168.1.21:554/11") and ins.sub_url.endswith(":554/12")
    assert ins.objects == ("person", "animal")
    assert not ins.use_https
    assert gen.detect == "motion" and gen.sub_url == "rtsp://x/main"
    assert "password" not in reo.public_dict()


def test_slugify_and_objects():
    assert slugify(" Einfahrt Nord! ") == "einfahrt_nord"
    assert slugify("###") == "camera"
    assert parse_objects("") == ("person", "vehicle")
    assert parse_objects("motion,person person") == ("motion", "person")


def test_redact():
    cfg = parse_options(OPTIONS)
    text = "rtsp://admin:p%40ss%2Fw%3Ard@h p@ss/w:rd ftp-secret instar"
    out = redact(text, cfg)
    assert "p@ss" not in out and "p%40ss" not in out and "ftp-secret" not in out
    assert "instar" not in out
    assert ffmpeg_redacted(["-i", "rtsp://u:pw@host/x"]) == "-i rtsp://***@host/x"


def test_reolink_state():
    reply = [
        {
            "cmd": "GetAiState",
            "code": 0,
            "value": {
                "people": {"alarm_state": 1, "support": 1},
                "vehicle": {"alarm_state": 0, "support": 1},
                "dog_cat": {"alarm_state": 1, "support": 1},
            },
        },
        {"cmd": "GetMdState", "code": 0, "value": {"state": 1}},
    ]
    assert parse_reolink_state(reply) == {"person", "animal", "motion"}
    assert parse_reolink_state([{"cmd": "GetAiState", "code": 1}]) == set()
    assert parse_reolink_state({"error": 1}) == set()


def test_instar_label():
    assert instar_label("1") == "motion"
    assert instar_label("12") == "person"
    assert instar_label(None) == "alarm"
    assert instar_label("99") == "alarm"


def test_segments():
    p = Path("/s/20261125T101500Z.mp4")
    assert segment_start(p) == 1795601700.0
    assert segment_start(Path("/s/garbage.mp4")) is None
    segs = [Segment(t, Path(f"{t}.mp4")) for t in (0, 10, 20, 30, 40)]
    # event 12..25 -> segments starting 10 and 20 (40 is still running)
    assert [s.start for s in select_segments(segs, 12, 25)] == [10, 20]
    assert [s.start for s in select_segments(segs, 0, 100)] == [0, 10, 20, 30]
    assert [s.start for s in expired_segments(segs, 15, now=45)] == [0, 10, 20]


def test_motion_analyzer():
    low_delta, low_area = thresholds(1)
    high_delta, high_area = thresholds(100)
    assert low_delta > high_delta and low_area > high_area
    m = MotionAnalyzer(50)
    still = np.full((HEIGHT, WIDTH), 100, np.uint8)
    assert m.feed(still) == 0.0
    assert not m.is_motion(m.feed(still))
    moved = still.copy()
    moved[20:60, 40:100] = 200  # a "person" appears
    assert m.is_motion(m.feed(moved))
    # whole image brighter (light switched on) is ignored
    m2 = MotionAnalyzer(50)
    m2.feed(still)
    assert m2.feed(np.full((HEIGHT, WIDTH), 180, np.uint8)) == 0.0


def test_parse_verdict():
    v = parse_verdict(
        'Sure! {"alarm": "true", "confidence": 1.7, "objects": "person",'
        ' "description": "Eine Person am Tor."}'
    )
    assert v == {
        "alarm": True,
        "confidence": 1.0,
        "objects": ["person"],
        "description": "Eine Person am Tor.",
    }
    assert parse_verdict('{"alarm": false, "confidence": "x"}')["confidence"] == 0.0
    with pytest.raises(ValueError):
        parse_verdict("no json here")


def test_remote_dir():
    assert remote_dir("/alcedo", "einfahrt", "2026-11-25") == "/alcedo/einfahrt/2026-11-25"
    assert remote_dir("../../etc", "c", "d") == "/etc/c/d"


def test_ring_defaults_and_on_demand_segments():
    from alcedo.media import recording_ranges

    cfg = parse_options(
        {
            "cameras": [
                {"name": "Ring Tür", "type": "ring", "rtsp_main": "rtsp://ring-mqtt:8554/abc_live"}
            ]
        }
    )
    ring = cfg.cameras[0]
    assert ring.record == "on_demand" and ring.detect == "off"
    assert not ring.uses_motion and not ring.uses_camera_events
    assert ring.main_url == ring.sub_url == "rtsp://ring-mqtt:8554/abc_live"
    segs = [Segment(t, Path(f"{t}.mp4")) for t in (0, 10, 20)]
    # recorder stopped -> the last segment is complete and included
    assert [s.start for s in select_segments(segs, 0, 25, include_last=True)] == [0, 10, 20]
    # timeline: two recordings with a gap
    segs = [Segment(t, Path(f"{t}.mp4")) for t in (0, 10, 20, 100, 110)]
    assert recording_ranges(segs, 10, 0, 1000) == [(0, 30), (100, 120)]
    assert recording_ranges(segs, 10, 15, 105) == [(15, 30), (100, 105)]
