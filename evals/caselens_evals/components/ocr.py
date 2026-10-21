"""OCR character error rate on the scanned statement (ws_pike.pdf).

Gold: the generator builds ws_pike as a digital PDF and then rasterises + degrades it into the
image-only scan (data_gen `render_digital` -> `scan_pdf`). We re-render that digital twin from the
story (deterministic) and read its text layer with PyMuPDF blocks, then apply the pipeline's own
layout normalization (reading order, running headers kept once) so CER measures recognition, not
the header-dropping policy. Hypothesis: the ingested chunks of ws_pike in chunk order, with each
chunk's carried-over overlap prefix removed. Both sides are compared per page and in total.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from caselens_data_gen.documents import render_digital
from caselens_data_gen.documents.pdfio import close, open_pdf
from caselens_data_gen.story import load_story
from caselens_evals.components.metrics import cer, collapse_ws, normalize_ocr, strip_ws
from caselens_evals.components.sources import CaseSource, Chunk
from caselens_workers.pdf_layout import Block, Page, clean_text, drop_running_headers, reading_order

FILENAME = "ws_pike.pdf"
PLAN_ID = "ws_pike"
# Gold blocks with at least this many words are narrative paragraphs (not form fields).
BODY_MIN_WORDS = 8
NEWLINE = "\n"


def gold_blocks(story_slug: str, plan_id: str = PLAN_ID) -> dict[int, list[str]]:
    """Page number -> text blocks (reading order) of the digital twin the scan was made from."""
    _, digital = render_digital(load_story(story_slug), plan_id)
    doc = open_pdf(digital)
    pages: list[Page] = []
    try:
        for index in range(doc.page_count):
            pg = doc[index]
            raw = cast(list[tuple[Any, ...]], pg.get_text("blocks"))  # type: ignore[no-untyped-call]
            blocks = [
                Block(
                    bbox=(float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                    text=clean_text(str(b[4])),
                )
                for b in raw
                if b[6] == 0 and clean_text(str(b[4]))
            ]
            pages.append(
                Page(
                    page=index + 1,
                    width=pg.rect.width,
                    height=pg.rect.height,
                    blocks=reading_order(blocks),
                )
            )
    finally:
        close(doc)
    pages, _ = drop_running_headers(pages)
    return {p.page: [b.text for b in p.blocks] for p in pages}


def strip_overlap(prev_words: Sequence[str], words: Sequence[str]) -> list[str]:
    """Drop the longest prefix of `words` that repeats the tail of `prev_words` (chunk overlap)."""
    for k in range(min(len(prev_words), len(words)), 0, -1):
        if list(prev_words[-k:]) == list(words[:k]):
            return list(words[k:])
    return list(words)


def hypothesis_pages(chunks: Sequence[Chunk]) -> dict[int, str]:
    """Page -> OCR text from chunks in chunk order, overlap duplicates removed (chunks never
    cross pages, so overlap is only removed between consecutive chunks of one page)."""
    pages: dict[int, list[str]] = {}
    prev: dict[int, list[str]] = {}
    for c in sorted(chunks, key=lambda ch: ch.index):
        page = int(c.locator.get("page", 0))
        words = c.text.split()
        kept = strip_overlap(prev.get(page, []), words)
        prev[page] = words
        if len(kept) == len(words):
            pages.setdefault(page, []).append(c.text)
        elif kept:
            pages.setdefault(page, []).append(" ".join(kept))
    return {p: NEWLINE.join(parts) for p, parts in pages.items()}


def local_cer(
    gold: Sequence[str], hypothesis: str, min_words: int = BODY_MIN_WORDS
) -> tuple[float, int]:
    """Reading-order-free CER over the narrative body; returns (cer, gold chars scored).

    Each gold block of at least `min_words` words (the handwritten paragraphs, not the form's
    label/value fields whose order the skew scrambles) is aligned to its best-matching window of
    the hypothesis (rapidfuzz partial_ratio_alignment), lowercased with whitespace removed;
    CER = sum of edit distances / sum of gold block lengths.
    """
    hyp = strip_ws(hypothesis)
    edits = total = 0
    for block in gold:
        if len(block.split()) < min_words:
            continue
        ref = strip_ws(block)
        found = fuzz.partial_ratio_alignment(ref, hyp)
        window = hyp[found.dest_start : found.dest_end] if found is not None else ""
        edits += Levenshtein.distance(ref, window)
        total += len(ref)
    return (edits / total if total else 0.0), total


def _rates(gold: str, hyp: str) -> dict[str, Any]:
    return {
        "gold_chars": len(collapse_ws(gold)),
        "hyp_chars": len(collapse_ws(hyp)),
        "cer_raw": round(cer(collapse_ws(gold), collapse_ws(hyp)), 4),
        "cer_normalized": round(cer(normalize_ocr(gold), normalize_ocr(hyp)), 4),
        "cer_no_whitespace": round(cer(strip_ws(gold), strip_ws(hyp)), 4),
    }


def evaluate(src: CaseSource, story_slug: str) -> dict[str, Any]:
    blocks = gold_blocks(story_slug)
    gold = {p: NEWLINE.join(b) for p, b in blocks.items()}
    hyp = hypothesis_pages(src.chunks(FILENAME))
    per_page = {
        str(p): _rates(gold.get(p, ""), hyp.get(p, "")) for p in sorted(set(gold) | set(hyp))
    }
    all_gold = NEWLINE.join(gold[p] for p in sorted(gold))
    all_hyp = NEWLINE.join(hyp[p] for p in sorted(hyp))
    total = _rates(all_gold, all_hyp)
    body_cer, body_chars = local_cer([b for p in sorted(blocks) for b in blocks[p]], all_hyp)
    total["cer_body_order_free"] = round(body_cer, 4)
    total["body_chars"] = body_chars
    return {
        "file": FILENAME,
        "total": total,
        "per_page": per_page,
        "method": (
            "gold = text layer of the digital twin re-rendered from the story "
            "(data_gen render_digital), PyMuPDF blocks in the pipeline's reading order with "
            "running headers kept once; hypothesis = ingested chunks in chunk order with overlap "
            "prefixes removed. CER = Levenshtein(gold, hyp) / len(gold). cer_raw: whitespace "
            "runs collapsed, case kept; cer_normalized: also lowercased; cer_no_whitespace: "
            "lowercased with all whitespace removed (word splits/joins ignored). The whole-page "
            "numbers include reading-order differences (the scan skew reorders the form's "
            "label/value fields); cer_body_order_free aligns each narrative paragraph "
            f"(>= {BODY_MIN_WORDS} words) to its best window of the OCR text, lowercased "
            "without whitespace, so it isolates character recognition."
        ),
    }
