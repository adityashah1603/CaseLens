"""Retrieval quality on the ingested SYNTHETIC demo case in the dev DB (read-only).

Opt-in: CASELENS_HEAVY=1 and CASELENS_DATABASE_URL pointing at the dev DB that holds
"The Riverside Lot Homicide". Loads the real embedder and reranker. Gold targets are copied from
data_gen/output/riverside_lot/answer_key.json (evidence file + locator). A retrieved chunk hits a
target when it is in the same file and its locator covers the target: same PDF page with an
overlapping bbox, an overlapping audio/video time span, or the same table row.

Run as a script for the comparison table:
    python packages/retrieval/tests/test_devdb_quality.py
"""

from __future__ import annotations

import os
import statistics
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from caselens_retrieval import SearchTimings, hybrid_search, search_video

DEMO_CASE = uuid.UUID("aa40579f-e535-44c0-b3a5-f2739ff5e1b9")
DEMO_LEAD = "3dd4f456-33fc-471f-a7e6-d9c73a0c362c"
K = 8

# (file, locator) pairs. pdf: (page, bbox); audio/video: (start_ms, end_ms); record: (table, row)
Target = tuple[str, dict[str, Any]]


def pdf(file: str, page: int, bbox: list[float]) -> Target:
    return file, {"kind": "pdf", "page": page, "bbox": bbox}


def span(file: str, kind: str, start: int, end: int) -> Target:
    return file, {"kind": kind, "start_ms": start, "end_ms": end}


def row(file: str, table: str, row_id: int) -> Target:
    return file, {"kind": "record", "table": table, "row_id": row_id}


PLATES = "plate_reader_hits.csv"
CALLS = "call_detail_records.csv"

