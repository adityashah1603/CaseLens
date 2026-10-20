"""Video pipeline data models and pure post-processing (no heavy imports).

* keyframe selection (1 fps + every scene start)
* dominant-color naming of vehicle crops (HSV)
* track post-processing: majority label/color, fragment merging, minimum length, coarse motion
  (enters / exits a frame edge, stops, parks, stationary), and the per-track summary text with
  wall-clock times in the case timezone.

Boxes are `(x1, y1, x2, y2)` in pixels of the ORIGINAL frame.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Box = tuple[float, float, float, float]

# COCO ids the detector keeps.
COCO_LABELS: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}
VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle", "bicycle"})
# Classes the detector often confuses for one physical vehicle; fragments may merge across them.
_CAR_LIKE = frozenset({"car", "truck", "bus"})

COLOR_DISPLAY = {"gray": "gray/silver"}


class VideoSettings(BaseSettings):
    """Video-pipeline knobs from CASELENS_VIDEO_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_VIDEO_", extra="ignore")

    scene_threshold: float = 27.0  # PySceneDetect ContentDetector default
    scene_min_len_frames: int = 15
    keyframe_interval_s: float = 1.0
    jpeg_quality: int = 85
    # Detection: YOLO weights (downloaded into model_cache_dir/ultralytics), sampled frame rate.
    detector_weights: str = "yolo11n.pt"
    sample_fps: float = 5.0
    imgsz: int = 640
    det_conf: float = 0.15  # ByteTrack uses low-score boxes in its second association pass
    # Horizontal bands (fraction of frame height) at the top and bottom where burned-in overlay
    # text lives; small boxes centered there are dropped.
    overlay_band: float = 0.06
    track_buffer_s: float = 3.0
    min_track_ms: int = 1000
    min_track_points: int = 3
    merge_gap_ms: int = 3000
    # Max center distance for a fragment merge, in multiples of the larger box side.
    merge_distance: float = 1.0
    edge_margin: float = 0.06
    stationary_ms: int = 3000
    parked_ms: int = 8000
    max_scene_captions: int = 6
    max_track_captions: int = 3
    min_caption_track_ms: int = 3000


# ------------------------------------------------------------------------------------------
# Artifacts
# ------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class VideoMeta(_Model):
    fps: float = Field(gt=0)
    frame_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def ms(self, frame_idx: int) -> int:
        return round(frame_idx * 1000 / self.fps)


class Scene(_Model):
    index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)  # exclusive
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class SceneList(_Model):
    meta: VideoMeta
    scenes: list[Scene]


class Keyframe(_Model):
    frame_idx: int = Field(ge=0)
    ms: int = Field(ge=0)
    scene_index: int = Field(ge=0)
    scene_start: bool
    name: str


class Detection(_Model):
    bbox: Box
    conf: float = Field(ge=0, le=1)
    label: str
    color: str | None = None


class FrameDetections(_Model):
    frame_idx: int = Field(ge=0)
    ms: int = Field(ge=0)
    detections: list[Detection]


class DetectionsFile(_Model):
    model: str
    sample_fps: float
    frame_step: int
    frames: list[FrameDetections]


class TrackPoint(_Model):
    frame_idx: int = Field(ge=0)
    ms: int = Field(ge=0)
    bbox: Box
    conf: float = Field(ge=0, le=1)
    label: str
    color: str | None = None


MotionKind = Literal[
    "visible_at_start",
    "enters",
    "appears",
    "appears_beside",
    "stationary",
    "stops",
    "parks",
    "moves",
    "visible_at_end",
    "exits",
    "last_seen",
]


class MotionEvent(_Model):
    kind: MotionKind
    ms: int = Field(ge=0)
    where: str  # edge side ("the left") or frame region ("the frame center")
    other_track: str | None = None


class Track(_Model):
    track_id: str
    label: str
    color: str | None
    points: list[TrackPoint]
    merged_from: list[int] = Field(default_factory=list)
    motion: list[MotionEvent] = Field(default_factory=list)

    @property
    def start_ms(self) -> int:
        return self.points[0].ms

    @property
    def end_ms(self) -> int:
        return self.points[-1].ms

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def is_vehicle(self) -> bool:
        return self.label in VEHICLE_LABELS


