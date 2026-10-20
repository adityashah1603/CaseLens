"""Video pipeline tests. All data is SYNTHETIC: generated frames and clips (moving rectangles),
made-up boxes, a fake detector / tracker / VLM and (heavy tests only) the generated demo video."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from caselens_workers import jobs
from caselens_workers.chunks import VideoLocator
from caselens_workers.db import Conn
from caselens_workers.extraction.timeres import case_timezone
from caselens_workers.pipeline import Pipeline, run_job
from caselens_workers.pipelines import video
from caselens_workers.settings import WorkerSettings
from caselens_workers.stages.embed import EMBEDDING_DIM, make_embed_stage
from caselens_workers.stages.extract import make_extract_stage
from caselens_workers.testing import SeededEvidence, as_role
from caselens_workers.video_media import _Boxes, _lapjv, crop_color, in_overlay_band
from caselens_workers.video_tracks import (
    Detection,
    FrameDetections,
    TrackPoint,
    VideoMeta,
    VideoSettings,
    color_name,
    edge_side,
    finalize_tracks,
    format_time,
    merge_fragments,
    region_name,
    scenes_from_cuts,
    select_keyframes,
    track_summary,
)

REPO = Path(__file__).resolve().parents[2]
HEAVY = os.environ.get("CASELENS_HEAVY") == "1"
TZ = case_timezone()
START = datetime.fromisoformat("2026-03-14T22:41:03-07:00")
META = VideoMeta(fps=15.0, frame_count=750, duration_ms=50_000, width=960, height=540)


def _pt(ms: int, box: tuple[float, float, float, float], label: str = "car") -> TrackPoint:
    return TrackPoint(
        frame_idx=round(ms * 15 / 1000),
        ms=ms,
        bbox=box,
        conf=0.9,
        label=label,
        color="gray" if label == "car" else None,
    )


def _path(
    t0: int, t1: int, a: tuple[float, float], b: tuple[float, float], size: tuple[float, float]
) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Boxes every 200 ms moving linearly from center a to center b."""
    out = []
    for ms in range(t0, t1 + 1, 200):
        f = (ms - t0) / max(1, t1 - t0)
        cx, cy = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        out.append((ms, (cx - size[0] / 2, cy - size[1] / 2, cx + size[0] / 2, cy + size[1] / 2)))
    return out


def _close(got: list[int], want: list[int], tol: int = 500) -> bool:
    return len(got) == len(want) and all(abs(g - w) <= tol for g, w in zip(got, want, strict=True))


# --- stage graph -------------------------------------------------------------------------------


def test_stage_graph() -> None:
    order: list[str] = []
    stage: str | None = video.PIPELINE.first
    while stage is not None:
        order.append(stage)
        stage = video.PIPELINE.next[stage]
    assert order == [
        "scene_detect",
        "keyframes",
        "detect",
        "track",
        "caption",
        "embed",
        "extract",
    ]


# --- keyframes and scenes ----------------------------------------------------------------------


def test_select_keyframes_one_per_second_plus_scene_starts() -> None:
    meta = VideoMeta(fps=15.0, frame_count=45, duration_ms=3000, width=64, height=48)
    scenes = scenes_from_cuts(meta, [(0, 22), (22, 45)])
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 1467), (1467, 3000)]
    kfs = select_keyframes(meta, scenes, 1.0)
    assert [(k.frame_idx, k.scene_index, k.scene_start) for k in kfs] == [
        (0, 0, True),
        (15, 0, False),
        (22, 1, True),
        (30, 1, False),
    ]
    assert kfs[2].name == "keyframes/22.jpg" and kfs[2].ms == 1467
    # No cut: one scene for the whole clip.
    (only,) = scenes_from_cuts(meta, [])
    assert (only.start_frame, only.end_frame, only.end_ms) == (0, 45, 3000)


