# packages/semantic_layer: semantic layer

Owner: retrieval agent. A compiler (a later ticket) will turn an operation + params into parameterized
SQL that runs on the read-only role.

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
