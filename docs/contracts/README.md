# Contracts

These files are the frozen interfaces between the CaseLens components. Implement **to** them.
They change only with tech-lead approval: propose the change first, then update the contract, its
tests, and every implementation in the same reviewed PR.

| Contract | File | Owner (implements it) |
| --- | --- | --- |
| HTTP API (OpenAPI 3.1) | `docs/contracts/openapi.yaml` | Backend (`apps/api`), Frontend (`apps/web`) |
| Answer contract (JSON Schema) | `packages/agent/caselens_agent/schemas/answer.schema.json` | Agent core (`packages/agent`) |
| Agent tool schemas | `packages/agent/caselens_agent/schemas/tools/*.json` | Agent core (`packages/agent`) |
| Semantic layer | `packages/semantic_layer/caselens_semantic_layer/semantic_layer.yaml` (validated by `semantic_layer.schema.json`) | Retrieval (`packages/semantic_layer`) |
| DB schema + RLS | `apps/api/alembic/versions/0001_initial_schema.py` | Backend (`apps/api`) |

## API notes

- All paths are under `/v1` except `GET /healthz`. Every other operation needs a Firebase Auth ID
  token as `Authorization: Bearer <token>`.
- Roles per case: `lead` > `analyst` > `viewer`. Each operation's description states the minimum
  role. Non-members get `404`; members whose role is too low get `403`.
- Errors are `application/problem+json` (RFC 9457). Lists return `{items, next_cursor}` and take
  `cursor` + `limit` (max 100).
- `AnswerContract` in the OpenAPI file mirrors `answer.schema.json`. Keep the two in sync.
- Frontend API types are generated from `openapi.yaml`; do not hand-write them.

`tests/test_openapi_contract.py` validates the OpenAPI file (OpenAPI 3.1 validity, auth, Problem
errors, enums, and a SYNTHETIC sample answer against `AnswerContract`).

All examples in these files are SYNTHETIC.