def _synthetic_clip(path: Path, frames: int = 45, cut_at: int = 22) -> None:
    """SYNTHETIC clip: a white rectangle moving right; the background flips at `cut_at`."""
    import cv2

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 15.0, (160, 96))
    for i in range(frames):
        bg = (40, 40, 40) if i < cut_at else (200, 120, 30)
        img = np.full((96, 160, 3), bg, dtype=np.uint8)
        x = 10 + 2 * i
        cv2.rectangle(img, (x, 40), (x + 24, 60), (235, 235, 235), -1)
        writer.write(img)
    writer.release()


def test_scene_cut_and_keyframes_on_synthetic_clip(tmp_path: Path) -> None:
    from caselens_workers.video_media import detect_cuts, probe, read_frames

    clip = tmp_path / "SYNTHETIC_clip.mp4"
    _synthetic_clip(clip)
    meta = probe(clip)
    assert (meta.width, meta.height, meta.frame_count, meta.fps) == (160, 96, 45, 15.0)
    cuts = detect_cuts(clip, threshold=27.0, min_scene_len=10)
    assert [s for s, _ in cuts] == [0, 22]
    kfs = select_keyframes(meta, scenes_from_cuts(meta, cuts), 1.0)
    wanted = {k.frame_idx for k in kfs}
    assert wanted == {0, 15, 22, 30}
    got = dict(read_frames(clip, lambda i: i in wanted))
    assert sorted(got) == [0, 15, 22, 30]
    assert got[0].shape == (96, 160, 3)


# --- color naming ------------------------------------------------------------------------------


def _car_crop(bgr: tuple[int, int, int], seed: int = 3) -> Any:
    """SYNTHETIC car crop: body color + noise, a dark windshield band and asphalt margins."""
    rng = np.random.default_rng(seed)
    img = np.full((60, 100, 3), (110, 110, 110), dtype=np.int16)  # asphalt around the car
    img[6:54, 8:92] = bgr
    img[14:24, 20:80] = (25, 25, 30)  # windshield
    img += rng.integers(-8, 9, size=img.shape, dtype=np.int16)
    return np.clip(img, 0, 255).astype(np.uint8)


@pytest.mark.parametrize(
    ("bgr", "name"),
    [
        ((165, 162, 160), "gray"),
        ((235, 236, 238), "white"),
        ((22, 22, 24), "black"),
        ((30, 30, 200), "red"),
        ((190, 90, 30), "blue"),
        ((40, 160, 40), "green"),
        ((30, 200, 230), "yellow"),
    ],
)
def test_color_name_on_synthetic_crops(bgr: tuple[int, int, int], name: str) -> None:
    import cv2

    crop = _car_crop(bgr)
    assert color_name(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)) == name
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    frame[50:110, 100:200] = crop
    assert crop_color(frame, (100.0, 50.0, 200.0, 110.0)) == name


def test_color_name_rejects_tiny_crops() -> None:
    assert color_name(np.zeros((2, 2, 3), dtype=np.uint8)) is None


def test_overlay_band_filter() -> None:
    assert in_overlay_band((10.0, 2.0, 200.0, 26.0), 540, 0.06)  # overlay text line
    assert not in_overlay_band((10.0, 2.0, 200.0, 140.0), 540, 0.06)  # tall car at the top
    assert not in_overlay_band((10.0, 200.0, 60.0, 240.0), 540, 0.06)


# --- ByteTrack plumbing ------------------------------------------------------------------------


def test_lapjv_standin_matches_brute_force() -> None:
    import itertools

    rng = np.random.default_rng(5)
    for _ in range(30):
        n, m = int(rng.integers(1, 5)), int(rng.integers(1, 5))
        cost = rng.uniform(0, 1, size=(n, m))
        limit = 0.5
        _, x, y = _lapjv(cost, extend_cost=True, cost_limit=limit)
        got = sum(cost[i, x[i]] for i in range(n) if x[i] >= 0)
        unmatched = (n - (x >= 0).sum()) + (m - (y >= 0).sum())
        best = np.inf
        for k in range(min(n, m) + 1):
            for rows in itertools.combinations(range(n), k):
                for cols in itertools.permutations(range(m), k):
                    pairs = list(zip(rows, cols, strict=True))
                    if all(cost[r, c] <= limit for r, c in pairs):
                        total = sum(cost[r, c] for r, c in pairs)
                        best = min(best, total + (n + m - 2 * k) * limit / 2)
        assert got + unmatched * limit / 2 == pytest.approx(best)
        for i in range(n):
            if x[i] >= 0:
                assert y[x[i]] == i and cost[i, x[i]] <= limit
    _, x, y = _lapjv(np.zeros((0, 3)), cost_limit=0.5)
    assert len(x) == 0 and list(y) == [-1, -1, -1]


