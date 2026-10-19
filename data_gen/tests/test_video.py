"""Tests for the SYNTHETIC CCTV relabel generator (MEVA CC BY 4.0 footage).

Fast tests run offline on a tiny synthetic numpy video. The download test only runs with
CASELENS_DATA_GEN_NETWORK=1.
"""

from __future__ import annotations

import json
import os
import struct
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from caselens_data_gen.common import SYNTHETIC_NOTICE, sha256_file  # noqa: E402
from caselens_data_gen.story import load_story  # noqa: E402
from caselens_data_gen.video import render_one  # noqa: E402
from caselens_data_gen.video.meva import ensure_source, parse_avi_index  # noqa: E402
from caselens_data_gen.video.render import (  # noqa: E402
    ClipTiming,
    draw_overlay,
    fill_times,
    format_stamp,
    output_size,
    scale_bbox,
)
from caselens_data_gen.video.spec import ClipSpec, Sidecar, SourceSpec, load_sources  # noqa: E402

ANCHOR = datetime.fromisoformat("2026-03-14T22:41:05-07:00")


def _timing(**kw: object) -> ClipTiming:
    args: dict[str, object] = {
        "start_frame": 3300,
        "end_frame": 4770,
        "anchor_frame": 3360,
        "anchor_wall_clock": ANCHOR,
        "src_fps": 30.0,
        "out_fps": 15.0,
    }
    args.update(kw)
    return ClipTiming(**args)  # type: ignore[arg-type]


def test_offset_maps_anchor_exactly_and_keeps_real_time() -> None:
    t = _timing()
    assert t.wall_clock(3360) == ANCHOR
    assert t.wall_clock(3390) == ANCHOR + timedelta(seconds=1)
    assert t.recorded_at == ANCHOR - timedelta(seconds=2)
    assert t.step == 2
    assert t.frame_count == 735
    assert t.duration_ms == 49000
    assert t.clip_ms(3360) == 2000
    assert t.out_index(3360) == 30
    assert t.out_index(3361) == 30
    assert t.out_index(10_000) == t.frame_count - 1
    assert t.out_wall_clock(30) == ANCHOR
    assert format_stamp(t.out_wall_clock(31)) == "2026-03-14 22:41:05"
    assert format_stamp(t.out_wall_clock(45)) == "2026-03-14 22:41:06"


def test_bad_output_fps_rejected() -> None:
    with pytest.raises(ValueError):
        _ = _timing(out_fps=12.0).step


def test_fill_times_and_scaling() -> None:
    t = _timing()
    assert fill_times("enters {t:3360}, parks {t:3630}", t) == "enters 22:41:05, parks 22:41:14"
    assert output_size(1920, 1080, 960) == (960, 540)
    assert output_size(352, 240, 704) == (704, 480)
    assert scale_bbox((100, 50, 300, 250), 1920, 1080, 960, 540) == (50.0, 25.0, 150.0, 125.0)


def test_overlay_burns_banner_and_leaves_middle_untouched() -> None:
    frame = np.full((240, 320, 3), 128, dtype=np.uint8)
    out = draw_overlay(frame, "CAM-A", "2026-03-14 22:41:05")
    assert out.shape == frame.shape
    assert not np.array_equal(out[:12], frame[:12])
    assert not np.array_equal(out[-12:], frame[-12:])
    assert np.array_equal(out[60:180], frame[60:180])
    assert np.array_equal(frame, np.full((240, 320, 3), 128, dtype=np.uint8))


def test_sources_json_matches_the_stories() -> None:
    sources = load_sources()
    stories = {s: load_story(s) for s in ("riverside_lot", "harbor_street")}
    covered = {c.evidence_id for c in sources.clips} | set(sources.not_produced)
    for story in stories.values():
        planned = {p.id for p in story.evidence_plan if p.type == "video"}
        assert planned <= covered, planned - covered
    for clip in sources.clips:
        story = stories[clip.case_slug]
        plan = next(p for p in story.evidence_plan if p.id == clip.evidence_id)
        source = sources.source(clip.source)
        t = ClipTiming(
            clip.start_frame,
            clip.end_frame,
            clip.anchor_frame,
            clip.anchor_wall_clock,
            source.fps,
            clip.out_fps,
        )
        story_times = {tr.t_start for tr in plan.tracks} | {tr.t_end for tr in plan.tracks}
        assert t.wall_clock(clip.anchor_frame) in story_times, clip.evidence_id
        ids = {e.id for e in story.events} | {v.id for v in story.vehicles}
        ids |= {p.id for p in story.people}
        for fact in clip.facts:
            assert fact.fact_id in ids, fact.fact_id
            assert fact.fact_id in plan.covers_events + plan.covers_entities, fact.fact_id
        assert clip.ungrounded, f"{clip.evidence_id}: list what the footage does not show"


