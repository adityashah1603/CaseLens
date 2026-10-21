"""Answer-quality metrics from stored runs, judge verdicts and the retrieval eval.

Deterministic where possible:
* citation recall: required gold citations matched by any run citation (`gold.match_citation`);
* unanswerable refusal: the answer status;
* SQL accuracy: a `query_records` call with the gold operation whose executed result matches
  `sql.expected` (row_count, or every expected row found in the result);
* latency p50 / p95 and cost per question: from the runs (cost from `agent_runs.cost_usd`);
* retrieval recall@10: computed before the agent by `retrieval_eval`.
From the judge: answer correctness (correct 1, partial 0.5, wrong 0), citation precision
(supported claim-citation pairs / all pairs), hallucination rate (claims with no supporting
citation / all claims) and conflict detection (answer.conflicts non-empty AND the judge agrees it
is the planted conflict).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from caselens_evals.gold import MODALITY_ORDER, GoldItem, SqlSpec, match_citation
from caselens_evals.judge import CORRECTNESS_SCORE, JudgeResult
from caselens_evals.runner import CATEGORIES, RunRecord, SqlResult

PRECISION_GATE = 0.95
FREE_TIER = "n/a (free tier)"


class Metric(BaseModel):
    key: str
    name: str
    target: str
    value: float | None
    display: str
    passed: bool | None  # None: not measurable on this run
    n: int
    note: str = ""


class CategoryRow(BaseModel):
    category: str
    n: int
    correctness: float | None
    citation_recall: float | None
    citation_precision: float | None
    hallucination: float | None


class Scorecard(BaseModel):
    metrics: list[Metric]
    categories: list[CategoryRow]
    models: list[str]
    judge_models: list[str]
    date: str
    runs: int
    failed_runs: int
    judged: int
    judge_calls: int = 0
    notes: list[str] = Field(default_factory=list)

    def metric(self, key: str) -> Metric:
        return next(m for m in self.metrics if m.key == key)

    @property
    def gate_passed(self) -> bool:
        """The hard CI gate: citation precision >= 95% (when measurable)."""
        return self.metric("citation_precision").passed is not False


def _ratio(num: float, den: int) -> float | None:
    return num / den if den else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


# --- per-item measurements ------------------------------------------------------------------


def recall_counts(item: GoldItem, run: RunRecord) -> tuple[int, int]:
    """(matched required citations, required citations)."""
    citations = (run.answer or {}).get("citations", [])
    hit = 0
    for req in item.required_citations:
        for cit in citations:
            name = run.evidence_files.get(str(cit.get("evidence_id")))
            if name and match_citation(name, cit.get("locator", {}), req):
                hit += 1
                break
    return hit, len(item.required_citations)


def precision_counts(run: RunRecord, verdict: JudgeResult) -> tuple[int, int, int, int]:
    """(supported pairs, pairs, claims with no supporting citation, claims). Pairs the judge
    left out count as unsupported."""
    claims = (run.answer or {}).get("claims", [])
    judged: dict[tuple[str, str], bool] = {}
    if verdict.output is not None:
        for cv in verdict.output.claims:
            for c in cv.citations:
                judged[(cv.claim_id, c.citation_id)] = c.supported
    supported = pairs = unsupported_claims = 0
    for claim in claims:
        ok = [judged.get((claim["id"], cid), False) for cid in claim.get("citation_ids", [])]
        pairs += len(ok)
        supported += sum(ok)
        unsupported_claims += not any(ok)
    return supported, pairs, unsupported_claims, len(claims)


def _norm(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        if ts.tzinfo is not None:
            return ts.timestamp()
    try:
        return float(text)
    except ValueError:
        return text.casefold()


def row_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(k in actual and _norm(actual[k]) == _norm(v) for k, v in expected.items())


def sql_result_matches(spec: SqlSpec, result: SqlResult) -> bool:
    if result.operation != spec.operation:
        return False
    exp = spec.expected
    if exp.row_count is not None and result.row_count != exp.row_count:
        return False
    if exp.rows_match is not None:
        if result.truncated and len(result.rows) < result.row_count:
            return False
        return all(any(row_matches(e, a) for a in result.rows) for e in exp.rows_match)
    return True


def sql_correct(item: GoldItem, run: RunRecord) -> bool:
    if item.sql is None:
        return False
    if run.sql_results:
        return any(sql_result_matches(item.sql, r) for r in run.sql_results)
    # Older runs without recorded results: fall back to the stored tool calls (row counts only).
    exp = item.sql.expected
    if exp.rows_match is not None or exp.row_count is None:
        return False
    return any(
        tc.get("tool") == "query_records"
        and tc.get("ok")
        and (tc.get("input") or {}).get("operation") == item.sql.operation
        and tc.get("result_count") == exp.row_count
        for tc in run.tool_calls
    )


def refused(run: RunRecord) -> bool:
    return (run.answer or {}).get("status") == "insufficient_evidence"


def conflict_detected(run: RunRecord, verdict: JudgeResult | None) -> bool:
    surfaced = (
        verdict is not None
        and verdict.output is not None
        and verdict.output.planted_conflict_surfaced is True
    )
    return bool((run.answer or {}).get("conflicts")) and surfaced


# --- aggregate ------------------------------------------------------------------------------


class RetrievalRecall(BaseModel):
    """Per-modality recall@k of the required citations, before the agent."""

    k: int = 10
    hits: dict[str, int] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=dict)
    items: int = 0

    def recall(self, modality: str) -> float | None:
        return _ratio(self.hits.get(modality, 0), self.totals.get(modality, 0))


def _quality(
    items: Sequence[GoldItem], runs: Mapping[str, RunRecord], verdicts: Mapping[str, JudgeResult]
) -> dict[str, Any]:
    corr: list[float] = []
    rec_hit = rec_total = sup = pairs = uncl = claims = 0
    for item in items:
        run = runs.get(item.id)
        if run is None:
            continue
        v = verdicts.get(item.id)
        if v is not None and v.output is not None:
            corr.append(CORRECTNESS_SCORE[v.output.correctness])
            s, p, u, c = precision_counts(run, v)
            sup, pairs, uncl, claims = sup + s, pairs + p, uncl + u, claims + c
        if item.expected_status == "answered":
            h, t = recall_counts(item, run)
            rec_hit, rec_total = rec_hit + h, rec_total + t
    return {
        "correctness": _ratio(sum(corr), len(corr)),
        "judged": len(corr),
        "recall": _ratio(rec_hit, rec_total),
        "recall_n": rec_total,
        "precision": _ratio(sup, pairs),
        "pairs": pairs,
        "hallucination": _ratio(uncl, claims),
        "claims": claims,
    }


def score(
    items: Sequence[GoldItem],
    runs: Mapping[str, RunRecord],
    verdicts: Mapping[str, JudgeResult],
    retrieval: RetrievalRecall | None = None,
    date: str = "",
    judge_calls: int = 0,
) -> Scorecard:
    scored = [i for i in items if i.id in runs]
    q = _quality(scored, runs, verdicts)
    notes: list[str] = []

    def m(
        key: str, name: str, target: str, value: float | None, passed: bool | None, n: int
    ) -> Metric:
        return Metric(
            key=key, name=name, target=target, value=value, display=_pct(value), passed=passed, n=n
        )

    def at_least(v: float | None, t: float) -> bool | None:
        return None if v is None else v >= t - 1e-9

    def at_most(v: float | None, t: float) -> bool | None:
        return None if v is None else v <= t + 1e-9

    metrics = [
        m("correctness", "Answer correctness", ">= 85%", q["correctness"],
          at_least(q["correctness"], 0.85), q["judged"]),
        m("citation_precision", "Citation precision", ">= 95%", q["precision"],
          at_least(q["precision"], PRECISION_GATE), q["pairs"]),
        m("citation_recall", "Citation recall", ">= 80%", q["recall"],
          at_least(q["recall"], 0.80), q["recall_n"]),
        m("hallucination", "Hallucination rate", "<= 2%", q["hallucination"],
          at_most(q["hallucination"], 0.02), q["claims"]),
    ]  # fmt: skip

    unans = [i for i in scored if i.category == "unanswerable"]
    v_ref = _ratio(sum(refused(runs[i.id]) for i in unans), len(unans))
    metrics.append(
        m("refusal", "Unanswerable refusal", "100%", v_ref, at_least(v_ref, 1.0), len(unans))
    )

    conf = [i for i in scored if i.category == "conflict"]
    v_conf = _ratio(sum(conflict_detected(runs[i.id], verdicts.get(i.id)) for i in conf), len(conf))
    metrics.append(
        m("conflict", "Conflict detection", ">= 80%", v_conf, at_least(v_conf, 0.80), len(conf))
    )

    if retrieval is not None:
        per = {mod: retrieval.recall(mod) for mod in MODALITY_ORDER if retrieval.totals.get(mod)}
        worst = min((v for v in per.values() if v is not None), default=None)
        hit, tot = sum(retrieval.hits.values()), sum(retrieval.totals.values())
        rr = m(
            "retrieval_recall", f"Retrieval recall@{retrieval.k}", ">= 90% per modality",
            _ratio(hit, tot), at_least(worst, 0.90), tot,
        )  # fmt: skip
        rr.display = ", ".join(f"{mod} {_pct(v)}" for mod, v in per.items()) or "n/a"
        metrics.append(rr)
    else:
        metrics.append(
            m("retrieval_recall", "Retrieval recall@10", ">= 90% per modality", None, None, 0)
        )
        notes.append("Retrieval recall@10 was not measured for this results dir.")

    structured = [i for i in scored if i.sql is not None]
    v_sql = _ratio(sum(sql_correct(i, runs[i.id]) for i in structured), len(structured))
    metrics.append(
        m("sql", "SQL accuracy", ">= 90%", v_sql, at_least(v_sql, 0.90), len(structured))
    )

    lat = [ms / 1000 for i in scored if (ms := runs[i.id].latency_ms) is not None]
    p50, p95 = percentile(lat, 50), percentile(lat, 95)
    lat_m = Metric(
        key="latency",
        name="p50 / p95 latency",
        target="< 8s / < 20s",
        value=p50,
        display="n/a" if p50 is None or p95 is None else f"{p50:.1f}s / {p95:.1f}s",
        passed=None if p50 is None or p95 is None else (p50 < 8 and p95 < 20),
        n=len(lat),
    )
    metrics.append(lat_m)

    costs = [runs[i.id].cost_usd for i in scored]
    known = [c for c in costs if c is not None]
    if known:
        mean_cost = sum(known) / len(known)
        cost_m = Metric(
            key="cost", name="Cost per question", target="< $0.05", value=mean_cost,
            display=f"${mean_cost:.4f}" + (" (free tier)" if mean_cost == 0 else ""),
            passed=mean_cost < 0.05, n=len(known),
        )  # fmt: skip
        if len(known) < len(costs):
            cost_m.note = f"{len(costs) - len(known)} runs without cost_usd"
    else:
        cost_m = Metric(
            key="cost", name="Cost per question", target="< $0.05", value=None,
            display=FREE_TIER, passed=None, n=0,
        )  # fmt: skip
        notes.append(
            "Cost: agent_runs.cost_usd is null (free Gemini tier, no billing), so cost per "
            "question is reported as n/a."
        )
    metrics.append(cost_m)

    categories = []
    for cat in CATEGORIES:
        in_cat = [i for i in scored if i.category == cat]
        if not in_cat:
            continue
        cq = _quality(in_cat, runs, verdicts)
        categories.append(
            CategoryRow(
                category=cat,
                n=len(in_cat),
                correctness=cq["correctness"],
                citation_recall=cq["recall"],
                citation_precision=cq["precision"],
                hallucination=cq["hallucination"],
            )
        )

    failed = sum(runs[i.id].model_failed for i in scored)
    if failed:
        notes.append(
            f"{failed} run(s) failed (model error or quota); they are scored as they stand."
        )
    unjudged = sum(1 for i in scored if not (verdicts.get(i.id) and verdicts[i.id].usable))
    if unjudged:
        notes.append(f"{unjudged} run(s) have no usable judge verdict yet (rerun `score`).")
    return Scorecard(
        metrics=metrics,
        categories=categories,
        models=sorted({r.model for i in scored if (r := runs[i.id]).model}),
        judge_models=sorted({v.judge_model for v in verdicts.values() if v.judge_model}),
        date=date,
        runs=len(scored),
        failed_runs=failed,
        judged=q["judged"],
        judge_calls=judge_calls,
        notes=notes,
    )
