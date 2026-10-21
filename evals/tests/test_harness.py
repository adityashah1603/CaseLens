"""Eval harness tests on a SYNTHETIC fixture gold set, hand-made runs and a fake judge model."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from caselens_agent.router import ModelRouter, RouterSettings, Task
from caselens_evals import cli, judge, metrics, report, retrieval_eval, runner
from caselens_evals.gold import GoldItem, load_gold
from caselens_evals.runner import RunRecord, SourceText, SqlResult

FIXTURE = Path(__file__).parent / "fixtures" / "gold_small.jsonl"
EV_RUIZ = str(uuid.uuid4())
EV_VID = str(uuid.uuid4())
EV_CSV = str(uuid.uuid4())
FILES = {EV_RUIZ: "ws_ruiz.pdf", EV_VID: "vid_02_market_lot_shooting.mp4", EV_CSV: "cdr.csv"}


@pytest.fixture
def gold() -> dict[str, GoldItem]:
    return {i.id: i for i in load_gold(FIXTURE, check=False)}


def cite(cid: str, ev: str, locator: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "source_type": "chunk",
        "evidence_id": ev,
        "chunk_id": str(uuid.uuid4()),
        "locator": locator,
        "quote": None,
    }


def make_run(
    item: GoldItem,
    citations: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    status: str = "answered",
    conflicts: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> RunRecord:
    citations = citations or []
    claims = (
        claims
        if claims is not None
        else (
            [{"id": "cl1", "text": "SYNTHETIC claim", "citation_ids": [c["id"] for c in citations]}]
            if citations
            else []
        )
    )
    answer = {
        "status": status,
        "answer": "SYNTHETIC answer",
        "claims": claims,
        "citations": citations,
        "conflicts": conflicts or [],
        "confidence": 0.8,
        "open_questions": [],
    }
    base: dict[str, Any] = {
        "gold_id": item.id,
        "category": item.category,
        "question": item.question,
        "run_id": str(uuid.uuid4()),
        "status": "succeeded",
        "answer": answer,
        "latency_ms": 5000,
        "model": "fake:fake",
        "evidence_files": FILES,
        "sources": {c["id"]: SourceText(filename="x", kind="pdf", text="t") for c in citations},
    }
    base.update(extra)
    return RunRecord(**base)


def verdict(run: RunRecord, supported: bool = True, **kw: Any) -> judge.JudgeResult:
    claims = (run.answer or {}).get("claims", [])
    out = judge.JudgeOutput(
        correctness=kw.get("correctness", "correct"),
        rationale="ok",
        claims=[
            judge.ClaimVerdict(
                claim_id=c["id"],
                citations=[
                    judge.CitationVerdict(citation_id=cid, supported=supported)
                    for cid in c["citation_ids"]
                ],
            )
            for c in claims
        ],
        planted_conflict_surfaced=kw.get("surfaced"),
    )
    return judge.JudgeResult(run_id=str(run.run_id), gold_id=run.gold_id, output=out)


# --- deterministic metrics ------------------------------------------------------------------


def test_citation_recall_uses_shared_match_rule(gold: dict[str, GoldItem]) -> None:
    q2 = gold["q02"]
    run = make_run(
        q2,
        [
            cite("c1", EV_VID, {"kind": "video", "start_ms": 8000, "end_ms": 12000}),
            cite("c2", EV_RUIZ, {"kind": "pdf", "page": 1, "bbox": None}),  # wrong file
        ],
    )
    assert metrics.recall_counts(q2, run) == (1, 2)


def test_refusal_and_conflict_detection(gold: dict[str, GoldItem]) -> None:
    refused = make_run(gold["q05"], status="insufficient_evidence")
    answered = make_run(gold["q05"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})])
    assert metrics.refused(refused) and not metrics.refused(answered)

    conflict = {"id": "cf1", "description": "times differ", "claim_a": {}, "claim_b": {}}
    run = make_run(
        gold["q04"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})], conflicts=[conflict]
    )
    assert metrics.conflict_detected(run, verdict(run, surfaced=True))
    assert not metrics.conflict_detected(run, verdict(run, surfaced=False))
    no_conflict = make_run(gold["q04"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})])
    assert not metrics.conflict_detected(no_conflict, verdict(no_conflict, surfaced=True))


def test_sql_accuracy_row_count_rows_and_fallback(gold: dict[str, GoldItem]) -> None:
    q3 = gold["q03"]
    ok = SqlResult(operation="calls_between", params={}, row_count=7)
    wrong_op = SqlResult(operation="calls_for_phone", params={}, row_count=7)
    assert metrics.sql_correct(q3, make_run(q3, sql_results=[wrong_op, ok]))
    assert not metrics.sql_correct(q3, make_run(q3, sql_results=[wrong_op]))
    calls = [
        {
            "tool": "query_records",
            "ok": True,
            "input": {"operation": "calls_between"},
            "result_count": 7,
        }
    ]
    assert metrics.sql_correct(q3, make_run(q3, tool_calls=calls))

    spec = q3.sql
    assert spec is not None
    rows_spec = spec.model_copy(
        update={
            "expected": spec.expected.model_copy(
                update={
                    "row_count": None,
                    "rows_match": [{"started_at": "2026-03-14T05:00:00Z", "duration_s": 840}],
                }
            )
        }
    )
    actual = SqlResult(
        operation="calls_between",
        params={},
        row_count=2,
        rows=[
            {"started_at": "2026-03-13T22:00:00-07:00", "duration_s": 840},
            {"started_at": "2026-03-14T01:00:00-07:00", "duration_s": 60},
        ],
    )
    assert metrics.sql_result_matches(rows_spec, actual)
    actual.rows[0]["duration_s"] = 30
    assert not metrics.sql_result_matches(rows_spec, actual)


def test_percentile_and_cost_free_tier(gold: dict[str, GoldItem]) -> None:
    assert metrics.percentile([1, 2, 3, 4, 100], 50) == 3
    assert metrics.percentile([1, 2, 3, 4, 100], 95) == 100
    assert metrics.percentile([], 50) is None
    runs = {"q05": make_run(gold["q05"], status="insufficient_evidence")}
    card = metrics.score([gold["q05"]], runs, {})
    assert card.metric("cost").display == metrics.FREE_TIER
    assert card.metric("latency").display == "5.0s / 5.0s"
    assert card.metric("refusal").value == 1.0
    assert any("free" in n for n in card.notes)


def test_scorecard_precision_hallucination_and_categories(gold: dict[str, GoldItem]) -> None:
    q1, q5 = gold["q01"], gold["q05"]
    run1 = make_run(
        q1,
        [
            cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1, "bbox": [10, 10, 20, 20]}),
            cite("c2", EV_RUIZ, {"kind": "pdf", "page": 2}),
        ],
        claims=[
            {"id": "cl1", "text": "a", "citation_ids": ["c1"]},
            {"id": "cl2", "text": "b", "citation_ids": ["c2"]},
        ],
    )
    v1 = verdict(run1)
    assert v1.output is not None
    v1.output.claims[1].citations[0].supported = False
    run5 = make_run(q5, status="insufficient_evidence")
    card = metrics.score(
        [q1, q5],
        {"q01": run1, "q05": run5},
        {"q01": v1, "q05": judge.judge_run(q5, run5, None)},
    )
    assert card.metric("citation_precision").value == 0.5
    assert card.metric("hallucination").value == 0.5
    assert card.metric("citation_recall").value == 1.0
    assert card.metric("refusal").value == 1.0
    assert card.metric("correctness").value == 1.0
    assert not card.gate_passed
    assert [c.category for c in card.categories] == ["single_source", "unanswerable"]


# --- judge ----------------------------------------------------------------------------------


class FakeJudge:
    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.prompts: list[str] = []

    def __call__(self, task: Task, system: str, user: str, schema: type[BaseModel]) -> str:
        assert task == "judge"
        self.prompts.append(user)
        return json.dumps(
            {
                "correctness": "partial",
                "rationale": "SYNTHETIC",
                "claims": [
                    {
                        "claim_id": "cl1",
                        "citations": [{"citation_id": "c1", "supported": self.supported}],
                    }
                ],
                "planted_conflict_surfaced": None,
            }
        )

    def router(self) -> ModelRouter:
        return ModelRouter(settings=RouterSettings(llm_provider="fake"), fake=self)


def test_judge_prompt_has_evidence_blocks_and_parses(gold: dict[str, GoldItem]) -> None:
    q4 = gold["q04"]
    run = make_run(q4, [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})])
    run.sources["c1"] = SourceText(
        filename="ws_whitfield.pdf", kind="pdf", text="SYNTHETIC: around 10:15 a blue car left"
    )
    fake = FakeJudge()
    res = judge.judge_run(q4, run, fake.router())
    assert res.output is not None and res.output.correctness == "partial"
    prompt = fake.prompts[0]
    assert "<<<EVIDENCE c1 ws_whitfield.pdf" in prompt
    assert "around 10:15 a blue car left" in prompt
    assert "<<<EVIDENCE system answer" in prompt
    assert "PLANTED CONFLICT" in prompt and "22:41 gray sedan" in prompt
    assert q4.expected_answer in prompt


def test_judge_cache_and_refusal_needs_no_call(gold: dict[str, GoldItem], tmp_path: Path) -> None:
    runs = {
        "q01": make_run(gold["q01"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})]),
        "q05": make_run(gold["q05"], status="insufficient_evidence"),
    }
    fake = FakeJudge()
    results, calls = judge.judge_runs(gold, runs, tmp_path, fake.router())
    assert calls == 1 and len(fake.prompts) == 1
    assert results["q05"].skipped and results["q05"].output is not None
    assert results["q05"].output.correctness == "correct"
    again, calls2 = judge.judge_runs(gold, runs, tmp_path, fake.router())
    assert calls2 == 0 and len(fake.prompts) == 1
    assert again["q01"].output == results["q01"].output


def test_unanswerable_that_answers_is_wrong(gold: dict[str, GoldItem]) -> None:
    run = make_run(gold["q05"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})])
    res = judge.judge_run(gold["q05"], run, FakeJudge().router())
    assert res.output is not None and res.output.correctness == "wrong"


# --- runner ---------------------------------------------------------------------------------


def test_run_items_is_resumable(gold: dict[str, GoldItem], tmp_path: Path) -> None:
    executed: list[str] = []

    def execute(item: GoldItem) -> RunRecord:
        executed.append(item.id)
        status = "failed" if item.id == "q02" else "succeeded"
        return make_run(item, status="insufficient_evidence").model_copy(update={"status": status})

    items = list(gold.values())
    first = runner.run_items(items[:2], tmp_path, execute, log=lambda _: None)
    assert first.ran == ["q01", "q02"]
    second = runner.run_items(items, tmp_path, execute, log=lambda _: None)
    assert second.skipped == ["q01", "q02"] and second.ran == ["q03", "q04", "q05"]
    third = runner.run_items(items, tmp_path, execute, retry_failed=True, log=lambda _: None)
    assert third.ran == ["q02"]
    assert executed == ["q01", "q02", "q03", "q04", "q05", "q02"]
    assert set(runner.load_runs(tmp_path)) == set(gold)


def test_run_items_stops_after_repeated_model_failures(
    gold: dict[str, GoldItem], tmp_path: Path
) -> None:
    def execute(item: GoldItem) -> RunRecord:
        return make_run(item).model_copy(update={"status": "failed"})

    summary = runner.run_items(list(gold.values()), tmp_path, execute, log=lambda _: None)
    assert summary.stopped_early and summary.ran == ["q01", "q02"]


def test_selection_smoke_and_model_overrides(gold: dict[str, GoldItem]) -> None:
    items = list(gold.values())
    assert [i.id for i in runner.smoke_subset(items)] == ["q01", "q02", "q03", "q04", "q05"]
    assert [i.id for i in runner.select_items(items, category="structured")] == ["q03"]
    assert [i.id for i in runner.select_items(items, ids=["q04", "q01"], limit=1)] == ["q01"]
    with pytest.raises(ValueError):
        runner.select_items(items, ids=["q99"])
    assert runner.parse_model_overrides("plan=m1, synthesize=m2,provider=ollama") == {
        "model_plan": "m1",
        "model_synthesize": "m2",
        "llm_provider": "ollama",
    }
    with pytest.raises(ValueError):
        runner.parse_model_overrides("planner=m1")
    settings = runner.router_settings({"model_plan": "m1"})
    assert ModelRouter(settings).model_for("plan") == "m1"


# --- retrieval eval -------------------------------------------------------------------------


def test_retrieval_recall_per_modality(gold: dict[str, GoldItem]) -> None:
    seen: list[tuple[str, bool]] = []

    def search(question: str, want_video: bool) -> list[tuple[str, dict[str, Any]]]:
        seen.append((question, want_video))
        return [
            (EV_RUIZ, {"kind": "pdf", "page": 1, "bbox": [5, 5, 50, 50]}),
            (EV_VID, {"kind": "video", "start_ms": 30000, "end_ms": 31000}),  # too late
        ]

    rep = retrieval_eval.evaluate(list(gold.values()), search, FILES)
    rec = rep.recall
    assert rec.recall("pdf") == 0.5  # q01 hit, q04 (ws_whitfield) missed
    assert rec.recall("video") == 0.0
    assert rec.totals == {"pdf": 2, "video": 1, "audio": 1, "csv": 1}
    assert [w for _, w in seen] == [False, True, False, False]  # unanswerable skipped
    merged = rep.merged(retrieval_eval.RetrievalReport(items=rep.items[:1]))
    assert [i.gold_id for i in merged.items] == ["q01", "q02", "q03", "q04"]


# --- report + CLI ---------------------------------------------------------------------------


def _card(gold: dict[str, GoldItem]) -> metrics.Scorecard:
    run = make_run(gold["q05"], status="insufficient_evidence")
    return metrics.score([gold["q05"]], {"q05": run}, {}, date="2026-10-20")


def test_readme_block_replacement_is_idempotent(gold: dict[str, GoldItem]) -> None:
    card = _card(gold)
    block = report.readme_block([("20261020T000000Z", card)])
    text = "# CaseLens\n\nIntro.\n"
    once = report.replace_block(text, block)
    assert once.count(report.START) == 1 and "## Evaluation" in once
    assert report.replace_block(once, block) == once
    newer = report.readme_block([("20261021T000000Z", card), ("20261020T000000Z", card)])
    twice = report.replace_block(once, newer)
    assert twice.count(report.START) == 1 and "Model comparison" in twice
    assert twice.startswith("# CaseLens\n\nIntro.\n")


def _write_runs(tmp_path: Path, runs: list[RunRecord]) -> None:
    for r in runs:
        runner.append_run(tmp_path, r)


def test_score_gate_exit_code(
    gold: dict[str, GoldItem], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(gold["q01"], [cite("c1", EV_RUIZ, {"kind": "pdf", "page": 1})])
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    _write_runs(good, [run])
    _write_runs(bad, [run])

    monkeypatch.setattr(cli, "make_judge_router", lambda _m: FakeJudge(True).router())
    assert cli.main(["score", str(good), "--gold", str(FIXTURE)]) == 0
    assert (good / report.REPORT_FILE).exists() and (good / report.SCORES_FILE).exists()
    monkeypatch.setattr(cli, "make_judge_router", lambda _m: FakeJudge(False).router())
    assert cli.main(["score", str(bad), "--gold", str(FIXTURE)]) == 1
    # Re-scoring uses the cache: no judge router needed.
    assert cli.main(["score", str(good), "--gold", str(FIXTURE), "--no-judge"]) == 0

    readme = tmp_path / "README.md"
    readme.write_text("# SYNTHETIC readme\n", encoding="utf-8")
    monkeypatch.setattr(report, "README", readme)
    assert cli.main(["report", str(good), "--compare", str(bad), "--readme"]) == 0
    text = readme.read_text(encoding="utf-8")
    assert "Citation precision" in text and "Model comparison" in text


def test_smoke_skips_without_live_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CASELENS_DATABASE_URL", raising=False)
    monkeypatch.delenv("CASELENS_EVAL_REQUIRE_LIVE", raising=False)
    assert cli.main(["smoke", "--gold", str(FIXTURE)]) == 0
    assert cli.main(["smoke", "--gold", str(tmp_path / "missing.jsonl")]) == 0
    monkeypatch.setenv("CASELENS_EVAL_REQUIRE_LIVE", "1")
    assert cli.main(["smoke", "--gold", str(FIXTURE)]) == 2