# Queries derived from the story; targets from the answer key facts named in the comment.
QUERIES: list[tuple[str, list[Target]]] = [
    (  # ev_22
        "gray sedan leaves the lot with headlights off",
        [
            pdf("rpt_supp_01.pdf", 2, [342.1, 118.0, 545.9, 133.7]),
            pdf("ws_ruiz.pdf", 1, [55.8, 376.5, 556.2, 405.7]),
            pdf("ws_pike.pdf", 2, [296.5, 67.2, 446.5, 88.7]),
        ],
    ),
    (  # ev_23
        "who called 911",
        [
            pdf("rpt_incident.pdf", 1, [55.8, 295.0, 556.2, 324.2]),
            pdf("ws_ruiz.pdf", 2, [55.8, 87.0, 414.3, 102.7]),
            span("aud_911.wav", "audio", 7324, 11762),
        ],
    ),
    (  # ev_02, ev_03, ev_07 (the CSV rows carry numbers only, not names)
        "Kemper phone calls Hale",
        [
            pdf("rpt_supp_02.pdf", 2, [55.8, 146.0, 556.2, 188.7]),
            span("aud_interview.wav", "audio", 64440, 77974),
        ],
    ),
    (  # ev_18 and the SYN4821 plate reads
        "plate SYN4821 sightings",
        [
            pdf("rpt_supp_02.pdf", 1, [55.8, 390.0, 556.2, 419.2]),
            row(PLATES, "plate_reads", 246),
            row(PLATES, "plate_reads", 250),
            row(PLATES, "plate_reads", 254),
            row(PLATES, "plate_reads", 255),
            row(PLATES, "plate_reads", 256),
        ],
    ),
    (  # cf_01
        "witness says she heard a bang around 10:15",
        [
            pdf("ws_whitfield.pdf", 1, [55.8, 331.0, 343.2, 346.7]),
            pdf("rpt_supp_01.pdf", 2, [354.2, 232.0, 556.2, 247.7]),
        ],
    ),
    (  # ev_09, ev_10, ev_11
        "blue Camry argument trailer hitch",
        [
            pdf("ws_darnell.pdf", 1, [238.9, 317.5, 429.0, 365.2]),
            pdf("rpt_supp_01.pdf", 1, [125.6, 600.4, 325.5, 629.1]),
        ],
    ),
    (  # p_associate, cf_03, ev_19
        "Who was in the gray sedan on March 14?",
        [
            pdf("rpt_supp_03.pdf", 1, [176.6, 225.0, 335.7, 240.7]),
            pdf("rpt_supp_03.pdf", 2, [55.8, 208.5, 556.2, 237.7]),
            span("aud_interview.wav", "audio", 229125, 231214),
            span("vid_02_market_lot_shooting.mp4", "video", 18000, 27000),
        ],
    ),
    (  # ev_20
        "single gunshot muzzle flash at the pickup tailgate",
        [
            pdf("rpt_supp_01.pdf", 1, [125.6, 675.4, 283.5, 689.1]),
            pdf("ws_ruiz.pdf", 1, [55.8, 376.5, 215.9, 392.2]),
            pdf("ws_pike.pdf", 1, [58.0, 446.5, 171.5, 469.3]),
            pdf("rpt_incident.pdf", 1, [170.5, 572.5, 277.0, 588.1]),
        ],
    ),
    (  # ev_15
        "Darnell drove straight home east on Route 9",
        [
            pdf("ws_darnell.pdf", 1, [55.8, 408.5, 240.3, 424.2]),
            pdf("rpt_supp_02.pdf", 1, [55.8, 435.5, 556.2, 464.7]),
        ],
    ),
    (  # ev_16
        "passenger paid cash for energy drinks at the fuel stop",
        [pdf("rpt_supp_03.pdf", 1, [266.1, 238.5, 492.7, 254.2])],
    ),
    (  # ev_25
        "Civic enters the Larkspur Court garage",
        [
            pdf("rpt_supp_03.pdf", 1, [55.8, 307.0, 556.2, 336.2]),
            pdf("rpt_supp_02.pdf", 1, [164.1, 403.5, 379.1, 419.2]),
            span("vid_06_larkspur_garage.mp4", "video", 4000, 15000),
        ],
    ),
    (  # ev_29
        "search warrant served at Kemper's apartment",
        [pdf("rpt_supp_03.pdf", 1, [127.3, 375.5, 492.3, 391.2])],
    ),
    (  # cf_02 / ev_30
        "Kemper says he was alone and never got out of the car",
        [
            span("aud_interview.wav", "audio", 157214, 160430),
            pdf("rpt_supp_03.pdf", 2, [90.3, 176.5, 229.8, 192.2]),
        ],
    ),
    (  # ev_19
        "two men get out of the gray sedan",
        [
            span("vid_02_market_lot_shooting.mp4", "video", 18000, 27000),
            pdf("rpt_supp_03.pdf", 2, [55.8, 195.0, 556.2, 224.2]),
            pdf("ws_ruiz.pdf", 1, [55.8, 331.0, 139.4, 346.7]),
        ],
    ),
    (  # ev_24
        "when did the first officer arrive on scene",
        [pdf("rpt_incident.pdf", 1, [55.8, 340.5, 226.9, 356.2])],
    ),
    (  # ev_21
        "Darnell's call to Hale at 22:44 rang out unanswered",
        [
            pdf("rpt_supp_02.pdf", 2, [232.8, 250.5, 406.1, 266.2]),
            pdf("ws_darnell.pdf", 1, [55.8, 440.5, 343.7, 456.2]),
            row(CALLS, "call_records", 468),
        ],
    ),
    (  # loc_riverside_lot / ev_23 in the 911 call
        "caller reports a man has been shot at the Riverside Lot",
        [
            span("aud_911.wav", "audio", 7324, 11762),
            pdf("rpt_incident.pdf", 1, [55.8, 281.5, 556.2, 310.7]),
        ],
    ),
]


