"""Detection and tracking quality on the SYNTHETIC case's video clips.

Labels: the data_gen video sidecars (`video/<clip>.json`). Each labeled track has a class, a
time span and exactly ONE keyframe box (frame index + pixel bbox), so there are 11 labeled boxes
over 5 clips. That is too few for a real mAP; what we compute:

* Detection (stored `detect/detections.json`): at each labeled keyframe, the nearest sampled
  frame (within one sampling step). Per class family (vehicle / person): recall and precision at
  IoU >= 0.5 at the detector's stored confidences >= OPERATING_CONF, and AP@0.5 (all-point VOC
  interpolation) over every stored detection on those frames. Precision and AP are LOWER BOUNDS:
  the sidecars label only story objects, and only at their own keyframe, so other real vehicles or
  people in the frame count as false positives.
* Tracking (stored `track/tracks.json`, falling back to the video chunks' locators): a labeled
  track is found when a predicted track of the same family has a point within one step of the
  keyframe time whose box has IoU >= 0.5 with the label; we report that track's id, IoU, temporal
  IoU of the two time spans, and whether a retrieval chunk cites it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from caselens_evals.components.metrics import (
    Box,
    ScoredBox,
    average_precision,
    interval_iou,
    iou,
    match_greedy,
)
from caselens_evals.components.sources import CaseSource

IOU_THRESHOLD = 0.5
OPERATING_CONF = 0.25  # Ultralytics' default predict threshold
_GOLD_FAMILY = {"sedan": "vehicle", "car": "vehicle", "pickup_truck": "vehicle", "person": "person"}
_PRED_FAMILY = {
    "car": "vehicle",
    "truck": "vehicle",
    "bus": "vehicle",
    "motorcycle": "vehicle",
    "person": "person",
}
CLASSES = ("vehicle", "person")


def as_box(values: Any) -> Box:
    x0, y0, x1, y1 = (float(v) for v in values)
    return (x0, y0, x1, y1)


def gold_family(label: str) -> str:
    return _GOLD_FAMILY.get(label, label)


def pred_family(label: str) -> str:
    return _PRED_FAMILY.get(label, label)


def load_sidecars(case_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((case_dir / "video").glob("*.json"))
    ]


def _nearest_frame(
    frames: list[dict[str, Any]], frame_idx: int, step: int
) -> dict[str, Any] | None:
    best = min(frames, key=lambda f: abs(int(f["frame_idx"]) - frame_idx), default=None)
    if best is None or abs(int(best["frame_idx"]) - frame_idx) > step:
        return None
    return best


def track_box_at(track: dict[str, Any], ms: float, tolerance_ms: float) -> list[float] | None:
    """The box of the track point nearest `ms`, if one is within `tolerance_ms`."""
    points = track.get("points") or []
    if not points:
        return None
    best = min(points, key=lambda p: abs(float(p["ms"]) - ms))
    return list(best["bbox"]) if abs(float(best["ms"]) - ms) <= tolerance_ms else None


def _pred_tracks(src: CaseSource, filename: str) -> tuple[str, list[dict[str, Any]], set[str]]:
    chunk_tracks = {
        str(c.locator["track_id"]) for c in src.chunks(filename) if c.locator.get("track_id")
    }
    data = src.artifact_json(filename, "track", "tracks.json")
    if data:
        return "track/tracks.json", list(data["tracks"]), chunk_tracks
    # Fallback: one point per track chunk (its representative frame + box).
    tracks = []
    for c in src.chunks(filename):
        loc = c.locator
        if loc.get("track_id") and loc.get("bbox"):
            label = "person" if "(person" in c.text else "car"
            tracks.append(
                {
                    "track_id": loc["track_id"],
                    "label": label,
                    "points": [{"ms": loc["start_ms"], "bbox": loc["bbox"]}],
                    "start_ms": loc["start_ms"],
                    "end_ms": loc.get("end_ms") or loc["start_ms"],
                }
            )
    return "chunk locators (representative point only)", tracks, chunk_tracks


def _span(track: dict[str, Any]) -> tuple[float, float]:
    if "start_ms" in track:
        return float(track["start_ms"]), float(track["end_ms"])
    ms = [float(p["ms"]) for p in track["points"]]
    return min(ms), max(ms)


def evaluate(src: CaseSource, case_dir: Path) -> dict[str, Any]:
    labels: dict[str, list[ScoredBox]] = defaultdict(list)
    preds_all: dict[str, list[ScoredBox]] = defaultdict(list)
    tracks_out: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for side in load_sidecars(case_dir):
        filename = f"{side['evidence_id']}.mp4"
        fps = float(side["fps"])
        dets = src.artifact_json(filename, "detect", "detections.json")
        step = int(dets["frame_step"]) if dets else 3
        tolerance_ms = 1000.0 * step / fps
        # Detection: labeled keyframes grouped by image (several labels may share a frame).
        images: dict[int, dict[str, Any] | None] = {}
        for t in side["tracks"]:
            kf = int(t["keyframe"]["frame_idx"])
            if kf not in images:
                images[kf] = _nearest_frame(dets["frames"], kf, step) if dets else None
            frame = images[kf]
            if frame is None:
                continue
            key = f"{filename}#{kf}"
            labels[gold_family(t["label"])].append(
                ScoredBox(image=key, box=as_box(t["keyframe"]["bbox"]))
            )
        for kf, frame in images.items():
            if frame is None:
                continue
            for d in frame["detections"]:
                preds_all[pred_family(d["label"])].append(
                    ScoredBox(
                        image=f"{filename}#{kf}", box=as_box(d["bbox"]), score=float(d["conf"])
                    )
                )
        # Tracking.
        source, ptracks, chunk_tracks = _pred_tracks(src, filename)
        sources[filename] = source
        for t in side["tracks"]:
            kf_ms = 1000.0 * int(t["keyframe"]["frame_idx"]) / fps
            family = gold_family(t["label"])
            best: tuple[float, dict[str, Any]] | None = None
            for pt in ptracks:
                if pred_family(str(pt["label"])) != family:
                    continue
                box = track_box_at(pt, kf_ms, tolerance_ms)
                if box is None:
                    continue
                v = iou(t["keyframe"]["bbox"], box)
                if best is None or v > best[0]:
                    best = (v, pt)
            found = best is not None and best[0] >= IOU_THRESHOLD
            entry: dict[str, Any] = {
                "clip": filename,
                "label_track": t["track_id"],
                "class": family,
                "entity_id": t.get("entity_id"),
                "best_iou": round(best[0], 3) if best else 0.0,
                "found": found,
            }
            if best is not None and found:
                pt = best[1]
                entry["pred_track"] = pt["track_id"]
                entry["temporal_iou"] = round(
                    interval_iou((float(t["start_ms"]), float(t["end_ms"])), _span(pt)), 3
                )
                entry["cited_by_chunk"] = str(pt["track_id"]) in chunk_tracks
            tracks_out.append(entry)

    detection: dict[str, Any] = {}
    for cls in CLASSES:
        gold, preds = labels[cls], preds_all[cls]
        operating = [p for p in preds if p.score >= OPERATING_CONF]
        flags = match_greedy(gold, operating, IOU_THRESHOLD)
        tp = sum(flags)
        detection[cls] = {
            "labels": len(gold),
            "predictions": len(operating),
            "tp": tp,
            "recall": round(tp / len(gold), 3) if gold else None,
            "precision_lower_bound": round(tp / len(operating), 3) if operating else None,
            "ap50_lower_bound": round(average_precision(gold, preds, IOU_THRESHOLD), 3),
        }
    aps = [detection[c]["ap50_lower_bound"] for c in CLASSES if detection[c]["labels"]]
    by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"labels": 0, "found": 0})
    for e in tracks_out:
        by_class[e["class"]]["labels"] += 1
        by_class[e["class"]]["found"] += int(e["found"])
    return {
        "detection": detection,
        "map50_lower_bound": round(sum(aps) / len(aps), 3) if aps else None,
        "tracking": {
            "by_class": {
                c: {**v, "recall": round(v["found"] / v["labels"], 3)} for c, v in by_class.items()
            },
            "tracks": tracks_out,
            "sources": sources,
        },
        "method": __doc__,
    }
