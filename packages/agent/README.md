# packages/agent: investigator agent

Owner: agent-core agent. The agent loop, answer validator, and model router come in later tickets.

## Contracts (frozen; changes need tech-lead approval)

- `caselens_agent/schemas/answer.schema.json`: the answer contract (JSON Schema 2020-12). `status`,
  `answer`, `claims[]` (each with `citation_ids`), `citations[]` (with a `kind`-discriminated
  locator: pdf / audio / video / record), `conflicts[]`, `confidence`, `open_questions`. The runtime
  validator additionally checks that every `citation_id` resolves to `citations[]`; claims without a
  valid citation force a retry or `insufficient_evidence`.
- `caselens_agent/schemas/tools/*.json`: the six agent tools (`search_evidence`, `search_video`,
  `query_records`, `get_entity`, `get_timeline`, `flag_conflict`). Each file is
  `{name, description, input_schema, output_schema}`; `input_schema` is self-contained and can be sent
  to the Anthropic tool-use API as-is. `case_id` is never a tool input; the runtime binds it.
- `caselens_agent/schemas/tool_definition.schema.json`: meta-schema for the tool files.
- `caselens_agent/contracts.py`: `load_answer_schema()`, `load_tool_definitions()`.

The `query_records` operation enum and per-operation params mirror
`packages/semantic_layer/caselens_semantic_layer/semantic_layer.yaml`; tests in both packages keep them
in sync.