def _tiny_avi(path: Path, frames: int = 90) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 30.0, (64, 48))
    assert writer.isOpened()
    for i in range(frames):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[:, : (i % 64) + 1] = (40, 160, 220)
        writer.write(frame)
    writer.release()


def test_parse_avi_index_on_a_local_avi(tmp_path: Path) -> None:
    path = tmp_path / "tiny.avi"
    _tiny_avi(path, frames=12)
    data = path.read_bytes()
    pos = data.rindex(b"idx1")
    index = parse_avi_index(data[:65536], data[pos:])
    assert len(index.chunks) == 12
    assert index.is_keyframe(0)
    first, last = index.byte_range(0, 12)
    assert data[first + 2 : first + 4] == b"dc"
    (size,) = struct.unpack("<I", data[first + 4 : first + 8])
    assert size == index.chunks[0][2]
    assert last < pos
    assert index.header.endswith(b"movi")


def test_render_and_sidecar_on_synthetic_video(tmp_path: Path) -> None:
    src_path = tmp_path / "tiny_src.avi"
    _tiny_avi(src_path)
    sha = sha256_file(src_path)
    assert ensure_source(tmp_path, "tiny_src", "drops-test/x.avi", sha, None) == src_path
    with pytest.raises(RuntimeError):
        ensure_source(tmp_path, "tiny_src", "drops-test/x.avi", "0" * 64, None)

    story = load_story("riverside_lot")
    source = SourceSpec(
        name="tiny_src",
        key="drops-test/x.avi",
        sha256=sha,
        width=64,
        height=48,
        view="SYNTHETIC test pattern",
    )
    clip = ClipSpec.model_validate(
        {
            "evidence_id": "vid_02_market_lot_shooting",
            "case_slug": "riverside_lot",
            "source": "tiny_src",
            "start_frame": 30,
            "end_frame": 90,
            "anchor_frame": 45,
            "anchor_wall_clock": "2026-03-14T22:41:05-07:00",
            "anchor_reason": "test",
            "out_width": 64,
            "tracks": [
                {
                    "track_id": "trk_201",
                    "label": "sedan",
                    "color_as_story": "gray sedan",
                    "entity_id": "v_gray_sedan",
                    "start_frame": 45,
                    "end_frame": 90,
                    "key_frame": 60,
                    "bbox": [8, 8, 32, 24],
                    "summary": "enters {t:45}, parks {t:60}",
                }
            ],
            "facts": [
                {
                    "fact_id": "ev_18",
                    "track_id": "trk_201",
                    "start_frame": 45,
                    "end_frame": 60,
                    "key_frame": 60,
                    "bbox": [8, 8, 32, 24],
                    "note": "test",
                }
            ],
            "ungrounded": ["everything else"],
        }
    )
    produced = render_one(story, clip, source, src_path, tmp_path / "video")

    assert produced.path.exists() and produced.evidence_type == "video"
    capture = cv2.VideoCapture(str(produced.path))
    assert capture.get(cv2.CAP_PROP_FPS) == pytest.approx(15.0)
    count = 0
    while capture.read()[0]:
        count += 1
    capture.release()
    assert count == 30
    assert produced.recorded_at == "2026-03-14T22:41:04.500-07:00"

    (fact,) = produced.facts
    assert fact.locator == {
        "kind": "video",
        "start_ms": 500,
        "end_ms": 1000,
        "frame_idx": 15,
        "bbox": [8.0, 8.0, 32.0, 24.0],
        "track_id": "trk_201",
    }
    raw = json.loads((tmp_path / "video" / "vid_02_market_lot_shooting.json").read_text())
    sidecar = Sidecar.model_validate(raw)
    assert sidecar.notice == SYNTHETIC_NOTICE
    assert "SYNTHETIC" in sidecar.notice
    assert sidecar.source.license == "CC BY 4.0"
    assert sidecar.camera_id == "cam_market_lot"
    assert sidecar.frame_count == 30 and sidecar.duration_ms == 2000
    (track,) = sidecar.tracks
    assert track.summary == "enters 22:41:05, parks 22:41:05"
    assert track.wall_clock_start == ANCHOR
    assert track.keyframe.frame_idx == 15
    assert sidecar.ungrounded_story_facts == ["everything else"]


@pytest.mark.skipif(
    os.environ.get("CASELENS_DATA_GEN_NETWORK") != "1",
    reason="set CASELENS_DATA_GEN_NETWORK=1 to download from the public MEVA bucket",
)
def test_download_matches_pinned_sha(tmp_path: Path) -> None:
    sources = load_sources()
    small = min(
        (s for s in sources.sources if s.frame_range is None), key=lambda s: s.width * s.height
    )
    path = ensure_source(tmp_path, small.name, small.key, small.sha256, small.frame_range)
    assert sha256_file(path) == small.sha256
