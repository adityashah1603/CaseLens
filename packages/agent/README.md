# packages/agent: investigator agent

Owner: agent-core agent. This package holds the tools, the plan / act / observe / reflect loop, the
citation validator, the audited run record and a CLI. Every LLM call goes through
`caselens_agent.router.ModelRouter`.

## Ask a question (CLI)

```
set -a; . ./.env; set +a
python -m caselens_agent ask --case-id <case uuid> --user-id <user uuid> "Who was in the gray sedan on March 14?"
```

The CLI opens two connections from `CASELENS_DATABASE_URL`. One uses `SET ROLE caselens_app` (tools,
run record, audit) and the other `SET ROLE caselens_readonly` (semantic-layer SQL). Both set
`app.user_id`, so RLS applies. The CLI prints the answer, each claim with its citations (file name
and locator), conflicts, open questions and the tool trace. A real run writes one `agent_runs` row
plus audit rows, which is normal app behavior.

## Modules

| Module | What it does |
| --- | --- |
| `tools.py` | One executor per contract tool. Input is checked against the contract `input_schema` (jsonschema 2020-12, formats on) before anything runs. Output is checked against `output_schema` before the model sees it. A bad input, an unknown tool, a backend error or a DB error becomes an error observation for the model and never crashes the run. |
| `sources.py` | `SourceLedger`: every citable source a tool returned in the run gets a short handle (`S1`, `S2`, ...), and stored conflicts get `F1`, `F2`, .... Observations show handles and locators, never chunk or record UUIDs. |
| `prompts.py` | System prompts (the citation-or-refuse policy and the evidence-is-data rule) and the JSON shapes the model returns (`AgentAction`, `DraftAnswer`). |
| `prefetch.py` | The retrieval-first fast path: `prefetch_calls(question)` = `search_evidence` + `search_video` on the question, plus `get_entity` for up to 2 `salient_terms` (phone numbers, plate-like tokens, capitalized names / places, "<color> <vehicle>"). A plain heuristic, no model. |
| `loop.py` | `AgentLoop`: prefetch, then model turns, up to 8 tool calls in total (prefetched ones included) and a transcript budget of about 120k characters. A turn can call tools, `finish`, or `answer` directly. Every draft (direct or synthesized) is validated, with one corrective retry in total, and otherwise `insufficient_evidence`. Every attempt counts toward the 8, including invalid ones. `compose_answer` expands handles into contract citations. |
| `validator.py` | `validate_answer(answer, ledger)`: the answer schema plus the runtime rules below. |
| `runner.py` | `ask(app_conn, readonly_conn, case_id, user_id, question, router=None, backends=None)`: the audited run. |
| `testing.py` | `ScriptedModel` (a fake router responder; planning turns, meaning schema `AgentAction`, take the `plan` script) and `FakeBackends` for tests. SYNTHETIC data only. Tests that script every tool call pass `AgentConfig(prefetch=False)`. |

### Tools

- `get_entity` (SQL in this package) looks up by `entity_id` or by name. A name is scored by:
  - an exact `normalized_key` match (the ingest normalization of names, plates and phone numbers),
  - an exact name or alias match,
  - a substring match (`ILIKE`, with wildcards escaped),
  - `pg_trgm` similarity over the canonical name and `attributes.aliases` (threshold 0.3).

  It returns the top 5 matches. Each match lists its active `entity_links` in either direction and
  its **20 most confident (then newest) mentions**, each with chunk id, evidence id and the chunk's
  locator. Mentions of linked entities are not merged in; follow `linked_entity_ids` instead.
- `get_timeline` (SQL in this package) returns events that overlap a time window, belong to an
  entity, or both. For an entity, the set is the entity plus its active links (one hop). An event
  counts when its `location_id` is in the set or one of its source chunks mentions the set. Events
  are ordered by `t_start` and **capped at 50** (narrow with a window). Sources come from `chunks`
  (chunk id, evidence id, locator).
- `flag_conflict` (SQL in this package, and the only write) inserts one `conflicts` row with
  `agent_run_id`. The model passes source handles. The runtime expands them to the full contract
  SourceRefs before the contract schema check, and rejects any source not returned earlier in the
  run.