def test_boxes_view_for_bytetrack() -> None:
    boxes = _Boxes([[0, 0, 10, 20], [5, 5, 9, 9]], [0.9, 0.2], [2, 0])
    assert boxes.xywh.tolist() == [[5, 10, 10, 20], [7, 7, 4, 4]]
    high = boxes[boxes.conf > 0.5]
    assert len(high) == 1 and high.cls.tolist() == [2]


# --- track post-processing ---------------------------------------------------------------------


def _scenario() -> dict[int, list[TrackPoint]]:
    """A gray car enters at the top right, drives to the left side and parks; ByteTrack lost it
    for 1 s on the way (two fragments). A person gets out beside it. A 0.6 s blip is noise."""
    car = _path(2000, 7000, (880, 60), (300, 260), (160, 90))
    car += _path(7000, 11000, (300, 260), (120, 240), (160, 90))[1:]
    car += [(ms, car[-1][1]) for ms in range(11200, 50001, 200)]
    frag_a = [_pt(ms, b) for ms, b in car if ms < 5000]
    frag_b = [_pt(ms, b) for ms, b in car if ms >= 6000]
    person = [
        _pt(ms, b, "person") for ms, b in _path(18000, 23000, (150, 240), (40, 300), (30, 70))
    ]
    blip = [_pt(ms, (500, 400, 530, 470), "person") for ms in (30000, 30200, 30400, 30600)]
    return {7: frag_a, 9: frag_b, 12: person, 15: blip}


def test_merge_fragments_joins_close_gaps_only() -> None:
    raw = _scenario()
    tracks = [([k], v) for k, v in sorted(raw.items())]
    merged = merge_fragments(tracks, max_gap_ms=3000, max_distance=1.0)
    assert sorted(ids for ids, _ in merged) == [[7, 9], [12], [15]]
    assert len(merge_fragments(tracks, max_gap_ms=500, max_distance=1.0)) == 4
    # A far-away fragment of the same class is a different object.
    far = [_pt(ms, (10, 400, 170, 490)) for ms in range(6000, 9000, 200)]
    assert len(merge_fragments([([1], raw[7]), ([2], far)], 3000, 1.0)) == 2


def test_finalize_tracks_motion_and_summary() -> None:
    tracks = finalize_tracks(_scenario(), META, VideoSettings(), sample_step_ms=200)
    assert [(t.track_id, t.label, t.color, t.merged_from) for t in tracks] == [
        ("t1", "car", "gray", [7, 9]),
        ("t2", "person", None, [12]),
    ]
    car, person = tracks
    # Motion times are coarse: within half a second of the true change.
    assert [e.kind for e in car.motion] == ["enters", "parks", "visible_at_end"]
    assert _close([e.ms for e in car.motion], [2000, 11000, 50_000])
    assert track_summary(car, START, TZ, tracks) == (
        "Track 1 (car, gray/silver) on 2026-03-14: enters frame from the top right at 22:41:05, "
        "parks near the left side at 22:41:14, still visible near the left side when the clip "
        "ends at 22:41:53."
    )
    assert track_summary(person, START, TZ, tracks) == (
        "Track 2 (person) on 2026-03-14: appears beside Track 1 (car, gray/silver) near the "
        "left side at 22:41:21, exits frame to the left at 22:41:26."
    )
    assert track_summary(car, None, TZ, close_up="a silver sedan.").endswith(
        "at 0:50 into the clip. Close-up: a silver sedan."
    )


