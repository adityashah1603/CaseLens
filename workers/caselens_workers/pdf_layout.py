"""Pure layout helpers for the PDF pipeline: page/block models, running-header removal and
layout-aware chunking. No PDF or OCR libraries here, so this is cheap to import and test.

Coordinates are PDF points (1/72 in), origin top-left, as PyMuPDF reports them.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

TOKENS_PER_WORD = 1.3
CHUNK_TOKENS = 500
OVERLAP = 0.15
# Header/footer bands (fraction of page height) where repeated lines count as running headers.
MARGIN_BAND = 0.12


class Block(BaseModel):
    model_config = ConfigDict(frozen=True)

    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)  # OCR only


class Page(BaseModel):
    page: int = Field(ge=1)  # 1-based
    width: float
    height: float
    blocks: list[Block]
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)  # page mean, OCR pages only


class ChunkDraft(BaseModel):
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    ocr_confidence: float | None


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def reading_order(blocks: Sequence[Block], line_tol: float = 4.0) -> list[Block]:
    """Top-to-bottom, then left-to-right for blocks on the same visual line."""
    ordered = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    rows: list[list[Block]] = []
    for b in ordered:
        if rows and abs(rows[-1][0].bbox[1] - b.bbox[1]) <= line_tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [b for row in rows for b in sorted(row, key=lambda x: x.bbox[0])]


def _signature(text: str) -> str:
    """Compare running headers ignoring page numbers, spacing and OCR punctuation noise."""
    return re.sub(r"[^a-z]", "", text.lower())


def _in_margin(block: Block, page: Page) -> bool:
    band = page.height * MARGIN_BAND
    return block.bbox[3] <= band or block.bbox[1] >= page.height - band


def drop_running_headers(pages: Sequence[Page]) -> tuple[list[Page], int]:
    """Remove header/footer blocks that repeat on 2+ pages, except on the first page they appear.

    The SYNTHETIC notice and the department letterhead repeat on every page; left in, they
    would make every chunk look alike to retrieval. They are kept once (the first page), so the
    document still carries its SYNTHETIC notice. Returns (pages, blocks_dropped).
    """
    counts = Counter(
        sig
        for p in pages
        for sig in {_signature(b.text) for b in p.blocks if _in_margin(b, p)}
        if sig
    )
    repeated = {sig for sig, n in counts.items() if n >= 2}
    seen: set[str] = set()
    dropped = 0
    out: list[Page] = []
    for p in pages:
        keep: list[Block] = []
        for b in p.blocks:
            sig = _signature(b.text)
            if sig in repeated and sig in seen and _in_margin(b, p):
                dropped += 1
                continue
            keep.append(b)
        seen.update(_signature(b.text) for b in keep if _in_margin(b, p))
        out.append(p.model_copy(update={"blocks": keep}))
    return out, dropped


def _union(boxes: Sequence[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    )


class _Unit(BaseModel):
    """A block (or a slice of an oversized block) with its word list."""

    words: list[str]
    bbox: tuple[float, float, float, float]
    confidence: float | None


def _units(page: Page, max_words: int) -> list[_Unit]:
    units: list[_Unit] = []
    for b in page.blocks:
        words = clean_text(b.text).split()
        for i in range(0, len(words), max_words):
            units.append(
                _Unit(words=words[i : i + max_words], bbox=b.bbox, confidence=b.confidence)
            )
    return units


def _draft(page: Page, units: Sequence[_Unit]) -> ChunkDraft:
    confs = [(len(u.words), u.confidence) for u in units if u.confidence is not None]
    weight = sum(n for n, _ in confs)
    conf = round(sum(n * c for n, c in confs) / weight, 4) if confs and weight else None
    if conf is None and page.ocr_confidence is not None:
        conf = page.ocr_confidence
    return ChunkDraft(
        page=page.page,
        text="\n".join(" ".join(u.words) for u in units),
        bbox=_union([u.bbox for u in units]),
        ocr_confidence=conf,
    )


def chunk_page(
    page: Page, chunk_tokens: int = CHUNK_TOKENS, overlap: float = OVERLAP
) -> list[ChunkDraft]:
    """Greedy, block-aligned chunks of ~chunk_tokens with ~overlap carried into the next chunk.

    Chunks never cross pages. Blocks larger than one chunk are split by words (keeping the
    block's bbox). Overlap re-uses whole trailing blocks when they fit in the overlap budget,
    otherwise the last words of the previous block as a text prefix.
    """
    max_words = max(1, int(chunk_tokens / TOKENS_PER_WORD))
    overlap_words = max(0, int(max_words * overlap))
    units = _units(page, max_words)
    drafts: list[ChunkDraft] = []
    start = 0
    prefix: list[_Unit] = []
    while start < len(units):
        end, words = start, sum(len(u.words) for u in prefix)
        while end < len(units) and (end == start or words + len(units[end].words) <= max_words):
            words += len(units[end].words)
            end += 1
        drafts.append(_draft(page, [*prefix, *units[start:end]]))
        if end >= len(units):
            break
        # Overlap: trailing whole blocks within the budget, else the previous block's last words.
        back, carried = end, 0
        while back - 1 > start and carried + len(units[back - 1].words) <= overlap_words:
            back -= 1
            carried += len(units[back].words)
        if back < end:
            start, prefix = back, []
        else:
            last = units[end - 1]
            tail = last.words[-overlap_words:] if overlap_words else []
            start, prefix = end, ([last.model_copy(update={"words": tail})] if tail else [])
    return drafts
