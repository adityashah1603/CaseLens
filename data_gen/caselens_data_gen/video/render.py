"""Trim a cached MEVA source, burn in the SYNTHETIC overlay, and map clip time to story time.

Time relabel: one constant offset per clip. Source frame ``anchor_frame`` (the observed moment,
for example a vehicle entering) maps exactly to ``anchor_wall_clock``; every other frame is
``anchor_wall_clock + (frame - anchor_frame) / source_fps``. Playback speed is never changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from caselens_data_gen.video.spec import Bbox, ClipSpec, SourceSpec

OVERLAY_TEXT = "SYNTHETIC - CaseLens demo (MEVA CC BY 4.0)"
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_PLACEHOLDER = re.compile(r"\{t:(\d+)\}")

Frame = npt.NDArray[np.uint8]


@dataclass(frozen=True)
class ClipTiming:
    """Frame/time arithmetic for one output clip cut from a source at ``src_fps``."""

    start_frame: int
    end_frame: int
    anchor_frame: int
    anchor_wall_clock: datetime
    src_fps: float
    out_fps: float

    @property
    def step(self) -> int:
        step = self.src_fps / self.out_fps
        if abs(step - round(step)) > 1e-9 or round(step) < 1:
            raise ValueError("out_fps must divide the source fps")
        return round(step)

    @property
    def frame_count(self) -> int:
        return len(range(self.start_frame, self.end_frame, self.step))

    @property
    def duration_ms(self) -> int:
        return round(self.frame_count * 1000 / self.out_fps)

    def wall_clock(self, src_frame: int) -> datetime:
        """Story wall-clock of ``src_frame``, rounded to the millisecond."""
        ms = round((src_frame - self.anchor_frame) * 1000 / self.src_fps)
        return self.anchor_wall_clock + timedelta(milliseconds=ms)

    @property
    def recorded_at(self) -> datetime:
        return self.wall_clock(self.start_frame)

    def clip_ms(self, src_frame: int) -> int:
        """Milliseconds from the start of the output clip to ``src_frame``."""
        return round((src_frame - self.start_frame) * 1000 / self.src_fps)

    def out_index(self, src_frame: int) -> int:
        """Output frame index that shows ``src_frame`` (the nearest earlier kept frame)."""
        return min((src_frame - self.start_frame) // self.step, self.frame_count - 1)

    def out_wall_clock(self, out_idx: int) -> datetime:
        return self.wall_clock(self.start_frame + out_idx * self.step)


def timing_for(clip: ClipSpec, source: SourceSpec) -> ClipTiming:
    return ClipTiming(
        start_frame=clip.start_frame,
        end_frame=clip.end_frame,
        anchor_frame=clip.anchor_frame,
        anchor_wall_clock=clip.anchor_wall_clock,
        src_fps=source.fps,
        out_fps=clip.out_fps,
    )


def format_stamp(moment: datetime) -> str:
    """Camera-style burned-in timestamp, floored to the second."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def fill_times(template: str, timing: ClipTiming) -> str:
    """Replace ``{t:FRAME}`` with the story wall-clock (HH:MM:SS) of that source frame."""
    return _PLACEHOLDER.sub(
        lambda m: timing.wall_clock(int(m.group(1))).strftime("%H:%M:%S"), template
    )


def output_size(src_w: int, src_h: int, out_width: int) -> tuple[int, int]:
    out_h = round(src_h * out_width / src_w)
    return out_width, out_h + (out_h & 1)


def scale_bbox(
    bbox: Bbox, src_w: int, src_h: int, out_w: int, out_h: int
) -> tuple[float, float, float, float]:
    sx, sy = out_w / src_w, out_h / src_h
    x0, y0, x1, y1 = bbox
    return (round(x0 * sx, 1), round(y0 * sy, 1), round(x1 * sx, 1), round(y1 * sy, 1))


def draw_overlay(frame: Frame, camera_name: str, stamp: str) -> Frame:
    """Burn the SYNTHETIC banner (top) and camera name + story timestamp (bottom) into a copy."""
    out = frame.copy()
    h, w = out.shape[:2]
    scale = max(w / 960 * 0.6, 0.35)
    thick = 1 if scale < 0.5 else 2
    bar = int(30 * scale / 0.6)
    out[:bar] = (out[:bar] * 0.4).astype(np.uint8)
    out[h - bar :] = (out[h - bar :] * 0.4).astype(np.uint8)
    base = int(bar * 0.72)
    cv2.putText(out, OVERLAY_TEXT, (8, base), _FONT, scale, (0, 215, 255), thick, cv2.LINE_AA)
    cv2.putText(out, camera_name, (8, h - bar + base), _FONT, scale, (255, 255, 255), thick)
    (tw, _), _ = cv2.getTextSize(stamp, _FONT, scale, thick)
    cv2.putText(out, stamp, (w - tw - 8, h - bar + base), _FONT, scale, (255, 255, 255), thick)
    return out


def render_clip(
    source_path: Path,
    source: SourceSpec,
    timing: ClipTiming,
    out_width: int,
    camera_name: str,
    out_path: Path,
) -> tuple[int, int]:
    """Decode the source, keep every ``step``-th frame of the window, overlay, write mp4v .mp4.

    Returns the output (width, height).
    """
    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open {source_path}")
    out_w, out_h = output_size(source.width, source.height, out_width)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter.fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, timing.out_fps, (out_w, out_h))
    if not writer.isOpened():
        raise RuntimeError(f"cannot write {out_path}")
    interp = cv2.INTER_AREA if out_w < source.width else cv2.INTER_LINEAR
    written = 0
    src_frame = source.first_frame
    try:
        while src_frame < timing.end_frame:
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"{source_path}: ran out of frames at {src_frame}")
            offset = src_frame - timing.start_frame
            if offset >= 0 and offset % timing.step == 0:
                resized = cv2.resize(frame, (out_w, out_h), interpolation=interp)
                stamp = format_stamp(timing.out_wall_clock(written))
                writer.write(draw_overlay(resized.astype(np.uint8), camera_name, stamp))
                written += 1
            src_frame += 1
    finally:
        capture.release()
        writer.release()
    if written != timing.frame_count:
        raise RuntimeError(f"{out_path}: wrote {written} frames, expected {timing.frame_count}")
    return out_w, out_h