class TracksFile(_Model):
    raw_tracks: int
    tracks: list[Track]


# ------------------------------------------------------------------------------------------
# Keyframes
# ------------------------------------------------------------------------------------------


def keyframe_name(frame_idx: int) -> str:
    return f"keyframes/{frame_idx}.jpg"


def select_keyframes(meta: VideoMeta, scenes: Sequence[Scene], interval_s: float) -> list[Keyframe]:
    """One frame per `interval_s` seconds plus the first frame of every scene, sorted."""
    step = max(1, round(meta.fps * interval_s))
    regular = set(range(0, meta.frame_count, step))
    starts = {s.start_frame for s in scenes if s.start_frame < meta.frame_count}
    out: list[Keyframe] = []
    for idx in sorted(regular | starts):
        scene = scene_at(scenes, idx)
        out.append(
            Keyframe(
                frame_idx=idx,
                ms=meta.ms(idx),
                scene_index=scene.index if scene else 0,
                scene_start=idx in starts,
                name=keyframe_name(idx),
            )
        )
    return out


def scene_at(scenes: Sequence[Scene], frame_idx: int) -> Scene | None:
    for s in scenes:
        if s.start_frame <= frame_idx < s.end_frame:
            return s
    return scenes[-1] if scenes and frame_idx >= scenes[-1].end_frame else None


def scenes_from_cuts(meta: VideoMeta, cuts: Sequence[tuple[int, int]]) -> list[Scene]:
    """Scenes from detector (start, end) frame pairs; no cut means one scene for the clip."""
    pairs = [(s, e) for s, e in cuts if e > s] or [(0, meta.frame_count)]
    return [
        Scene(index=i, start_frame=s, end_frame=e, start_ms=meta.ms(s), end_ms=meta.ms(e))
        for i, (s, e) in enumerate(pairs)
    ]


# ------------------------------------------------------------------------------------------
# Color naming
# ------------------------------------------------------------------------------------------

# OpenCV hue is 0..179. Upper bounds of each hue band.
_HUES: tuple[tuple[int, str], ...] = (
    (8, "red"),
    (20, "orange"),
    (33, "yellow"),
    (85, "green"),
    (130, "blue"),
    (160, "purple"),
    (180, "red"),
)


def color_name(hsv: Any) -> str | None:
    """Dominant color name of a vehicle crop given as an OpenCV HSV uint8 array (H, W, 3).

    The central part of the box is used (less background). Saturated pixels vote for a hue;
    if few pixels are saturated the car is achromatic and its brightness names it. Dark
    pixels (glass, shadow) only win if they dominate."""
    import numpy as np

    arr = np.asarray(hsv)
    if arr.ndim != 3 or arr.shape[0] < 4 or arr.shape[1] < 4:
        return None
    h, w = arr.shape[:2]
    core = arr[h // 5 : h - h // 5, w // 6 : w - w // 6].reshape(-1, 3).astype(np.int32)
    hue, sat, val = core[:, 0], core[:, 1], core[:, 2]
    chromatic = (sat >= 90) & (val >= 60)
    if chromatic.mean() >= 0.25:
        names = [_hue_name(int(x)) for x in hue[chromatic]]
        return Counter(names).most_common(1)[0][0]
    dark = val < 70
    if dark.mean() >= 0.6:
        return "black"
    v = float(np.median(val[~dark]))
    if v >= 200:
        return "white"
    if v >= 90:
        return "gray"
    return "black"


def _hue_name(hue: int) -> str:
    for bound, name in _HUES:
        if hue < bound:
            return name
    return "red"


# ------------------------------------------------------------------------------------------
# Track post-processing
# ------------------------------------------------------------------------------------------


def _center(b: Box) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _side(b: Box) -> float:
    return max(b[2] - b[0], b[3] - b[1])


def _dist(a: Box, b: Box) -> float:
    (ax, ay), (bx, by) = _center(a), _center(b)
    return math.hypot(ax - bx, ay - by)


def _compatible(a: str, b: str) -> bool:
    return a == b or (a in _CAR_LIKE and b in _CAR_LIKE)


def majority_label(points: Sequence[TrackPoint]) -> str:
    return Counter(p.label for p in points).most_common(1)[0][0]


def majority_color(points: Sequence[TrackPoint]) -> str | None:
    """Area-weighted vote over the points' crop colors."""
    votes: dict[str, float] = {}
    for p in points:
        if p.color:
            area = (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1])
            votes[p.color] = votes.get(p.color, 0.0) + area
    return max(votes, key=lambda c: votes[c]) if votes else None