- `search_evidence`, `search_video` and `query_records` are delegated to `ToolBackends`.
  `default_backends()` lazily imports `caselens_retrieval` and `caselens_semantic_layer`. They are
  not dependencies of this package: the semantic layer depends on `caselens-agent`, so a hard
  dependency would create a cycle. `query_records` runs on the `caselens_readonly` connection.

`case_id` is never a tool input. It comes from `ToolContext`, and every query also filters on it,
with RLS as the second wall.

## Model protocol (why not native tool calling)

The guide mentions the Anthropic tool-use API. The loop uses a provider-neutral JSON protocol
through `ModelRouter.complete_json` instead. That keeps Gemini (free tier, the default), Claude and a
self-hosted model swappable by config, with no tool plumbing per provider:

- Each turn the model returns an `AgentAction`, validated by Pydantic:
  `{"thought": ..., "action": "call_tool", "calls": [{"tool": ..., "input": {...}}, ...]}`,
  `{"action": "finish"}`, or `{"action": "answer", "answer": <draft>}` (the same draft shape as
  synthesis, which saves the separate synthesis call). The first turn after the prefetch uses
  the `synthesize` router task (more thinking, since it often answers); later turns use `plan`. The tool definitions (name, description and `input_schema` from the contract)
  are listed in the system prompt. For `flag_conflict`, the `source_refs` items are shown as handles.
- Tool results go back as JSON inside `evidence_block(...)`. Text is clipped to 600 characters per
  item, and the whole observation to 8000 characters by dropping trailing items. Errors go back as
  plain `Tool error:` lines.
- Task `synthesize`: the model writes a draft that cites source handles (`{"source": "S3", "quote": ...}`)
  and flag handles. The runtime builds the contract answer with citation ids `c1`, `c2`, ... (one per
  handle and quote), and copies ids, evidence ids and locators verbatim from the tool result. The
  model never types UUIDs or locators.

Evidence text is treated as data:
- It only ever appears inside evidence blocks, and a `>>>` delimiter inside evidence is defused.
- The system prompt never changes during a run.
- Tool permissions are fixed in code.
- The validator rejects anything not grounded in observed sources.

## Validator rules

`validate_answer` first checks the answer JSON Schema (draft 2020-12, formats checked), then these
rules:
- An `insufficient_evidence` answer has no claims.
- Every citation id in claims and conflicts resolves to an entry in `citations[]`.
- Every citation matches a source returned in this run: same chunk or record id, evidence id and
  locator.
- A non-null quote is a whitespace-normalized substring of the text the tool returned for that
  source.
- A `conflict_record_id` must come from `flag_conflict` in this run, and an `answered` answer must
  show every conflict flagged in the run.
- Ids are unique within each array.

Unknown handles in the draft are also errors. When validation fails, the model gets one corrective
retry with the error list. If that also fails, the run returns `status: "insufficient_evidence"`
with no claims and an explanation in `open_questions`. Uncited claims are never let through or
patched up.

## Run record and audit

`ask()` writes the run in three phases:
- **Start:** one transaction inserts `agent_runs` (status `running`) and audits `agent.ask` (run id
  and question). RLS rejects non-members, which raises `CaseAccessError`.
- **After each tool call:** it updates `agent_runs.tool_calls` (allowed by migration 0002 while the
  run is `running`) and audits `agent.tool_call`. The audit row records the tool, its input (strings
  clipped to 300 characters), ok / error, result and source counts, the parameterized SQL for
  `query_records` and the conflict id for `flag_conflict`. It never records evidence text.
- **End:** it stores status, model, plan (thoughts, stop reason, validator errors), tool calls,
  answer, citations and latency. The row is then frozen.

If every synthesis attempt fails with a model error, the run is stored as `failed` with the
fallback answer.

### Usage and cost

