"""SYNTHETIC PDF evidence: incident / supplemental reports and witness statements.

``generate(story, case_dir)`` writes one PDF per ``pdf`` item in the story's evidence plan into
``case_dir/pdf/<plan id>.pdf`` and returns the answer-key facts for each. Fact locators are found by
searching the rendered digital PDF for each fact's key phrase. ``witness_statement_scanned`` items
are rasterised and degraded into an image-only PDF (no text layer); their bboxes are carried from
the digital layout through the scan skew and are approximate.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.documents import harbor, riverside
from caselens_data_gen.documents.facts import StoryView
from caselens_data_gen.documents.layout import DocSpec, locate_phrase, render_pdf
from caselens_data_gen.documents.pdfio import close, open_pdf
from caselens_data_gen.documents.scan import map_bbox, scan_pdf
from caselens_data_gen.story import Story

Box = tuple[float, float, float, float]

BUILDERS: dict[str, Callable[[StoryView], DocSpec]] = {**riverside.BUILDERS, **harbor.BUILDERS}


def required_fact_ids(sv: StoryView, plan_id: str) -> set[str]:
    """Events, entities and conflicts the evidence plan says this document covers."""
    plan = sv.plan_item(plan_id)
    return set(plan.covers_events) | set(plan.covers_entities) | set(sv.conflicts_for(plan_id))


def build_spec(story: Story, plan_id: str) -> DocSpec:
    sv = StoryView(story)
    if plan_id not in BUILDERS:
        raise KeyError(f"no document writer for evidence plan item {plan_id!r}")
    spec = BUILDERS[plan_id](sv)
    missing = required_fact_ids(sv, plan_id) - {fact_id for fact_id, _ in spec.facts}
    if missing:
        raise ValueError(f"{plan_id}: no key phrase for covered fact(s) {sorted(missing)}")
    return spec


def render_digital(story: Story, plan_id: str) -> tuple[DocSpec, bytes]:
    """The digital (text-layer) rendition of one plan item, before any scan simulation."""
    spec = build_spec(story, plan_id)
    return spec, render_pdf(spec)


def _locate_all(spec: DocSpec, data: bytes) -> list[tuple[str, str, int, Box]]:
    doc = open_pdf(data)
    try:
        found: list[tuple[str, str, int, Box]] = []
        for fact_id, phrase in spec.facts:
            page, bbox = locate_phrase(doc, phrase)
            found.append((fact_id, phrase, page, bbox))
        return found
    finally:
        close(doc)


def generate(story: Story, case_dir: Path) -> list[GeneratedFile]:
    out_dir = case_dir / "pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[GeneratedFile] = []
    for plan in story.evidence_plan:
        if plan.type != "pdf":
            continue
        spec, digital = render_digital(story, plan.id)
        located = _locate_all(spec, digital)
        facts: list[FactLocation] = []
        path = out_dir / f"{plan.id}.pdf"
        if plan.kind == "witness_statement_scanned":
            scan = scan_pdf(digital, seed_key=f"{story.case_slug}/{plan.id}")
            path.write_bytes(scan.pdf)
            for fact_id, phrase, page, bbox in located:
                x0, y0, x1, y1 = bbox
                mapped = map_bbox(
                    (x0, y0, x1, y1), scan.angles[page - 1], scan.page_sizes[page - 1]
                )
                facts.append(
                    FactLocation(
                        fact_id=fact_id,
                        locator={"kind": "pdf", "page": page, "bbox": list(mapped)},
                        note=(
                            f'key phrase: "{phrase}"; scanned image-only page (no text layer, '
                            f"OCR required); bbox mapped from the pre-scan layout through a "
                            f"{scan.angles[page - 1]:+.1f} deg skew, approximate"
                        ),
                    )
                )
            description = f"{plan.title} - scanned, image-only (SYNTHETIC)"
        else:
            path.write_bytes(digital)
            for fact_id, phrase, page, bbox in located:
                facts.append(
                    FactLocation(
                        fact_id=fact_id,
                        locator={"kind": "pdf", "page": page, "bbox": list(bbox)},
                        note=f'key phrase: "{phrase}"',
                    )
                )
            description = f"{plan.title} (SYNTHETIC)"
        produced.append(
            GeneratedFile(
                path=path,
                evidence_type="pdf",
                description=description,
                facts=facts,
                recorded_at=plan.recorded_at.isoformat(),
            )
        )
    return produced


__all__ = ["BUILDERS", "build_spec", "generate", "render_digital", "required_fact_ids"]