def test_stationary_and_stop_then_move() -> None:
    cfg = VideoSettings()
    parked = {1: [_pt(ms, (700, 300, 860, 390)) for ms in range(3000, 50001, 200)]}
    (t,) = finalize_tracks(parked, META, cfg, 200)
    # Detected late but never moved: "first seen", not an entrance from the right edge.
    assert [e.kind for e in t.motion] == ["appears", "stationary", "visible_at_end"]
    assert "first seen near the right side at 22:41:06, stationary near the right side" in (
        track_summary(t, START, TZ)
    )
    route = _path(0, 4000, (100, 300), (500, 300), (100, 60))
    route += [(ms, route[-1][1]) for ms in range(4200, 9001, 200)]
    route += _path(9000, 13000, (500, 300), (940, 300), (100, 60))[1:]
    (t,) = finalize_tracks({1: [_pt(ms, b) for ms, b in route]}, META, cfg, 200)
    assert [e.kind for e in t.motion] == ["visible_at_start", "stops", "moves", "exits"]
    assert _close([e.ms for e in t.motion], [0, 4000, 9000, 13000])
    assert t.motion[-1].where == "the right"


def test_short_tracks_are_dropped() -> None:
    raw = {1: [_pt(ms, (10, 10, 50, 50)) for ms in (1000, 1200, 1400)]}
    assert finalize_tracks(raw, META, VideoSettings(), 200) == []


def test_close_up_flags_unconfirmed_vehicles() -> None:
    sure = video.FrameCaption(
        caption="A silver sedan.",
        vehicles=[video.VisibleVehicle(type="sedan", color="silver")],
        people=[],
        confidence=0.9,
    )
    assert video._close_up(sure) == "A silver sedan (VLM, confidence 0.90)."
    dumpster = sure.model_copy(update={"caption": "A gray dumpster.", "vehicles": []})
    assert video._close_up(dumpster).endswith("possible false detection.")


def test_regions_edges_and_time_formatting() -> None:
    assert region_name((450, 250, 510, 290), 960, 540) == "the frame center"
    assert region_name((0, 400, 50, 530), 960, 540) == "the bottom left"
    assert edge_side((900, 10, 960, 100), 960, 540, 0.06) == "the top right"
    assert edge_side((300, 200, 400, 300), 960, 540, 0.06) is None
    utc = datetime(2026, 3, 15, 5, 41, 3, tzinfo=UTC)  # 22:41:03 PDT
    assert format_time(utc, 2500, TZ) == "22:41:05"
    assert format_time(None, 125_000, TZ) == "2:05 into the clip"


# --- whole pipeline through the job framework (DB, fake models) -------------------------------


def fake_detector(frame: Any) -> list[Detection]:
    """Finds the SYNTHETIC clip's bright rectangle (no model)."""
    mask = frame.min(axis=2) > 220
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return []
    box = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
    return [Detection(bbox=box, conf=0.9, label="car", color="white")]


def fake_tracker(frames: Sequence[FrameDetections], fps: float) -> dict[int, list[TrackPoint]]:
    return {
        1: [
            TrackPoint(
                frame_idx=f.frame_idx,
                ms=f.ms,
                bbox=d.bbox,
                conf=d.conf,
                label=d.label,
                color=d.color,
            )
            for f in frames
            for d in f.detections
        ]
    }


class FakeVlm:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, task: str, system: str, user: str, schema: type[Any]) -> str:
        assert task == "caption" and schema is video.FrameCaption
        self.calls.append(user)
        if "crop around one tracked object" in user:
            body = {
                "caption": "A white compact sedan.",
                "vehicles": [{"type": "sedan", "color": "white"}],
                "people": [],
                "confidence": 0.8,
            }
        else:
            body = {
                "caption": "An empty lot with one white car.",
                "vehicles": [{"type": "car", "color": "white"}],
                "people": [{"description": "no one visible", "clothing_colors": []}],
                "confidence": 0.7,
            }
        return json.dumps(body)