def build_tracks(raw: dict[int, list[TrackPoint]]) -> list[tuple[list[int], list[TrackPoint]]]:
    """Raw tracker output -> (source ids, chronologically ordered points)."""
    out = []
    for tid, pts in sorted(raw.items()):
        ordered = sorted(pts, key=lambda p: p.frame_idx)
        if ordered:
            out.append(([tid], ordered))
    return out


def merge_fragments(
    tracks: list[tuple[list[int], list[TrackPoint]]], max_gap_ms: int, max_distance: float
) -> list[tuple[list[int], list[TrackPoint]]]:
    """Join a track to the closest earlier one that ended shortly before it started nearby.

    Same class (car/truck/bus count as one), no time overlap, gap <= max_gap_ms, and the
    jump between the old end box and the new start box at most `max_distance` box sides."""
    pending = sorted(tracks, key=lambda t: t[1][0].ms)
    done: list[tuple[list[int], list[TrackPoint]]] = []
    for ids, pts in pending:
        label = majority_label(pts)
        best: int | None = None
        best_cost = math.inf
        for i, (_, prev) in enumerate(done):
            gap = pts[0].ms - prev[-1].ms
            if gap <= 0 or gap > max_gap_ms or not _compatible(majority_label(prev), label):
                continue
            limit = max_distance * max(_side(prev[-1].bbox), _side(pts[0].bbox))
            d = _dist(prev[-1].bbox, pts[0].bbox)
            if d <= limit and d < best_cost:
                best, best_cost = i, d
        if best is None:
            done.append((list(ids), list(pts)))
        else:
            prev_ids, prev_pts = done[best]
            done[best] = (prev_ids + ids, prev_pts + pts)
    return done


def region_name(b: Box, width: int, height: int) -> str:
    cx, cy = _center(b)
    col = min(2, int(3 * cx / width))
    row = min(2, int(3 * cy / height))
    return (
        ("the top left", "the top", "the top right"),
        ("the left side", "the frame center", "the right side"),
        ("the bottom left", "the bottom", "the bottom right"),
    )[row][col]


def edge_side(b: Box, width: int, height: int, margin: float) -> str | None:
    """Which frame edge(s) the box touches, e.g. "the left" or "the top right"; None if none."""
    mx, my = margin * width, margin * height
    vert = "top" if b[1] <= my else "bottom" if b[3] >= height - my else ""
    horiz = "left" if b[0] <= mx else "right" if b[2] >= width - mx else ""
    words = " ".join(w for w in (vert, horiz) if w)
    return f"the {words}" if words else None


def _states(points: Sequence[TrackPoint], width: int, height: int) -> list[bool]:
    """Per point: is the object moving within a centered ~1 s window? Debounced to >= 1 s runs."""
    diag = math.hypot(width, height)
    moving: list[bool] = []
    n = len(points)
    for i, p in enumerate(points):
        a, b = i, i
        while a > 0 and p.ms - points[a - 1].ms <= 500:
            a -= 1
        while b + 1 < n and points[b + 1].ms - p.ms <= 500:
            b += 1
        if a == b:
            moving.append(moving[-1] if moving else False)
            continue
        span = max(1, points[b].ms - points[a].ms) / 1000
        limit = max(0.12 * _side(p.bbox), 0.008 * diag) * span
        moving.append(_dist(points[a].bbox, points[b].bbox) > limit)
    # Debounce: a run shorter than a second between two others takes their state.
    while True:
        runs = _runs(points, moving)
        short = [(s, e, state) for s, e, state in runs[1:-1] if points[e].ms - points[s].ms < 1000]
        if not short:
            return moving
        s, e, state = short[0]
        for x in range(s, e + 1):
            moving[x] = not state


def _runs(points: Sequence[TrackPoint], states: Sequence[bool]) -> list[tuple[int, int, bool]]:
    runs: list[tuple[int, int, bool]] = []
    start = 0
    for i in range(1, len(states) + 1):
        if i == len(states) or states[i] != states[start]:
            runs.append((start, i - 1, states[start]))
            start = i
    return runs


