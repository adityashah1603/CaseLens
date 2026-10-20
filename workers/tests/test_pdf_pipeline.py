"""PDF pipeline on tiny SYNTHETIC PDFs: digital path, scanned (OCR) path, low-confidence
VLM fallback hook, plus the pure layout/chunking helpers."""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from caselens_workers import jobs
from caselens_workers.db import Conn
from caselens_workers.pdf_layout import Block, Page, chunk_page, drop_running_headers
from caselens_workers.pipeline import Pipeline, run_job
from caselens_workers.pipelines import pdf as pdf_pipe
from caselens_workers.settings import WorkerSettings
from caselens_workers.stages.embed import EMBEDDING_DIM, make_embed_stage
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db

HEADER = "SYNTHETIC DATA - fictional test document"
FOOTER = "SYNTHETIC DATA - generated for CaseLens tests. No real people or events."


def _fake_embedder(texts: Sequence[str]) -> list[list[float]]:
    return [[0.5] * EMBEDDING_DIM for _ in texts]


PIPE: Pipeline = dataclasses.replace(
    pdf_pipe.PIPELINE,
    stages={**pdf_pipe.PIPELINE.stages, "embed": make_embed_stage(_fake_embedder)},
)
DIGITAL = ["parse", "layout", "chunk", "embed", "extract"]
SCANNED = ["parse", "render", "ocr", "ocr_check", "chunk", "embed", "extract"]


def _paragraph(page: int, i: int) -> str:
    words = " ".join(f"word{page}x{i}x{k}" for k in range(60))
    return f"Paragraph {i} on page {page}: the gray sedan {words}."


