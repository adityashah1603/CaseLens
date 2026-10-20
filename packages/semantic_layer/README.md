# packages/semantic_layer: semantic layer

Owner: retrieval agent. The LLM never writes SQL: through the `query_records` tool it picks one
operation and gives its params, and this package compiles that into parameterized SQL that runs
on the read-only role, scoped to the case.

## Contracts (frozen; changes need tech-lead approval)

- `caselens_semantic_layer/semantic_layer.yaml`: declarative sources (`call_records`, `plate_reads`,
  `custody_records`, `entities`), joins, metrics, and operations (`calls_between`, `calls_for_phone`,
  `call_count_between`, `plate_sightings`, `plate_sightings_near`, `custody_chain`). Filters are
  structured predicates `{column, op, param}` (ops: eq, in, gte, lte, between, ilike) plus `any_of` /
  `all_of` groups; there is no raw SQL. The compiler always injects the `case_id` filter and applies
  `defaults` (role `caselens_readonly`, 500 rows, 5 s timeout).
- `caselens_semantic_layer/semantic_layer.schema.json`: JSON Schema for the YAML.
- `caselens_semantic_layer/contracts.py`: `load_semantic_layer()`, `load_semantic_layer_schema()`.

Operation names and params must match the `query_records` tool in `packages/agent`; the tests check this.

## Compiler

```python
from caselens_semantic_layer import OperationError, run_operation

out = run_operation(
    conn,
    case_id,
    "call_count_between",
    {
        "phone_a": "(360) 555-0117",
        "phone_b": "+13605550142",
        "window_start": "2026-03-07T22:44:00-08:00",
        "window_end": "2026-03-14T22:44:00-07:00",
    },
)  # -> QueryRecordsOutput (matches the query_records output_schema)
```

- `conn` must already be on `caselens_readonly` with `app.user_id` set (RLS applies); the compiler
  refuses any other role (`RoleError`). `case_id` is bound by the runtime, never an operation param.
- Validation (`params.py`): the YAML is validated against its schema and cross-checked once (cached,
  `layer.py`). Params are checked against the tool contract's per-operation input schema, then
  normalized per `param_types` (phone -> E.164 with +1 for 10 digits, plate -> uppercase without
  spaces/hyphens, timestamp -> aware datetime (a UTC offset is required), string -> trimmed,
  1..200 chars) and re-checked against `pattern`. Failures raise `OperationError` (a `ValueError`)
  whose message names the param and rule but never echoes the value.
- Compilation (`compiler.py`): tables/columns come only from the YAML via `sql.Identifier`; ops come
  from a fixed mapping (`eq`, `gte`, `lte`, `between`, `ilike` as an escaped contains-match with
  `ESCAPE '\'`, `in` expanded to one placeholder per element); every value is a named bound param.
  `<case_column> = %(case_id)s` is always the first predicate. List operations apply the YAML
  `order_by` plus the primary key as a final tiebreak and `LIMIT max_rows + 1` to detect truncation.
  YAML `joins` are not compiled because no operation uses one.
- Execution: inside a (sub)transaction that sets `SET LOCAL transaction_read_only = on` and
  `SET LOCAL statement_timeout = <defaults.statement_timeout_ms>` (a validated int literal) and is
  always rolled back, so neither setting leaks into the caller's transaction.
- Output: `sql` is the rendered statement with `%(name)s` placeholders (no values); `params` holds
  every bound value after normalization, JSON-safe, **including `case_id`** so `sql` + `params` are a
  complete audit record (ilike params appear escaped and wrapped, e.g. `%Riverside%`). Rows are
  JSON-safe (ISO datetimes, str uuids, numerics as floats) and keyed by the operation's `select`
  columns; `sources[i]` cites `rows[i]` via the YAML `citation` columns.
- Metric operations (`call_count_between`) run as one statement: a CTE of the matching rows, the
  aggregate, and a lateral list of the matching rows' citations. `rows` is one row
  (`{"call_count": n}`), `row_count` is 1, and `sources` cites every counted record (ordered by
  evidence id, row id; capped at `max_rows`, with `truncated: true` when the citation list is cut —
  the count itself is always exact).

## Tests

`tests/test_compiler.py` (`db` marker) inserts SYNTHETIC call, plate and custody rows as owner, then
runs every operation as `caselens_readonly`: normalization equivalence, case scoping, RLS for
non-members and an unset user, truncation, timeout/read-only settings, write rejection, wrong role,
ilike escaping, bad params, an injection attempt, and output validation against the tool's
`output_schema`. Set `CASELENS_TEST_DATABASE_URL` to run them.