def _drain(conn: Conn, pipe: Pipeline, ev: SeededEvidence, settings: WorkerSettings) -> list[str]:
    out = []
    while (job := jobs.claim_next(conn, set(pipe.stages), 3, 1800)) is not None:
        status = run_job(conn, job, {"video": pipe}, ev.store, settings, jobs.PostgresQueue())
        out.append(f"{job.stage}:{status}")
    return out


def _test_pipeline() -> Pipeline:
    from caselens_agent.router import ModelRouter, RouterSettings

    def fake_embed(texts: Sequence[str]) -> list[list[float]]:
        return [[0.1] * EMBEDDING_DIM for _ in texts]

    stages = dict(video.PIPELINE.stages)
    stages["embed"] = make_embed_stage(fake_embed)
    # A fake provider without a responder counts as "not configured": the LLM pass is skipped.
    stages["extract"] = make_extract_stage(ModelRouter(RouterSettings(llm_provider="fake")))
    return Pipeline(first=video.PIPELINE.first, stages=stages, next=video.PIPELINE.next)


@pytest.fixture
def fake_models(monkeypatch: pytest.MonkeyPatch) -> FakeVlm:
    from caselens_agent.router import ModelRouter, RouterSettings

    vlm = FakeVlm()
    monkeypatch.setattr(video, "detector_factory", lambda ctx, cfg: fake_detector)
    monkeypatch.setattr(video, "tracker_factory", lambda ctx, cfg: fake_tracker)
    monkeypatch.setattr(
        video,
        "vlm_router_factory",
        lambda: ModelRouter(RouterSettings(llm_provider="fake"), fake=vlm),
    )
    monkeypatch.setenv("CASELENS_VIDEO_MIN_CAPTION_TRACK_MS", "1000")
    return vlm


