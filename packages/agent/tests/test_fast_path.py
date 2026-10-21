"""Latency fast path: retrieval-first prefetch, direct answers, quote repair, usage accounting and
agent pacing. Fake model and fake backends only (no DB, no LLM). All data is SYNTHETIC."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
from pydantic import BaseModel

from caselens_agent.loop import AgentConfig, AgentLoop, LoopResult, repair_quotes
from caselens_agent.prefetch import prefetch_calls, salient_terms
from caselens_agent.prompts import DraftAnswer
from caselens_agent.router import (
    ModelCooldowns,
    ModelRouter,
    RouterSettings,
    UsageMeter,
    list_price,
    quota_cooldown,
)
from caselens_agent.sources import SourceLedger, SourceRef
from caselens_agent.testing import FakeBackends, ScriptedModel, evidence_result, tool_call
from caselens_agent.tools import Conn, ToolContext, ToolRuntime

CASE = uuid.UUID("aa000000-0000-4000-8000-000000000002")
STATEMENT = "SYNTHETIC. I saw Dale Mercer drive the gray sedan into the lot at 22:41."
QUESTION = "Who drove the gray sedan?"


class FakeConn:
    """Transactions are no-ops; get_entity SQL returns no rows."""

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, query: str, params: Any = None) -> FakeConn:
        assert "FROM public.entities" in query
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


def run(model: ScriptedModel, fakes: FakeBackends, config: AgentConfig | None = None) -> LoopResult:
    conn = cast(Conn, FakeConn())
    runtime = ToolRuntime(ToolContext(conn, conn, CASE), fakes.backends())
    return AgentLoop(model.router(), runtime, config).run(QUESTION)


def answer(quote: str | None, source: str = "S1") -> dict[str, Any]:
    draft = {
        "status": "answered",
        "answer": "SYNTHETIC. Dale Mercer drove the gray sedan.",
        "claims": [
            {
                "text": "A witness saw Dale Mercer drive the gray sedan.",
                "citations": [{"source": source, "quote": quote}],
            }
        ],
        "conflicts": [],
        "confidence": 0.8,
        "open_questions": [],
    }
    return {"thought": "the prefetched statement answers it", "action": "answer", "answer": draft}


def test_salient_terms_pick_names_plates_phones_and_vehicles() -> None:
    assert salient_terms("Who was in the gray sedan on March 14?") == ["gray sedan"]
    assert salient_terms("How many times did Kemper and Hale call each other?") == [
        "Kemper",
        "Hale",
    ]
    assert salient_terms("What is Travis Kemper's blood type?") == ["Travis Kemper"]
    assert salient_terms("When was plate SYN4821 read at the Riverside Lot?") == [
        "SYN4821",
        "Riverside Lot",
    ]
    assert salient_terms("Who called (360) 555-0117 on Monday?") == ["(360) 555-0117"]
    assert salient_terms("what happened next?") == []
    calls = prefetch_calls("Did Dale Mercer and Rory Vale and Ann Pike meet?", entity_limit=2)
    assert [c.tool for c in calls] == [
        "search_evidence",
        "search_video",
        "get_entity",
        "get_entity",
    ]
    assert calls[0].input == {"query": "Did Dale Mercer and Rory Vale and Ann Pike meet?"}


def test_prefetch_then_direct_answer_is_one_model_call() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(plan=[answer("drive the gray sedan into the lot")])
    result = run(model, fakes, AgentConfig())

    tools = [c.tool for c in result.tool_calls]
    assert tools == ["search_evidence", "search_video", "get_entity"]  # "gray sedan"
    assert result.prefetched_steps == 3
    assert len(model.calls) == 1 and model.calls[0].task == "synthesize"
    # The prefetched observation is in the first prompt, marked as run by the system.
    assert "the system ran (before your first turn)" in model.calls[0].user
    assert "drive the gray sedan" in model.calls[0].user
    assert result.stop_reason == "answer" and not result.fell_back
    assert result.answer["status"] == "answered"
    assert result.answer["citations"][0]["quote"] == "drive the gray sedan into the lot"
    assert len(result.usage.calls) == 1


def test_model_does_not_repeat_a_prefetched_call() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(
        plan=[tool_call("search_evidence", {"query": QUESTION}), answer(None)],
    )
    result = run(model, fakes, AgentConfig())
    assert [c[0] for c in fakes.calls] == ["search_evidence", "search_video"]
    assert "Repeated call" in model.calls[1].user
    assert model.calls[1].task == "plan"  # later turns use the planning settings
    assert result.answer["status"] == "answered"


def test_rejected_direct_answer_gets_one_corrective_synthesis_then_falls_back() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    bad = answer(None, source="S9")["answer"]  # unknown handle
    model = ScriptedModel(plan=[answer(None, source="S9")], synthesize=[bad])
    result = run(model, fakes, AgentConfig())

    # direct answer = attempt 1, one corrective synthesis = attempt 2, then refuse
    assert [c.task for c in model.calls] == ["synthesize", "synthesize"]
    assert "rejected by the citation validator" in model.calls[1].user
    assert result.fell_back and not result.model_failed
    assert result.answer["status"] == "insufficient_evidence" and result.answer["claims"] == []
    assert len(result.validation_errors) == 2


def test_rejected_direct_answer_can_be_fixed_by_synthesis() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(
        plan=[answer(None, source="S9")], synthesize=[answer(None, source="S1")["answer"]]
    )
    result = run(model, fakes, AgentConfig())
    assert not result.fell_back and result.answer["status"] == "answered"
    assert result.answer["citations"][0]["chunk_id"] == fakes.evidence[0]["chunk_id"]


def test_quote_repair_trims_marks_and_drops_unverifiable_quotes() -> None:
    ledger = SourceLedger()
    chunk = evidence_result(STATEMENT)
    ledger.add(
        SourceRef.chunk(chunk["chunk_id"], chunk["evidence_id"], chunk["locator"]),
        "search_evidence",
        STATEMENT,
    )
    draft = DraftAnswer.model_validate(
        {
            "status": "answered",
            "answer": "SYNTHETIC.",
            "claims": [
                {"text": "a", "citations": [{"source": "S1", "quote": "“Dale Mercer drive…"}]},
                {"text": "b", "citations": [{"source": "S1", "quote": "Mercer confessed"}]},
                {"text": "c", "citations": [{"source": "S1", "quote": "into  the LOT"}]},
            ],
        }
    )
    assert repair_quotes(draft, ledger) == 1
    quotes = [c.citations[0].quote for c in draft.claims]
    assert quotes == ["Dale Mercer drive", None, "into  the LOT"]
    # the citations themselves are untouched
    assert all(c.citations[0].source == "S1" for c in draft.claims)


def test_quote_repair_saves_the_corrective_call() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(plan=[answer("Mercer was driving the sedan")])  # paraphrase
    result = run(model, fakes, AgentConfig())
    assert len(model.calls) == 1 and not result.fell_back
    assert result.answer["citations"][0]["quote"] is None
    assert result.quotes_dropped == 1


class Out(BaseModel):
    name: str


def _gemini_router(handler: Any, sleeps: list[float], **settings: Any) -> ModelRouter:
    return ModelRouter(
        RouterSettings(llm_provider="gemini", gemini_api_key="k-SYNTHETIC", **settings),  # type: ignore[arg-type]
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": json.dumps({"name": "SYNTHETIC"})}]}}],
            "usageMetadata": {
                "promptTokenCount": 1200,
                "candidatesTokenCount": 80,
                "thoughtsTokenCount": 20,
            },
        },
    )


def test_router_reports_gemini_usage_to_the_meter() -> None:
    meter = UsageMeter()
    router = _gemini_router(_ok, [])
    router.complete_json("plan", "sys", "user", Out, meter=meter)
    (call,) = meter.calls
    assert (call.task, call.model) == ("plan", "gemini-3.5-flash")
    assert (call.input_tokens, call.output_tokens, call.thought_tokens) == (1200, 100, 20)
    price = list_price("gemini-3.5-flash")
    assert price is not None
    assert meter.list_price_usd() == round((1200 * price[0] + 100 * price[1]) / 1e6, 6)
    assert meter.summary()["model_calls"] == 1
    assert list_price("some-unknown-model") is None


def test_agent_tasks_skip_fixed_pacing_but_keep_a_per_minute_cap() -> None:
    sleeps: list[float] = []
    router = _gemini_router(_ok, sleeps, llm_agent_rpm=3)
    for _ in range(3):
        router.complete_json("plan", "sys", "user", Out)
    assert sleeps == []  # no 4 s interval between agent calls
    router.complete_json("synthesize", "sys", "user", Out)  # 4th call on the same model
    assert len(sleeps) == 1 and 50 < sleeps[0] <= 60
    # ingest tasks keep the fixed interval
    router.complete_json("extract", "sys", "user", Out)
    router.complete_json("extract", "sys", "user", Out)
    assert len(sleeps) == 2 and 3 < sleeps[1] <= 4


def _quota_429(details: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(429, json={"error": {"code": 429, "details": details}})


def test_quota_cooldown_reads_retry_delay_and_per_day_quota() -> None:
    retry = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}
    day = {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
    }
    assert quota_cooldown(_quota_429([retry]), 60.0, 3600.0) == 37.0
    assert quota_cooldown(_quota_429([retry, day]), 60.0, 3600.0) == 3600.0
    assert quota_cooldown(httpx.Response(429, text="not json"), 60.0, 3600.0) == 60.0


def test_a_429_model_is_skipped_by_other_routers_until_its_cooldown_ends() -> None:
    now = [0.0]
    shared = ModelCooldowns(clock=lambda: now[0])
    seen: list[str] = []
    retry = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        seen.append(model)
        return _quota_429([retry]) if model == "gemini-3.5-flash" else _ok(request)

    def router() -> ModelRouter:
        return ModelRouter(
            RouterSettings(llm_provider="gemini", gemini_api_key="k-SYNTHETIC"),  # type: ignore[arg-type]
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda s: None,
            cooldowns=shared,
        )

    router().complete_json("plan", "sys", "user", Out)
    router().complete_json("plan", "sys", "user", Out)  # a new router (next API request)
    assert seen == ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3-flash-preview"]
    now[0] = 31.0
    router().complete_json("plan", "sys", "user", Out)
    assert seen[-2:] == ["gemini-3.5-flash", "gemini-3-flash-preview"]  # re-probed after 30 s


def test_agent_call_timeout_fails_over_to_the_next_model() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        seen.append(model)
        if model == "gemini-3.5-flash":
            raise httpx.ReadTimeout("slow", request=request)
        return _ok(request)

    router = _gemini_router(handler, [])
    meter = UsageMeter()
    assert router.complete_json("synthesize", "sys", "user", Out, meter=meter).name == "SYNTHETIC"
    assert seen == ["gemini-3.5-flash", "gemini-3-flash-preview"]
    assert meter.calls[0].model == "gemini-3-flash-preview"
    # a timeout is not a quota error: the model is not put on cooldown
    assert not router.cooldowns.cooling("gemini-3.5-flash")


def with_gap(action: dict[str, Any], gap: str = "Who was the passenger?") -> dict[str, Any]:
    out = json.loads(json.dumps(action))
    out["answer"]["open_questions"] = [gap]
    return out


def test_open_questions_get_one_follow_up_turn_that_can_call_tools() -> None:
    receipt = "SYNTHETIC. Store receipt: passenger Rory Vale paid cash at 22:30."
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(
        plan=[
            with_gap(answer(None)),
            lambda user: (
                fakes.evidence.append(evidence_result(receipt))
                or tool_call("search_evidence", {"query": "passenger receipt"})
            ),
            answer(None, source="S2"),
        ]
    )
    result = run(model, fakes, AgentConfig())
    assert "leaves these open:" in model.calls[1].user
    # The gaps are model-written text that may echo evidence: quoted as data.
    assert "<<<EVIDENCE open questions of your draft\n- Who was the passenger?" in (
        model.calls[1].user
    )
    assert "leaves these open" not in model.calls[2].user  # the note is one-off
    assert len(model.calls) == 3 and result.stop_reason == "answer"
    assert [c.tool for c in result.tool_calls][-1] == "search_evidence"
    assert result.answer["claims"][0]["citation_ids"] == ["c1"]
    assert result.answer["citations"][0]["chunk_id"] == fakes.evidence[1]["chunk_id"]


def test_follow_up_is_sent_once_and_a_repeated_answer_is_kept() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    gap = with_gap(answer(None))
    model = ScriptedModel(plan=[gap, gap])
    result = run(model, fakes, AgentConfig())
    assert len(model.calls) == 2 and not result.fell_back
    assert result.answer["open_questions"] == ["Who was the passenger?"]


def test_finish_after_the_follow_up_keeps_the_draft_without_a_synthesis_call() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(plan=[with_gap(answer(None))])  # then the script finishes
    result = run(model, fakes, AgentConfig())
    assert [c.task for c in model.calls] == ["synthesize", "plan"]
    assert result.answer["status"] == "answered" and result.stop_reason == "answer"


def test_follow_up_can_be_turned_off() -> None:
    fakes = FakeBackends(evidence=[evidence_result(STATEMENT)])
    model = ScriptedModel(plan=[with_gap(answer(None))])
    result = run(model, fakes, AgentConfig(follow_open_questions=False))
    assert len(model.calls) == 1 and result.answer["status"] == "answered"