def _digital_pdf(path: Path) -> None:
    pymupdf: Any = importlib.import_module("pymupdf")  # partial annotations

    doc = pymupdf.open()
    for page_no in (1, 2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((200, 30), HEADER, fontsize=9)
        y = 80.0
        for i in range(8 if page_no == 1 else 2):
            rect = pymupdf.Rect(56, y, 556, y + 70)
            page.insert_textbox(rect, _paragraph(page_no, i), fontsize=8)
            y += 80
        page.insert_text((120, 760), FOOTER, fontsize=7)
    doc.save(path)


def _scanned_pdf(path: Path, lines: Sequence[str]) -> None:
    """Draw text, rasterize it, and keep only the image (no text layer), like a scan."""
    pymupdf: Any = importlib.import_module("pymupdf")  # partial annotations

    src = pymupdf.open()
    page = src.new_page(width=400, height=200)
    for i, line in enumerate(lines):
        page.insert_text((30, 50 + 40 * i), line, fontsize=20)
    png = page.get_pixmap(dpi=150).tobytes("png")
    out = pymupdf.open()
    scan = out.new_page(width=400, height=200)
    scan.insert_image(scan.rect, stream=png)
    out.save(path)


def _run(
    conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    path: Path,
    settings: WorkerSettings,
) -> tuple[SeededEvidence, list[str]]:
    ev = seed_evidence(path, "pdf")
    jobs.PostgresQueue().enqueue(conn, ev.case_id, ev.evidence_id, PIPE.first)
    as_role(conn, "caselens_worker")
    stages = []
    while (job := jobs.claim_next(conn, set(PIPE.stages), 3, 1800)) is not None:
        status = run_job(conn, job, {"pdf": PIPE}, ev.store, settings, jobs.PostgresQueue())
        assert status == "succeeded", job.stage
        stages.append(job.stage)
    return ev, stages


def _chunks(conn: Conn, ev: SeededEvidence) -> list[tuple[str, dict[str, Any], int]]:
    rows = conn.execute(
        "SELECT text, locator, vector_dims(embedding) FROM public.chunks WHERE evidence_id = %s "
        "ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _summary(conn: Conn, ev: SeededEvidence, stage: str) -> dict[str, Any]:
    as_role(conn, None)
    row = conn.execute(
        "SELECT payload FROM public.audit_log WHERE case_id = %s AND payload->>'stage' = %s",
        (ev.case_id, stage),
    ).fetchone()
    assert row is not None
    payload: dict[str, Any] = row[0]
    return payload


def test_digital_pdf_chunks_with_bboxes_and_overlap(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    f = tmp_path / "report.pdf"
    _digital_pdf(f)
    ev, stages = _run(owner_conn, seed_evidence, f, worker_settings)
    assert stages == DIGITAL
    chunks = _chunks(owner_conn, ev)
    pages = [c[1]["page"] for c in chunks]
    assert pages == sorted(pages) and set(pages) == {1, 2}
    page1 = [c for c in chunks if c[1]["page"] == 1]
    assert len(page1) >= 2  # 8 paragraphs of ~68 words exceed one ~500-token chunk
    for text, loc, dims in chunks:
        assert dims == EMBEDDING_DIM and loc["kind"] == "pdf" and loc["ocr_confidence"] is None
        x0, y0, x1, y1 = loc["bbox"]
        assert 0 <= x0 < x1 <= 612 and 0 <= y0 < y1 <= 792
        assert len(text.split()) * 1.3 <= 520
        # Chunks never cross pages.
        other = 2 if loc["page"] == 1 else 1
        assert f"word{other}x" not in text
    # ~15% overlap (57 words of a 384-word budget) carried from chunk 0 into chunk 1.
    assert page1[1][0].split()[:57] == page1[0][0].split()[-57:]
    # Running header/footer kept once per document, not repeated in every chunk.
    joined = "\n".join(c[0] for c in chunks)
    assert joined.count(HEADER) == 1 and joined.count(FOOTER) == 1


def test_scanned_pdf_takes_ocr_path(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f, ["SYNTHETIC GRAY SEDAN", "PLATE SYN 4821"])
    ev, stages = _run(owner_conn, seed_evidence, f, worker_settings)
    assert stages == SCANNED
    chunks = _chunks(owner_conn, ev)
    assert len(chunks) == 1
    text, loc, _ = chunks[0]
    assert "SEDAN" in text.upper() and "4821" in text
    assert loc["page"] == 1 and loc["ocr_confidence"] > 0.6
    x0, y0, x1, y1 = loc["bbox"]
    # PDF points, not pixels: the text was drawn from (30, ~30) to (~300, ~95) on a 400x200 page.
    assert 10 < x0 < 60 and 15 < y0 < 60 and 150 < x1 <= 400 and 60 < y1 < 120
    summary = _summary(owner_conn, ev, "ocr_check")
    assert summary["vlm_fallback"] == "not_needed" and summary["low_confidence_pages"] == []


def _low_conf_engine(png: bytes) -> list[tuple[list[list[float]], str, float]]:
    box = [[100.0, 100.0], [500.0, 100.0], [500.0, 140.0], [100.0, 140.0]]
    return [(box, "GRAV SEDAM", 0.3)]


@pytest.mark.parametrize("vlm", [False, True])
def test_low_confidence_page_vlm_fallback_is_optional(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
    monkeypatch: pytest.MonkeyPatch,
    vlm: bool,
) -> None:
    from caselens_agent.router import ModelRouter, RouterSettings

    calls: list[str] = []

    def fake(task: str, system: str, user: str, schema: object) -> str:
        calls.append(task)
        return '{"text": "SYNTHETIC gray sedan"}'

    router = ModelRouter(RouterSettings(llm_provider="fake"), fake=fake)
    monkeypatch.setattr(pdf_pipe, "ocr_engine", _low_conf_engine)
    monkeypatch.setattr(pdf_pipe, "vlm_router_factory", lambda: router if vlm else None)
    f = tmp_path / "faint.pdf"
    _scanned_pdf(f, ["SYNTHETIC"])
    ev, stages = _run(owner_conn, seed_evidence, f, worker_settings)
    assert stages == SCANNED
    (text, loc, _), *rest = _chunks(owner_conn, ev)
    assert rest == []
    assert loc["ocr_confidence"] == pytest.approx(0.3)
    summary = _summary(owner_conn, ev, "ocr_check")
    assert summary["low_confidence_pages"] == [1]
    if vlm:
        assert summary["vlm_fallback"] == "used" and calls == ["caption"]
        assert text == "SYNTHETIC gray sedan"
    else:
        assert summary["vlm_fallback"] == "unavailable" and calls == []
        assert text == "GRAV SEDAM"


def _block(y: float, text: str) -> Block:
    return Block(bbox=(50.0, y, 500.0, y + 10), text=text)


def test_chunk_page_respects_budget_and_overlaps() -> None:
    blocks = [_block(100 + 20 * i, " ".join(f"w{i}_{k}" for k in range(100))) for i in range(9)]
    page = Page(page=3, width=612, height=792, blocks=blocks)
    drafts = chunk_page(page)
    max_words = int(500 / 1.3)
    assert len(drafts) >= 3
    for d in drafts:
        assert d.page == 3 and len(d.text.split()) <= max_words
    # Blocks are 100 words (> 15% budget of 57), so overlap is a word prefix of the last block.
    first_tail = drafts[0].text.split()[-57:]
    assert drafts[1].text.split()[:57] == first_tail
    # An oversized block is split by words but keeps its bbox.
    huge = Page(page=1, width=612, height=792, blocks=[_block(100, "x " * 1000)])
    parts = chunk_page(huge)
    assert len(parts) >= 3 and all(p.bbox == (50.0, 100.0, 500.0, 110.0) for p in parts)


def test_running_headers_dropped_after_first_page() -> None:
    def page(n: int) -> Page:
        return Page(
            page=n,
            width=612,
            height=792,
            blocks=[
                Block(bbox=(200, 20, 400, 30), text=f"SYNTHETIC DATA | Page {n} of 3"),
                _block(300, f"body {n}"),
                Block(bbox=(100, 760, 500, 770), text="SYNTHETIC footer"),
            ],
        )

    pages, dropped = drop_running_headers([page(1), page(2), page(3)])
    assert dropped == 4
    assert [len(p.blocks) for p in pages] == [3, 1, 1]


def test_respace_inserts_word_breaks_from_char_boxes() -> None:
    def box(x: float) -> list[list[float]]:
        return [[x, 0], [x + 10, 0], [x + 10, 20], [x, 20]]

    # "abcd": a-b touch, b-c gap of 9 px (> 0.65 * 10), c-d touch.
    boxes = [box(0), box(10), box(29), box(39)]
    assert pdf_pipe.respace("abcd", boxes) == "ab cd"
    assert pdf_pipe.respace("abc", boxes) == "abc"  # box count mismatch: unchanged