# search_video queries: (query, target video file, gold span in ms) from the answer key.
VIDEO_QUERIES: list[tuple[str, Target]] = [
    (
        "gray sedan enters the lot and parks",
        span("vid_02_market_lot_shooting.mp4", "video", 2000, 14000),
    ),
    (
        "white pickup truck enters the parking lot",
        span("vid_01_market_lot_early.mp4", "video", 4167, 10000),
    ),
    ("gray sedan pulls in at the fuel stop", span("vid_04_fuel_stop.mp4", "video", 1500, 12000)),
    (
        "person leaves on foot through the garage gate",
        span("vid_06_larkspur_garage.mp4", "video", 4000, 15000),
    ),
    (
        "person steps out of the market front door",
        span("vid_03_market_door.mp4", "video", 4400, 9000),
    ),
]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def hits(file: str, locator: dict[str, Any], target: Target) -> bool:
    t_file, t = target
    if file != t_file or locator["kind"] != t["kind"]:
        return False
    if t["kind"] == "pdf":
        bb = locator.get("bbox")
        if locator["page"] != t["page"] or bb is None:
            return locator["page"] == t["page"] and bb is None
        return _overlap(bb[0], bb[2], t["bbox"][0], t["bbox"][2]) and _overlap(
            bb[1], bb[3], t["bbox"][1], t["bbox"][3]
        )
    if t["kind"] == "record":
        return bool(locator["table"] == t["table"] and locator["row_id"] == t["row_id"])
    end = locator.get("end_ms") or locator["start_ms"]
    return _overlap(locator["start_ms"], end + 1, t["start_ms"], t["end_ms"])


@dataclass
class Report:
    recall: list[float] = field(default_factory=list)
    rr: list[float] = field(default_factory=list)
    csv_share: list[float] = field(default_factory=list)
    latency: list[SearchTimings] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        def p(attr: str) -> float:
            return statistics.median(getattr(t, attr) for t in self.latency)

        return {
            "recall@8": round(statistics.mean(self.recall), 3),
            "mrr": round(statistics.mean(self.rr), 3),
            "csv_share_top8": round(statistics.mean(self.csv_share), 3),
            "embed_ms_p50": round(p("embed_ms"), 1),
            "sql_ms_p50": round(p("sql_ms"), 1),
            "rerank_ms_p50": round(p("rerank_ms"), 1),
            "total_ms_max": round(
                max(t.embed_ms + t.sql_ms + t.rerank_ms for t in self.latency), 1
            ),
        }


def evaluate(conn: psycopg.Connection[Any], **search_kwargs: Any) -> Report:
    files = dict(
        conn.execute(
            "SELECT id, original_filename FROM public.evidence WHERE case_id = %s", (DEMO_CASE,)
        ).fetchall()
    )
    report = Report()
    for query, targets in QUERIES:
        t = SearchTimings()
        results = hybrid_search(conn, DEMO_CASE, query, None, K, timings=t, **search_kwargs)
        found: set[int] = set()
        first: int | None = None
        for rank, r in enumerate(results, start=1):
            c = r.candidate
            for i, target in enumerate(targets):
                if hits(files[c.evidence_id], c.locator, target):
                    found.add(i)
                    first = first or rank
        report.recall.append(len(found) / min(len(targets), K))
        report.rr.append(1.0 / first if first else 0.0)
        report.csv_share.append(sum(r.candidate.modality == "csv" for r in results) / K)
        report.latency.append(t)
        missed = [targets[i][0] for i in range(len(targets)) if i not in found]
        if missed:
            report.misses.append(f"{query!r}: missed {missed}")
    return report


