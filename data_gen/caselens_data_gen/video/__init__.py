"""SYNTHETIC CCTV evidence: real MEVA footage (CC BY 4.0) relabeled to the story.

For every clip in ``sources.json`` this downloads (once, into ``data_gen/.cache/meva/``, sha256
pinned) a short range of a public MEVA clip, trims it, and writes
``<case_dir>/video/<evidence_id>.mp4`` (mp4v, 15 fps, <= 960 px wide) with a burned-in
"SYNTHETIC - CaseLens demo (MEVA CC BY 4.0)" banner, the story camera name and a story
wall-clock timestamp, plus ``<evidence_id>.json``: notice, source attribution, recorded_at,
time mapping, and per-track summaries (the chunks the guide wants indexed). The sidecar is not
evidence and is not listed in the manifest.

Only facts the footage genuinely shows become FactLocations; everything else the story says the
clip shows is listed under ``ungrounded`` in ``sources.json`` and in the sidecar. Nothing is drawn
into the footage except the overlay text.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from caselens_data_gen.common import SYNTHETIC_NOTICE, FactLocation, GeneratedFile
from caselens_data_gen.story import Story
from caselens_data_gen.video.meva import MEVA_ATTRIBUTION, ensure_source, meva_url
from caselens_data_gen.video.render import (
    ClipTiming,
    fill_times,
    render_clip,
    scale_bbox,
    timing_for,
)
from caselens_data_gen.video.spec import (
    ClipSpec,
    Sidecar,
    SidecarKeyframe,
    SidecarSource,
    SidecarTrack,
    SourcesFile,
    SourceSpec,
    load_sources,
)

__all__ = [
    "DEFAULT_CACHE",
    "build_sidecar",
    "fact_locations",
    "generate",
    "load_sources",
    "render_one",
]

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / ".cache" / "meva"
CACHE_ENV = "CASELENS_DATA_GEN_CACHE"


def fact_locations(
    clip: ClipSpec, source: SourceSpec, timing: ClipTiming, size: tuple[int, int]
) -> list[FactLocation]:
    out_w, out_h = size
    facts: list[FactLocation] = []
    for fact in clip.facts:
        locator = {
            "kind": "video",
            "start_ms": timing.clip_ms(fact.start_frame),
            "end_ms": timing.clip_ms(fact.end_frame),
            "frame_idx": timing.out_index(fact.key_frame),
            "bbox": list(scale_bbox(fact.bbox, source.width, source.height, out_w, out_h)),
            "track_id": fact.track_id,
        }
        facts.append(FactLocation(fact_id=fact.fact_id, locator=locator, note=fact.note))
    return facts


def build_sidecar(
    story: Story,
    clip: ClipSpec,
    source: SourceSpec,
    timing: ClipTiming,
    size: tuple[int, int],
) -> Sidecar:
    plan = next(p for p in story.evidence_plan if p.id == clip.evidence_id)
    camera = next(c for c in story.cameras if c.id == plan.camera_id)
    out_w, out_h = size
    lo, hi = source.frame_range or (0, clip.end_frame)
    tracks = [
        SidecarTrack(
            track_id=t.track_id,
            label=t.label,
            color_as_story=t.color_as_story,
            entity_id=t.entity_id,
            start_ms=timing.clip_ms(t.start_frame),
            end_ms=timing.clip_ms(t.end_frame),
            wall_clock_start=timing.wall_clock(t.start_frame),
            wall_clock_end=timing.wall_clock(t.end_frame),
            keyframe=SidecarKeyframe(
                frame_idx=timing.out_index(t.key_frame),
                bbox=scale_bbox(t.bbox, source.width, source.height, out_w, out_h),
            ),
            summary=fill_times(t.summary, timing),
        )
        for t in clip.tracks
    ]
    return Sidecar(
        notice=SYNTHETIC_NOTICE,
        evidence_id=clip.evidence_id,
        title=plan.title,
        camera_id=camera.id,
        camera_name=camera.name,
        recorded_at=timing.recorded_at,
        fps=timing.out_fps,
        frame_count=timing.frame_count,
        duration_ms=timing.duration_ms,
        width=out_w,
        height=out_h,
        time_mapping=(
            f"Story wall-clock = {clip.anchor_wall_clock.isoformat()} + (source frame - "
            f"{clip.anchor_frame}) / {source.fps:g} s; anchor: {clip.anchor_reason}. "
            "Real-time playback, one constant offset."
        ),
        source=SidecarSource(
            key=source.key,
            url=meva_url(source.key),
            frame_range=(lo, hi),
            sha256=source.sha256,
            license="CC BY 4.0",
            attribution=MEVA_ATTRIBUTION,
            view=source.view,
        ),
        tracks=tracks,
        ungrounded_story_facts=list(clip.ungrounded),
    )


def render_one(
    story: Story,
    clip: ClipSpec,
    source: SourceSpec,
    source_path: Path,
    out_dir: Path,
) -> GeneratedFile:
    plan = next(p for p in story.evidence_plan if p.id == clip.evidence_id)
    camera = next(c for c in story.cameras if c.id == plan.camera_id)
    timing = timing_for(clip, source)
    out_path = out_dir / f"{clip.evidence_id}.mp4"
    size = render_clip(source_path, source, timing, clip.out_width, camera.name, out_path)
    sidecar = build_sidecar(story, clip, source, timing, size)
    (out_dir / f"{clip.evidence_id}.json").write_text(
        json.dumps(sidecar.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    return GeneratedFile(
        path=out_path,
        evidence_type="video",
        description=(
            f"{plan.title} (SYNTHETIC relabel of MEVA {source.key.rsplit('/', 1)[-1]}, "
            f"CC BY 4.0; track summaries in {clip.evidence_id}.json)"
        ),
        facts=fact_locations(clip, source, timing, size),
        recorded_at=timing.recorded_at.isoformat(timespec="milliseconds"),
    )


def generate(
    story: Story,
    case_dir: Path,
    sources: SourcesFile | None = None,
    cache_dir: Path | None = None,
) -> list[GeneratedFile]:
    sources = sources or load_sources()
    cache = cache_dir or Path(os.environ.get(CACHE_ENV, DEFAULT_CACHE))
    out_dir = case_dir / "video"
    produced: list[GeneratedFile] = []
    for clip in sources.clips:
        if clip.case_slug != story.case_slug:
            continue
        source = sources.source(clip.source)
        path = ensure_source(cache, source.name, source.key, source.sha256, source.frame_range)
        produced.append(render_one(story, clip, source, path, out_dir))
    return produced