`complete_json(..., meter=UsageMeter())` records one `CallUsage` per model reply: task, the
model that answered (after fallback), latency, and tokens from Gemini `usageMetadata`
(`promptTokenCount`; output = `candidatesTokenCount` + `thoughtsTokenCount`) or Ollama
(`prompt_eval_count` / `eval_count`). The fake model reports 0. `ask()` stores:

- `agent_runs.input_tokens` / `output_tokens`: sums over the run's model calls.
- `agent_runs.cost_usd`: 0 on the free tier (`CASELENS_LLM_FREE_TIER=true`, the default), else
  the list-price estimate.
- `plan.usage`: `model_calls`, the token totals, `list_price_usd_estimate` and the per-call
  breakdown. The estimate uses `router.LIST_PRICES_USD_PER_MTOK`. These are **assumed paid-tier
  prices for reporting only** (Flash-Lite $0.10 / $0.40, 3.5 Flash $0.30 / $2.50, 3 Flash $0.50 /
  $3.00 per million input / output tokens); update the table if Google's prices differ. It is
  null when a model has no known price.
- `plan.prefetched_steps` and `plan.quotes_dropped` (see Latency).

## Latency

Target (guide): p50 < 8 s, p95 < 20 s end to end. Before this work a run made about 4 to 6
sequential model calls (one planning turn per tool batch, then synthesis, often a validator
retry), plus a 4 s per-model pacing gap and about 0.7 s per search. What changed:

1. **Retrieval-first prefetch** (`prefetch.py`): the opening searches run before the first model
   call (about 1.1 s warm: search_evidence ~0.65 s, search_video ~0.45 s, get_entity ~0.05 s).
   They run sequentially on the run's single RLS connection; running them in parallel would need
   a second connection per run to save about 0.4 s. Prefetched calls are normal, audited tool
   calls, they count toward the 8, and the model is told not to repeat them.
2. **Direct answers**: a turn may return the final draft (`"action": "answer"`), so a typical
   question is prefetch, then 1 turn for any record query, then 1 turn that answers: 2 model
   calls instead of 4 to 6.
3. **Open-question follow-up** (quality guard, `AgentConfig.follow_open_questions`): once per run,
   an `answered` direct draft that still lists open questions while tool calls remain is sent
   back once ("call a tool that settles them, or repeat the answer"). If nothing new is observed,
   that draft is kept with no extra synthesis call.
4. **Quote repair** (`loop.repair_quotes`): an optional quote that is not a verbatim excerpt of
   its source is trimmed of wrapping quotation marks / ellipses, or else set to null, before
   validation. Paraphrased quotes caused most validator retries (a full extra model call). The
   citation itself (the source handle) is untouched and the validator still runs unchanged on
   the result, so no unverified quote ever reaches the answer. The count is stored as
   `plan.quotes_dropped`.
5. **Router pacing**: plan / synthesize skip the fixed 4 s interval
   (`CASELENS_LLM_AGENT_MIN_INTERVAL_S=0`; ingest / judge tasks keep `CASELENS_LLM_MIN_INTERVAL_S`).
   A router shared by many runs (the eval harness) still keeps each agent model under
   `CASELENS_LLM_AGENT_RPM=10` requests per rolling minute.
6. **429 cooldowns**: a model that answers 429 is skipped by every router in the process (the
   API builds one router per request) for Gemini's `RetryInfo.retryDelay`, an hour for a
   per-day `QuotaFailure`, else 60 s (`CASELENS_LLM_QUOTA_COOLDOWN_S` /
   `CASELENS_LLM_QUOTA_DAY_COOLDOWN_S`). Before, every question re-probed spent models (about
   0.3 to 0.6 s each). Only those fields of the 429 body are read and nothing is logged. A router
   with an injected HTTP client (tests) gets its own cooldown table.
7. **Agent timeout fail-over**: a plan / synthesize request slower than
   `CASELENS_LLM_AGENT_TIMEOUT_S=30` moves to the next model in the chain (not a cooldown).
8. Cold start: the first search in a process loads the embedder and reranker (about 7 s). Call
   `caselens_retrieval.encoders.warm_up()` at server start-up. The numbers below are warm.

