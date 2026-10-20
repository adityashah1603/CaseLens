"""Video decoding, scene detection, YOLO detection and ByteTrack (heavy libraries, lazy imports).

* `probe` / `read_frames` / `encode_jpeg`: OpenCV.
* `detect_cuts`: PySceneDetect ContentDetector.
* `yolo_detector`: Ultralytics YOLO (weights in `<model_cache_dir>/ultralytics`, CPU, capped
  threads, auto-install of missing packages disabled).
* `bytetrack`: Ultralytics' BYTETracker with its stock `bytetrack.yaml` settings, run over the
  stored detections. Ultralytics needs the `lap` package for assignment; when it is not
  installed, an exact stand-in built on SciPy's Hungarian solver is registered instead of
  letting Ultralytics pip-install it at runtime.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from caselens_workers.video_tracks import (
    COCO_LABELS,
    VEHICLE_LABELS,
    Box,
    Detection,
    FrameDetections,
    TrackPoint,
    VideoMeta,
    color_name,
)

# (BGR frame) -> detections in original-frame pixels. Swappable in tests.
FrameDetector = Callable[[Any], list[Detection]]
# (sampled frames' detections, sampled fps) -> raw track id -> points. Swappable in tests.
Tracker = Callable[[Sequence[FrameDetections], float], dict[int, list[TrackPoint]]]


def _quiet_ultralytics(threads: int) -> None:
    os.environ["YOLO_AUTOINSTALL"] = "False"
    os.environ.setdefault("YOLO_VERBOSE", "False")
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    import torch

    torch.set_num_threads(threads)


def probe(path: Path) -> VideoMeta:
    import cv2

    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"cannot open video {path.name}")
        fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    return VideoMeta(
        fps=fps,
        frame_count=count,
        duration_ms=round(count * 1000 / fps),
        width=width,
        height=height,
    )


def detect_cuts(path: Path, threshold: float, min_scene_len: int) -> list[tuple[int, int]]:
    """(start_frame, end_frame exclusive) per detected scene; empty when there is no cut."""
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector(threshold=threshold, min_scene_len=min_scene_len))
    return [(int(a.frame_num), int(b.frame_num)) for a, b in scenes]


def read_frames(path: Path, wanted: Callable[[int], bool]) -> Iterator[tuple[int, Any]]:
    """Decode sequentially and yield (frame_idx, BGR frame) for the wanted indices."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    try:
        idx = 0
        while cap.grab():
            if wanted(idx):
                ok, frame = cap.retrieve()
                if ok:
                    yield idx, frame
            idx += 1
    finally:
        cap.release()


def encode_jpeg(frame: Any, quality: int) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return bytes(buf.tobytes())


def crop(frame: Any, bbox: Box, pad: float = 0.0) -> Any:
    h, w = frame.shape[:2]
    dx, dy = (bbox[2] - bbox[0]) * pad, (bbox[3] - bbox[1]) * pad
    x1, y1 = max(0, int(bbox[0] - dx)), max(0, int(bbox[1] - dy))
    x2, y2 = min(w, int(bbox[2] + dx + 1)), min(h, int(bbox[3] + dy + 1))
    return frame[y1:y2, x1:x2]


def crop_color(frame: Any, bbox: Box) -> str | None:
    import cv2

    region = crop(frame, bbox)
    if region.size == 0:
        return None
    return color_name(cv2.cvtColor(region, cv2.COLOR_BGR2HSV))


def in_overlay_band(bbox: Box, height: int, band: float) -> bool:
    """A small box centered in the top/bottom band where burned-in overlay text sits."""
    limit = band * height
    cy = (bbox[1] + bbox[3]) / 2
    small = bbox[3] - bbox[1] <= 1.5 * limit
    return small and (cy <= limit or cy >= height - limit)


def weights_path(cache_dir: Path, weights: str) -> Path:
    return cache_dir / "ultralytics" / weights


@lru_cache(maxsize=1)
def _yolo(path: str, threads: int) -> Any:
    _quiet_ultralytics(threads)
    ultralytics: Any = importlib.import_module("ultralytics")  # heavy; partially typed

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return ultralytics.YOLO(path)


def release_models() -> None:
    """Drop the cached YOLO model (call after each clip so memory does not pile up)."""
    import gc

    _yolo.cache_clear()
    gc.collect()


def downscale(frame: Any, max_side: int) -> tuple[Any, float]:
    """Shrink a frame so its longer side is at most `max_side`; returns (frame, scale)."""
    import cv2

    h, w = frame.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale >= 1.0:
        return frame, 1.0
    small = cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    return small, scale


