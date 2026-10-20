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
| `loop.py` | `AgentLoop`: up to 8 tool calls and a transcript budget of about 120k characters, then synthesis, validation, one corrective retry, and otherwise `insufficient_evidence`. Every attempt counts toward the 8, including invalid ones. `compose_answer` expands handles into contract citations. |
| `validator.py` | `validate_answer(answer, ledger)`: the answer schema plus the runtime rules below. |
| `runner.py` | `ask(app_conn, readonly_conn, case_id, user_id, question, router=None, backends=None)`: the audited run. |
| `testing.py` | `ScriptedModel` (a fake router responder) and `FakeBackends` for tests. SYNTHETIC data only. |

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

- Task `plan`, at each step: the model returns
  `{"thought": ..., "action": "call_tool", "tool": ..., "input": {...}}` or `{"action": "finish"}`,
  validated by Pydantic. The tool definitions (name, description and `input_schema` from the contract)
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

Token counts and cost stay null because the router does not report usage yet. If every synthesis
attempt fails with a model error, the run is stored as `failed` with the fallback answer.

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
- `tests/test_agent_db.py` (`db` marker) covers:
  - `get_entity` and `get_timeline` SQL against SYNTHETIC rows,
  - `ask()` writing the run, tool calls, conflict and audit rows as `caselens_app`,
  - a finished run being frozen,
  - a non-member being refused by RLS.