Measured on the dev DB (demo case, one warm process, free Gemini tier, 2026-10-20). Model
calls are successful replies (fast 429s on spent models not counted). Most "after" calls fell
back to gemini-3.5-flash-lite because the shared key's 3.5-flash / 3-flash-preview daily quota
was spent:

| Question | Before | Model calls | After | Model calls |
| --- | --- | --- | --- | --- |
| Who was in the gray sedan on March 14? | 26 to 105 s (4 earlier runs) | about 5 to 7 (estimated from 7 to 8 tool calls) | 8.4 s / 11.1 s | 2 / 3 |
| How many times did Kemper and Hale call each other in the week before the shooting? | 18.1 s | 4 | 7.0 s | 2 |
| When was plate SYN4821 read entering and leaving the Riverside Lot? | n/a | n/a | 6.6 s | 2 |
| What is Travis Kemper's blood type? (unanswerable) | 25.6 s | 4 | 6.1 s | 2 |
| What time did the gray car arrive at the lot? | 14.1 s | 4 | 6.3 s | 2 |

All "after" answers were correct and cited: 7 calls with CDR rows, the SYN4821 22:41:05 / 22:47:31
plate reads, `insufficient_evidence` with no claims for the blood type, and 22:41 for the
arrival. On flash-lite, the north-star answer names Kemper and the two-occupant conflict but
leaves the passenger unidentified. An earlier 3.5-flash run named Rory Vance, after 8 tool calls
and 96 s. This is a model-strength gap that is still open: re-measure when the 3.5-flash quota is
available.

## Contracts (frozen; changes need tech-lead approval)

- `caselens_agent/schemas/answer.schema.json`: the answer contract (JSON Schema 2020-12). It covers
  `status`, `answer`, `claims[]` (each with `citation_ids`), `citations[]` (with a `kind`-discriminated
  locator: pdf / audio / video / record), `conflicts[]`, `confidence` and `open_questions`.
- `caselens_agent/schemas/tools/*.json`: the six agent tools (`search_evidence`, `search_video`,
  `query_records`, `get_entity`, `get_timeline`, `flag_conflict`). Each file is
  `{name, description, input_schema, output_schema}`. `case_id` is never a tool input; the runtime
  binds it.
- `caselens_agent/schemas/tool_definition.schema.json`: the meta-schema for the tool files.
- `caselens_agent/contracts.py`: `load_answer_schema()` and `load_tool_definitions()`.

The `query_records` operation enum and per-operation params mirror
`packages/semantic_layer/caselens_semantic_layer/semantic_layer.yaml`. Tests in both packages keep
them in sync.

## Tests

- `tests/test_agent_loop.py` (fast; fake router and fake backends) covers:
  - a scripted multi-step run that ends in a cited answer,
  - an uncited claim, then a retry, then `insufficient_evidence`,
  - a retry that fixes the answer,
  - unknown, fabricated and altered citations being rejected,
  - the quote and duplicate-id checks,
  - schema-violating tool input becoming an error observation,
  - the 8-call cap,
  - prompt injection inside evidence,
  - `flag_conflict` handles and the rule that flagged conflicts must be surfaced.
- `tests/test_fast_path.py` (fast; fake router, fake backends, mock HTTP) covers:
  - `salient_terms` / `prefetch_calls`,
  - prefetch followed by a direct answer in 1 model call,
  - a prefetched call never being repeated,
  - a rejected direct answer getting one corrective synthesis, then `insufficient_evidence`,
  - the open-question follow-up (it can call tools, is sent once, keeps the draft on finish),
  - quote repair,
  - Gemini usage reaching the meter and the list-price estimate,
  - agent pacing and the per-minute cap,
  - 429 cooldowns shared across routers (retryDelay and per-day quota),
  - timeout fail-over.
- `tests/test_agent_db.py` (`db` marker) covers:
  - `get_entity` and `get_timeline` SQL against SYNTHETIC rows,
  - `ask()` writing the run, tool calls, conflict and audit rows (and token / cost columns) as
    `caselens_app`,
  - a finished run being frozen,
  - a non-member being refused by RLS.
