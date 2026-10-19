"""Tests for the SYNTHETIC PDF document generator (reports, statements, scanned statement)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caselens_data_gen.common import GeneratedFile
from caselens_data_gen.documents import generate, render_digital, required_fact_ids
from caselens_data_gen.documents.facts import StoryView
from caselens_data_gen.documents.layout import HEADER_LINE, Para, locate_phrase, render_pdf
from caselens_data_gen.documents.pdfio import close, open_pdf, page_text
from caselens_data_gen.story import Story, load_story


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _phrase(note: str) -> str:
    match = re.match(r'key phrase: "(.*?)"(;|$)', note)
    assert match, note
    return match.group(1)


@pytest.fixture(scope="module")
def harbor(tmp_path_factory: pytest.TempPathFactory) -> tuple[Story, Path, list[GeneratedFile]]:
    story = load_story("harbor_street")
    case_dir = tmp_path_factory.mktemp("harbor_street")
    return story, case_dir, generate(story, case_dir)


@pytest.fixture(scope="module")
def riverside() -> Story:
    return load_story("riverside_lot")


def _check_digital(data: bytes, facts: list[tuple[str, int, list[float], str]]) -> None:
    doc = open_pdf(data)
    try:
        assert doc.page_count >= 1
        for index in range(doc.page_count):
            assert HEADER_LINE in page_text(doc[index]), f"page {index + 1} lacks SYNTHETIC header"
        for fact_id, page, bbox, phrase in facts:
            assert 1 <= page <= doc.page_count, fact_id
            target = doc[page - 1]
            assert _norm(phrase) in _norm(page_text(target)), (fact_id, phrase)
            x0, y0, x1, y1 = bbox
            assert 0 <= x0 < x1 <= target.rect.width and 0 <= y0 < y1 <= target.rect.height
            clipped = page_text(target, clip=(x0, y0, x1, y1))
            assert _norm(phrase) in _norm(clipped), (fact_id, phrase, clipped)
    finally:
        close(doc)


def test_harbor_files_and_facts(harbor: tuple[Story, Path, list[GeneratedFile]]) -> None:
    story, case_dir, files = harbor
    sv = StoryView(story)
    expected = {p.id for p in story.evidence_plan if p.type == "pdf"}
    assert {f.path.stem for f in files} == expected
    for gen in files:
        assert gen.path.parent == case_dir / "pdf" and gen.path.exists()
        assert gen.evidence_type == "pdf" and gen.recorded_at
        covered = {fact.fact_id for fact in gen.facts}
        assert required_fact_ids(sv, gen.path.stem) <= covered
        facts = [
            (f.fact_id, f.locator["page"], f.locator["bbox"], _phrase(f.note)) for f in gen.facts
        ]
        assert all(f.locator["kind"] == "pdf" for f in gen.facts)
        _check_digital(gen.path.read_bytes(), facts)


def test_harbor_conflict_phrase_present(harbor: tuple[Story, Path, list[GeneratedFile]]) -> None:
    _, case_dir, _ = harbor
    doc = open_pdf((case_dir / "pdf" / "ws_hs_ferrante.pdf").read_bytes())
    try:
        assert "around one in the morning" in page_text(doc[0]).lower()
    finally:
        close(doc)


def test_generation_is_deterministic(
    harbor: tuple[Story, Path, list[GeneratedFile]], tmp_path: Path
) -> None:
    story, case_dir, _ = harbor
    generate(story, tmp_path)
    for path in (case_dir / "pdf").glob("*.pdf"):
        assert (tmp_path / "pdf" / path.name).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("plan_id", ["rpt_incident", "rpt_supp_02", "ws_ruiz", "ws_darnell"])
def test_riverside_digital_facts(riverside: Story, plan_id: str) -> None:
    spec, data = render_digital(riverside, plan_id)
    doc = open_pdf(data)
    try:
        facts: list[tuple[str, int, list[float], str]] = []
        for fact_id, phrase in spec.facts:
            page, bbox = locate_phrase(doc, phrase)
            facts.append((fact_id, page, list(bbox), phrase))
    finally:
        close(doc)
    assert required_fact_ids(StoryView(riverside), plan_id) <= {f[0] for f in facts}
    _check_digital(data, facts)


def test_inconsistent_statement_has_wrong_time_and_color(riverside: Story) -> None:
    _, data = render_digital(riverside, "ws_whitfield")
    doc = open_pdf(data)
    try:
        text = " ".join(page_text(doc[i]) for i in range(doc.page_count))
    finally:
        close(doc)
    flat = " ".join(text.split()).lower()
    assert "around 10:15 pm" in flat
    assert "a blue car took off out of the lot" in flat
    assert "gray" not in flat and "grey" not in flat


def test_scanned_statement_is_image_only(riverside: Story, tmp_path: Path) -> None:
    pike_only = riverside.model_copy(
        update={"evidence_plan": [p for p in riverside.evidence_plan if p.id == "ws_pike"]}
    )
    files = generate(pike_only, tmp_path)
    assert len(files) == 1
    gen = files[0]
    _, digital = render_digital(riverside, "ws_pike")
    digital_doc = open_pdf(digital)
    scanned = open_pdf(gen.path.read_bytes())
    try:
        assert scanned.page_count == digital_doc.page_count == 2
        for index in range(scanned.page_count):
            page = scanned[index]
            assert page_text(page).strip() == ""
            assert len(page.get_images()) == 1
            assert not page.get_fonts()
        for fact in gen.facts:
            assert "OCR" in fact.note
            page = fact.locator["page"]
            assert 1 <= page <= scanned.page_count
            x0, y0, x1, y1 = fact.locator["bbox"]
            assert 0 <= x0 < x1 <= 612 and 0 <= y0 < y1 <= 792
            phrase = _phrase(fact.note)
            assert _norm(phrase) in _norm(page_text(digital_doc[page - 1]))
        assert required_fact_ids(StoryView(riverside), "ws_pike") <= {f.fact_id for f in gen.facts}
    finally:
        close(scanned)
        close(digital_doc)


def test_render_pdf_rejects_non_latin1(riverside: Story) -> None:
    spec, _ = render_digital(riverside, "ws_darnell")
    spec.blocks.append(Para("snowman ☃"))
    with pytest.raises(UnicodeEncodeError):
        render_pdf(spec)