@pytest.mark.db
def test_pipeline_writes_track_and_scene_chunks(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    worker_settings: WorkerSettings,
    tmp_path: Path,
    fake_models: FakeVlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip = tmp_path / "SYNTHETIC_clip.mp4"
    _synthetic_clip(clip)
    ev = seed_evidence(clip, "video", recorded_at="2026-03-14T22:41:03-07:00")
    jobs.PostgresQueue().enqueue(owner_conn, ev.case_id, ev.evidence_id, "scene_detect")
    as_role(owner_conn, "caselens_worker")
    pipe = _test_pipeline()
    monkeypatch.setenv("CASELENS_VIDEO_SCENE_MIN_LEN_FRAMES", "10")
    statuses = _drain(owner_conn, pipe, ev, worker_settings)
    assert statuses == [
        f"{s}:succeeded"
        for s in ["scene_detect", "keyframes", "detect", "track", "caption", "embed", "extract"]
    ]
    # Two scene captions + one close-up of the (only, white) vehicle track.
    assert len(fake_models.calls) == 3
    rows = owner_conn.execute(
        "SELECT chunk_index, text, locator, t_start, t_end, modality, embedding IS NOT NULL "
        "FROM public.chunks WHERE evidence_id = %s ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    assert [r[5] for r in rows] == ["video"] * 3 and all(r[6] for r in rows)
    text, loc = rows[0][1], VideoLocator.model_validate(rows[0][2])
    assert text.startswith("Track 1 (car, white) on 2026-03-14: visible near the left side")
    assert text.endswith("Close-up: A white compact sedan (VLM, confidence 0.80).")
    assert (loc.track_id, loc.start_ms) == ("t1", 0)
    assert loc.frame_idx is not None and loc.frame_idx % 3 == 0 and loc.bbox is not None
    assert rows[0][3] == START and rows[0][4] == START + timedelta(milliseconds=loc.end_ms or 0)
    assert rows[1][1].startswith("Scene 1 (22:41:03 to 22:41:04), keyframe ")
    assert "Vehicles: white car." in rows[1][1] and "People: no one visible." in rows[1][1]
    assert rows[2][2]["start_ms"] == 1467 and rows[2][2].get("track_id") is None

    key = f"cases/{ev.case_id}/evidence/{ev.evidence_id}"
    assert ev.store.get_bytes(f"{key}/keyframes/keyframes/22.jpg")[:2] == b"\xff\xd8"
    tracks = json.loads(ev.store.get_bytes(f"{key}/track/tracks.json"))
    assert tracks["raw_tracks"] == 1 and tracks["tracks"][0]["color"] == "white"

    # Re-running caption replaces chunks instead of duplicating them.
    assert jobs.requeue(owner_conn, ev.evidence_id, "caption")
    _drain(owner_conn, pipe, ev, worker_settings)
    count = owner_conn.execute(
        "SELECT count(*) FROM public.chunks WHERE evidence_id = %s", (ev.evidence_id,)
    ).fetchone()
    assert count == (3,)


@pytest.mark.db
def test_caption_skipped_without_model_still_writes_tracks(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    worker_settings: WorkerSettings,
    tmp_path: Path,
    fake_models: FakeVlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video, "vlm_router_factory", lambda: None)
    clip = tmp_path / "SYNTHETIC_clip.mp4"
    _synthetic_clip(clip)
    ev = seed_evidence(clip, "video", recorded_at=None)
    jobs.PostgresQueue().enqueue(owner_conn, ev.case_id, ev.evidence_id, "scene_detect")
    as_role(owner_conn, "caselens_worker")
    assert all(
        s.endswith(":succeeded") for s in _drain(owner_conn, _test_pipeline(), ev, worker_settings)
    )
    rows = owner_conn.execute(
        "SELECT text, t_start FROM public.chunks WHERE evidence_id = %s", (ev.evidence_id,)
    ).fetchall()
    assert len(rows) == 1 and rows[0][1] is None
    assert "into the clip" in rows[0][0] and "Close-up" not in rows[0][0]
    as_role(owner_conn, None)
    (payload,) = owner_conn.execute(
        "SELECT payload FROM public.audit_log WHERE case_id = %s "
        "AND action = 'ingest.stage_succeeded' AND payload->>'stage' = 'caption'",
        (ev.case_id,),
    ).fetchone() or (None,)
    assert payload["caption"].startswith("skipped:") and payload["vlm_calls"] == 0


# --- real models (CASELENS_HEAVY=1) -----------------------------------------------------------


@pytest.mark.skipif(not HEAVY, reason="set CASELENS_HEAVY=1 to run real YOLO + ByteTrack")
@pytest.mark.db
def test_real_video_end_to_end(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    worker_settings: WorkerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = REPO / "data_gen" / "output" / "riverside_lot"
    clip = case_dir / "video" / "vid_02_market_lot_shooting.mp4"
    if not clip.exists():
        pytest.skip("no generated demo video (run data_gen)")
    monkeypatch.setattr(video, "vlm_router_factory", lambda: None)
    settings = worker_settings.model_copy(update={"model_cache_dir": REPO / ".cache" / "models"})
    ev = seed_evidence(clip, "video", recorded_at="2026-03-14T22:41:03-07:00")
    jobs.PostgresQueue().enqueue(owner_conn, ev.case_id, ev.evidence_id, "scene_detect")
    as_role(owner_conn, "caselens_worker")
    statuses = _drain(owner_conn, _test_pipeline(), ev, settings)
    assert all(s.endswith(":succeeded") for s in statuses), statuses
    rows = owner_conn.execute(
        "SELECT text, locator FROM public.chunks WHERE evidence_id = %s ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    facts = json.loads((case_dir / "answer_key.json").read_text(encoding="utf-8"))["facts"]
    mine = [f["locator"] for f in facts if f["evidence"] == f"video/{clip.name}"]
    assert mine
    for loc in mine:
        hit = [
            r
            for r in rows
            if r[1]["start_ms"] < loc["end_ms"] and (r[1]["end_ms"] or 0) > loc["start_ms"]
        ]
        assert hit, loc
    assert any("(car, gray/silver)" in r[0] and "enters frame" in r[0] for r in rows)