def describe_motion(
    points: Sequence[TrackPoint],
    label: str,
    meta: VideoMeta,
    cfg: VideoSettings,
    sample_step_ms: int,
) -> list[MotionEvent]:
    """Coarse motion events for one track, in time order."""
    w, h = meta.width, meta.height
    first, last = points[0], points[-1]
    events: list[MotionEvent] = []
    slack = max(1000, 2 * sample_step_ms)
    if first.ms <= slack:
        events.append(
            MotionEvent(kind="visible_at_start", ms=first.ms, where=region_name(first.bbox, w, h))
        )
    elif (side := edge_side(first.bbox, w, h, cfg.edge_margin)) is not None:
        events.append(MotionEvent(kind="enters", ms=first.ms, where=side))
    else:
        events.append(MotionEvent(kind="appears", ms=first.ms, where=region_name(first.bbox, w, h)))

    moving = _states(points, w, h)
    runs = _runs(points, moving)
    if len(runs) == 1 and not runs[0][2]:
        if events[0].kind == "enters":
            # Never moved: first detected late (occlusion, detector warm-up), not an entrance.
            where = region_name(first.bbox, w, h)
            events[0] = MotionEvent(kind="appears", ms=first.ms, where=where)
        events.append(
            MotionEvent(kind="stationary", ms=first.ms, where=region_name(first.bbox, w, h))
        )
    else:
        for k in range(1, len(runs)):
            s, e, state = runs[k]
            ps, pe, prev_state = runs[k - 1]
            where = region_name(points[s].bbox, w, h)
            if not state and points[e].ms - points[s].ms >= cfg.stationary_ms:
                dur = points[e].ms - points[s].ms
                parks = label in VEHICLE_LABELS and k == len(runs) - 1 and dur >= cfg.parked_ms
                events.append(
                    MotionEvent(kind="parks" if parks else "stops", ms=points[s].ms, where=where)
                )
            elif (
                state
                and not prev_state
                and (k == 1 or points[pe].ms - points[ps].ms >= cfg.stationary_ms)
            ):
                events.append(MotionEvent(kind="moves", ms=points[s].ms, where=where))

    if last.ms >= meta.duration_ms - slack:
        events.append(
            MotionEvent(
                kind="visible_at_end", ms=meta.duration_ms, where=region_name(last.bbox, w, h)
            )
        )
    elif (side := edge_side(last.bbox, w, h, cfg.edge_margin)) is not None:
        events.append(MotionEvent(kind="exits", ms=last.ms, where=side))
    else:
        events.append(MotionEvent(kind="last_seen", ms=last.ms, where=region_name(last.bbox, w, h)))
    return events


def _expand(b: Box, f: float) -> Box:
    dx, dy = (b[2] - b[0]) * f, (b[3] - b[1]) * f
    return (b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy)