def yolo_detector(
    cache_dir: Path, weights: str, threads: int, imgsz: int, conf: float, overlay_band: float
) -> FrameDetector:
    model = _yolo(str(weights_path(cache_dir, weights)), threads)
    classes = sorted(COCO_LABELS)

    def detect(frame: Any) -> list[Detection]:
        # Detect on a <= imgsz copy (memory + speed); boxes are mapped back to original pixels.
        small, scale = downscale(frame, imgsz)
        result = model.predict(
            small,
            imgsz=imgsz,
            conf=conf,
            classes=classes,
            agnostic_nms=True,  # one box per object, not a car box and a truck box
            device="cpu",
            verbose=False,
        )[0]
        boxes = result.boxes.cpu().numpy()
        del small, result
        height = int(frame.shape[0])
        out: list[Detection] = []
        for xyxy, score, cls in zip(boxes.xyxy, boxes.conf, boxes.cls, strict=True):
            bbox: Box = (
                round(float(xyxy[0]) / scale, 1),
                round(float(xyxy[1]) / scale, 1),
                round(float(xyxy[2]) / scale, 1),
                round(float(xyxy[3]) / scale, 1),
            )
            if in_overlay_band(bbox, height, overlay_band):
                continue
            label = COCO_LABELS[int(cls)]
            color = crop_color(frame, bbox) if label in VEHICLE_LABELS else None
            out.append(Detection(bbox=bbox, conf=round(float(score), 4), label=label, color=color))
        return out

    return detect


# ------------------------------------------------------------------------------------------
# ByteTrack
# ------------------------------------------------------------------------------------------


def _lapjv(
    cost: Any, extend_cost: bool = True, cost_limit: float = float("inf")
) -> tuple[float, Any, Any]:
    """`lap.lapjv` semantics: rows/columns whose best match costs more than `cost_limit` stay
    unassigned (-1). Solved exactly on the standard augmented matrix with SciPy."""
    import numpy as np

    linear_sum_assignment = importlib.import_module("scipy.optimize").linear_sum_assignment
    c = np.asarray(cost, dtype=np.float64)
    n, m = c.shape
    x = np.full(n, -1, dtype=np.int64)
    y = np.full(m, -1, dtype=np.int64)
    if n == 0 or m == 0:
        return 0.0, x, y
    if np.isfinite(cost_limit):
        big = 1e12
        size = n + m
        aug = np.full((size, size), big)
        aug[:n, :m] = np.where(c <= cost_limit, c, big)
        aug[:n, m:] = np.where(np.eye(n), cost_limit / 2, big)
        aug[n:, :m] = np.where(np.eye(m), cost_limit / 2, big)
        aug[n:, m:] = 0.0
        rows, cols = linear_sum_assignment(aug)
        pairs = [(r, k) for r, k in zip(rows, cols, strict=True) if r < n and k < m]
    else:
        rows, cols = linear_sum_assignment(c)
        pairs = list(zip(rows, cols, strict=True))
    total = 0.0
    for r, k in pairs:
        if c[r, k] <= cost_limit:
            x[r], y[k] = k, r
            total += float(c[r, k])
    return total, x, y


def _ensure_lap() -> None:
    try:
        importlib.import_module("lap")
    except ImportError:
        shim = types.ModuleType("lap")
        shim.__version__ = "0.0-caselens-scipy"  # type: ignore[attr-defined]
        shim.lapjv = _lapjv  # type: ignore[attr-defined]
        sys.modules["lap"] = shim


class _Boxes:
    """The slice of Ultralytics' `Boxes` interface that BYTETracker reads."""

    def __init__(self, xyxy: Any, conf: Any, cls: Any) -> None:
        import numpy as np

        self.xyxy = np.asarray(xyxy, dtype=np.float32).reshape(-1, 4)
        self.conf = np.asarray(conf, dtype=np.float32).reshape(-1)
        self.cls = np.asarray(cls, dtype=np.float32).reshape(-1)
        wh = self.xyxy[:, 2:4] - self.xyxy[:, 0:2]
        self.xywh = np.concatenate([self.xyxy[:, 0:2] + wh / 2, wh], axis=1)

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, mask: Any) -> _Boxes:
        return _Boxes(self.xyxy[mask], self.conf[mask], self.cls[mask])


def _bytetrack_args(track_buffer: int) -> Any:
    utils: Any = importlib.import_module("ultralytics.utils")
    cfg_path = Path(utils.ROOT) / "cfg" / "trackers" / "bytetrack.yaml"
    cfg = dict(utils.YAML.load(cfg_path))
    cfg["track_buffer"] = track_buffer
    return types.SimpleNamespace(**cfg)


def make_bytetrack(track_buffer_s: float, threads: int) -> Tracker:
    label_ids = {name: i for i, name in COCO_LABELS.items()}

    def track(frames: Sequence[FrameDetections], sample_fps: float) -> dict[int, list[TrackPoint]]:
        _quiet_ultralytics(threads)
        _ensure_lap()
        byte_tracker: Any = importlib.import_module("ultralytics.trackers.byte_tracker")
        tracker = byte_tracker.BYTETracker(
            _bytetrack_args(max(1, round(track_buffer_s * sample_fps)))
        )
        out: dict[int, list[TrackPoint]] = {}
        for f in frames:
            dets = f.detections
            boxes = _Boxes(
                [d.bbox for d in dets], [d.conf for d in dets], [label_ids[d.label] for d in dets]
            )
            rows = tracker.update(boxes, None)
            for row in rows:
                tid, idx = int(row[4]), int(row[7])
                d = dets[idx]
                out.setdefault(tid, []).append(
                    TrackPoint(
                        frame_idx=f.frame_idx,
                        ms=f.ms,
                        bbox=d.bbox,
                        conf=d.conf,
                        label=d.label,
                        color=d.color,
                    )
                )
        return out

    return track
