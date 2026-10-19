"""SYNTHETIC ground-truth stories that every evidence generator reads from."""

from __future__ import annotations

from pathlib import Path

from caselens_data_gen.story.models import Story

STORY_SLUGS = ("riverside_lot", "harbor_street")

_DATA_DIR = Path(__file__).parent / "data"


def load_story(slug: str) -> Story:
    """Load and validate one story by slug (see ``STORY_SLUGS``)."""
    if slug not in STORY_SLUGS:
        raise ValueError(f"unknown story slug {slug!r}; expected one of {STORY_SLUGS}")
    text = (_DATA_DIR / f"{slug}.json").read_text(encoding="utf-8")
    return Story.model_validate_json(text)


__all__ = ["STORY_SLUGS", "Story", "load_story"]