def _overlap_share(a: Box, b: Box) -> float:
    """Fraction of box a's area inside box b."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return max(0.0, w) * max(0.0, h) / area if area > 0 else 0.0


def link_people_to_vehicles(tracks: list[Track], window_ms: int) -> list[Track]:
    """A person who first shows up mid-clip (appears or enters) largely inside an earlier
    vehicle's box gets an "appears beside Track N" event: the coarse cue for someone getting
    out of a car."""
    vehicles = [t for t in tracks if t.is_vehicle]
    out: list[Track] = []
    for t in tracks:
        if t.label != "person" or not t.motion or t.motion[0].kind not in ("appears", "enters"):
            out.append(t)
            continue
        p0 = t.points[0]
        match: Track | None = None
        best = 0.0
        for v in vehicles:
            for q in v.points:
                if abs(q.ms - p0.ms) <= window_ms and v.start_ms < p0.ms:
                    share = _overlap_share(p0.bbox, _expand(q.bbox, 0.1))
                    if share > best:
                        match, best = v, share
        if match is None or best < 0.2:
            out.append(t)
            continue
        first = t.motion[0].model_copy(
            update={"kind": "appears_beside", "other_track": match.track_id}
        )
        out.append(t.model_copy(update={"motion": [first, *t.motion[1:]]}))
    return out


def finalize_tracks(
    raw: dict[int, list[TrackPoint]],
    meta: VideoMeta,
    cfg: VideoSettings,
    sample_step_ms: int,
) -> list[Track]:
    """Merge fragments, drop short tracks, number t1.. by first appearance, add motion."""
    merged = merge_fragments(build_tracks(raw), cfg.merge_gap_ms, cfg.merge_distance)
    kept = [
        (ids, pts)
        for ids, pts in merged
        if pts[-1].ms - pts[0].ms >= cfg.min_track_ms and len(pts) >= cfg.min_track_points
    ]
    kept.sort(key=lambda t: (t[1][0].ms, t[1][0].bbox[0]))
    tracks: list[Track] = []
    for n, (ids, pts) in enumerate(kept, start=1):
        label = majority_label(pts)
        tracks.append(
            Track(
                track_id=f"t{n}",
                label=label,
                color=majority_color(pts) if label in VEHICLE_LABELS else None,
                points=pts,
                merged_from=ids,
                motion=describe_motion(pts, label, meta, cfg, sample_step_ms),
            )
        )
    return link_people_to_vehicles(tracks, window_ms=max(1000, 2 * sample_step_ms))


def representative_point(track: Track, meta: VideoMeta, margin: float) -> TrackPoint:
    """The largest box that is fully inside the frame (else the largest box)."""
    inside = [p for p in track.points if edge_side(p.bbox, meta.width, meta.height, margin) is None]
    pool = inside or track.points

    def area(p: TrackPoint) -> float:
        return (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1])

    return max(pool, key=area)


# ------------------------------------------------------------------------------------------
# Summary text
# ------------------------------------------------------------------------------------------


def wall_clock(recorded_at: datetime | None, ms: int) -> datetime | None:
    return recorded_at + timedelta(milliseconds=ms) if recorded_at is not None else None


def format_time(recorded_at: datetime | None, ms: int, tz: ZoneInfo) -> str:
    """'22:41:05' in the case timezone, or a clip offset ('0:05 into the clip') without one."""
    t = wall_clock(recorded_at, ms)
    if t is not None:
        return t.astimezone(tz).strftime("%H:%M:%S")
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d} into the clip"


def track_header(track: Track) -> str:
    n = track.track_id.removeprefix("t")
    if track.color:
        return f"Track {n} ({track.label}, {COLOR_DISPLAY.get(track.color, track.color)})"
    return f"Track {n} ({track.label})"


def _phrase(e: MotionEvent, when: str, others: dict[str, Track]) -> str:
    match e.kind:
        case "visible_at_start":
            return f"visible near {e.where} when the clip starts at {when}"
        case "enters":
            return f"enters frame from {e.where} at {when}"
        case "appears":
            return f"first seen near {e.where} at {when}"
        case "appears_beside":
            other = others.get(e.other_track or "")
            who = track_header(other) if other else "a vehicle"
            return f"appears beside {who} near {e.where} at {when}"
        case "stationary":
            return f"stationary near {e.where}"
        case "stops":
            return f"stops near {e.where} at {when}"
        case "parks":
            return f"parks near {e.where} at {when}"
        case "moves":
            return f"starts moving at {when}"
        case "visible_at_end":
            return f"still visible near {e.where} when the clip ends at {when}"
        case "exits":
            return f"exits frame to {e.where} at {when}"
        case "last_seen":
            return f"last seen near {e.where} at {when}"
    raise ValueError(e.kind)  # pragma: no cover


def track_summary(
    track: Track,
    recorded_at: datetime | None,
    tz: ZoneInfo,
    others: Sequence[Track] = (),
    close_up: str | None = None,
) -> str:
    """'Track 3 (car, gray/silver) on 2026-03-14: enters frame from the top right at 22:41:05,
    parks near the left side at 22:41:14, still visible ... when the clip ends at 22:41:52.'"""
    by_id = {t.track_id: t for t in others}
    head = track_header(track)
    start = wall_clock(recorded_at, track.start_ms)
    if start is not None:
        head += f" on {start.astimezone(tz).date().isoformat()}"
    parts = [_phrase(e, format_time(recorded_at, e.ms, tz), by_id) for e in track.motion]
    text = f"{head}: {', '.join(parts)}."
    if close_up:
        text += f" Close-up: {close_up}"
    return text
