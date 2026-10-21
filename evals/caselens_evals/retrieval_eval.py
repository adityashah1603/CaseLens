"""Retrieval recall@10 per modality, before the agent (no LLM).

For every answered gold item: `caselens_retrieval.search_evidence(question, top_k=10)` (plus
`search_video(question, top_k=10)` when a required citation is video) as the seed lead on the
`caselens_app` connection, then check which required citations a result hits under the shared
`gold.match_citation` rule. Written to `retrieval.json` in the results dir.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from caselens_evals.gold import GoldItem, match_citation
from caselens_evals.metrics import RetrievalRecall

RETRIEVAL_FILE = "retrieval.json"
TOP_K = 10

# (question, want_video) -> list of (evidence_id, locator) results
Searcher = Callable[[str, bool], list[tuple[str, dict[str, Any]]]]


class CitationHit(BaseModel):
    fact_id: str
    modality: str
    hit: bool


class ItemRetrieval(BaseModel):
    gold_id: str
    citations: list[CitationHit]


class RetrievalReport(BaseModel):
    k: int = TOP_K
    items: list[ItemRetrieval]

    @property
    def recall(self) -> RetrievalRecall:
        recall = RetrievalRecall(k=self.k, items=len(self.items))
        for item in self.items:
            for c in item.citations:
                recall.totals[c.modality] = recall.totals.get(c.modality, 0) + 1
                recall.hits[c.modality] = recall.hits.get(c.modality, 0) + int(c.hit)
        return recall

    def merged(self, other: RetrievalReport) -> RetrievalReport:
        """`other`'s items replace this report's items with the same gold id."""
        by_id = {i.gold_id: i for i in self.items}
        by_id.update({i.gold_id: i for i in other.items})
        return RetrievalReport(k=self.k, items=[by_id[k] for k in sorted(by_id)])


def evaluate(
    items: Sequence[GoldItem],
    search: Searcher,
    filenames: Mapping[str, str],
    k: int = TOP_K,
) -> RetrievalReport:
    per_item: list[ItemRetrieval] = []
    for item in items:
        if item.expected_status != "answered" or not item.required_citations:
            continue
        want_video = any(c.modality == "video" for c in item.required_citations)
        results = search(item.question, want_video)
        hits = [
            CitationHit(
                fact_id=req.fact_id,
                modality=req.modality,
                hit=any(
                    match_citation(filenames[ev], loc, req)
                    for ev, loc in results
                    if ev in filenames
                ),
            )
            for req in item.required_citations
        ]
        per_item.append(ItemRetrieval(gold_id=item.id, citations=hits))
    return RetrievalReport(k=k, items=per_item)


def live_searcher(conn: Any, case_id: uuid.UUID, k: int = TOP_K) -> Searcher:
    import caselens_retrieval as r

    def search(question: str, want_video: bool) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = [
            (str(res.evidence_id), res.locator.model_dump(mode="json"))
            for res in r.search_evidence(conn, case_id, question, top_k=k).results
        ]
        if want_video:
            out += [
                (str(res.evidence_id), res.locator.model_dump(mode="json"))
                for res in r.search_video(conn, case_id, question, top_k=k).results
            ]
        return out

    return search


def case_filenames(conn: Any, case_id: uuid.UUID) -> dict[str, str]:
    rows = conn.execute(
        "SELECT id::text, original_filename FROM public.evidence WHERE case_id = %s", (case_id,)
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def save(report: RetrievalReport, out_dir: Path) -> None:
    (out_dir / RETRIEVAL_FILE).write_text(report.model_dump_json(indent=2), encoding="utf-8")


def load(out_dir: Path) -> RetrievalReport | None:
    path = out_dir / RETRIEVAL_FILE
    if not path.exists():
        return None
    return RetrievalReport.model_validate_json(path.read_text(encoding="utf-8"))