def evaluate_video(conn: psycopg.Connection[Any], **search_kwargs: Any) -> tuple[float, float]:
    """(hit@8, MRR) of search_video over VIDEO_QUERIES."""
    files = dict(
        conn.execute(
            "SELECT id, original_filename FROM public.evidence WHERE case_id = %s", (DEMO_CASE,)
        ).fetchall()
    )
    hit, rr = 0, 0.0
    for query, target in VIDEO_QUERIES:
        out = search_video(conn, DEMO_CASE, query, top_k=K, **search_kwargs)
        ranks = [
            i
            for i, r in enumerate(out.results, start=1)
            if hits(files[r.evidence_id], r.locator.model_dump(mode="json"), target)
        ]
        hit += bool(ranks)
        rr += 1.0 / ranks[0] if ranks else 0.0
    return hit / len(VIDEO_QUERIES), rr / len(VIDEO_QUERIES)


def _dev_conn() -> psycopg.Connection[Any]:
    url = os.environ.get("CASELENS_DATABASE_URL", "")
    scheme, _, rest = url.partition("://")
    conn = psycopg.connect(f"{scheme.split('+', 1)[0]}://{rest}", autocommit=True)
    conn.execute("SET ROLE caselens_app")
    conn.execute("SELECT set_config('app.user_id', %s, false)", (DEMO_LEAD,))
    conn.execute("SET default_transaction_read_only = on")
    return conn


heavy = pytest.mark.skipif(
    os.environ.get("CASELENS_HEAVY") != "1" or not os.environ.get("CASELENS_DATABASE_URL"),
    reason="set CASELENS_HEAVY=1 and CASELENS_DATABASE_URL (dev DB with the demo case)",
)


@heavy
def test_demo_case_recall_at_8() -> None:
    conn = _dev_conn()
    try:
        hybrid_search(conn, DEMO_CASE, "warm up the models", None, 1)
        report = evaluate(conn)
        video_hit, video_mrr = evaluate_video(conn)
    finally:
        conn.close()
    print(report.summary(), f"video hit@8={video_hit:.2f} mrr={video_mrr:.2f}", *report.misses)
    assert report.summary()["recall@8"] >= 0.8
    assert video_hit >= 0.8


def main() -> None:
    """Compare query prefixes x rerankers (RERANKERS=a,b env; "none" = RRF only)."""
    from unittest.mock import patch

    from caselens_retrieval.encoders import _cross_encoder, _text_embedding, fastembed_rerank
    from caselens_retrieval.settings import get_settings

    s = get_settings()

    def embedder(prefix: str) -> Callable[[str], list[float]]:
        def embed(query: str) -> list[float]:
            engine = _text_embedding(s.embedding_model, str(s.model_cache_dir), s.model_threads)
            return [float(x) for x in next(iter(engine.embed([prefix + query])))]

        return embed

    def reranker(name: str) -> Callable[[str, Any], list[float]]:
        def rank(query: str, docs: Any) -> list[float]:
            _cross_encoder(name, str(s.model_cache_dir), s.model_threads)
            with patch.object(s, "rerank_model", name):
                return fastembed_rerank(query, docs)

        return rank

    prefixes = {
        "plain": "",
        "bge-instr": "Represent this sentence for searching relevant passages: ",
    }
    names = os.environ.get("RERANKERS", f"none,{s.rerank_model}").split(",")
    wanted = os.environ.get("PREFIXES", ",".join(prefixes)).split(",")
    for pname, prefix in ((p, prefixes[p]) for p in wanted):
        for name in names:
            kwargs: dict[str, Any] = {"embedder": embedder(prefix)}
            kwargs.update({"rerank": False} if name == "none" else {"reranker": reranker(name)})
            conn = _dev_conn()
            hybrid_search(conn, DEMO_CASE, "warm up the models", None, 1, **kwargs)
            report = evaluate(conn, **kwargs)
            video = evaluate_video(conn, **kwargs)
            conn.close()
            label = f"{pname} / {name} (window {s.rerank_window_words}, pool {s.rerank_candidates})"
            print(label, report.summary(), "video hit@8 / mrr", video)
            for miss in report.misses:
                print("   ", miss)


if __name__ == "__main__":
    main()
