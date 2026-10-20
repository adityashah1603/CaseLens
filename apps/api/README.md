# apps/api: CaseLens API

FastAPI service. Owner: backend agent. API contract: `docs/contracts/openapi.yaml` (every `/v1`
path is implemented to it). Schema + RLS: `alembic/versions/`. All data is SYNTHETIC.

## Run locally

```
make up && make migrate                       # Postgres + MinIO, schema
# .env needs CASELENS_DEV_AUTH_SECRET (32+ random characters) for local dev tokens
python -m caselens_api seed-dev-users         # demo viewer + analyst on the demo case; prints tokens
python -m caselens_api serve                  # uvicorn on 127.0.0.1:8000, one worker
python -m caselens_api dev-token local-dev:seed-lead --hours 8
curl -H "Authorization: Bearer $LEAD_TOKEN" http://127.0.0.1:8000/v1/me
```

`seed-dev-users` acts through RLS as the seed lead (`local-dev:seed-lead`): it ensures the users
`local-dev:demo-viewer` ("Demo viewer (SYNTHETIC)", viewer) and `local-dev:analyst` (analyst) are
members of "The Riverside Lot Homicide" (audited `member.add` / `member.update`), then prints
`LEAD_TOKEN`, `ANALYST_TOKEN` and `VIEWER_TOKEN`. `users.is_demo`, `users.is_admin` and
`cases.is_demo` are operator-only columns (no app grant): set them with SQL as the migrator when
needed. Suggested questions appear only on cases with `is_demo = true`.

## Layout

| Module | What |
| --- | --- |
| `settings.py` | `Settings` from `CASELENS_*` (DB, roles, auth, S3, CORS, rate limit, SSE) |
| `db.py` | psycopg pool; each checkout does `SET ROLE caselens_app` (or `caselens_readonly`) and `set_config('app.user_id', ...)`, both reset on return |
| `auth.py` | token verifiers (Firebase RS256 / local HS256 dev), user resolve-or-create by `oidc_subject` |
| `deps.py` | services on `app.state`, the per-request session, case role checks, `/ask` rate limiter |
| `problems.py` | RFC 9457 `application/problem+json` for every error (422 carries `errors[{loc,msg}]`) |
| `storage.py` | evidence store interface: streaming put, streaming SHA-256, signed GET URL (MinIO / memory) |
| `routes/` | `cases` (me, cases, members), `evidence`, `ingest` (SSE), `agent`, `graph` (timeline, entities, graph, conflicts), `audit` (case + admin) |
| `testing.py` | contract checker (responses vs. `openapi.yaml`) and a SYNTHETIC test world |

## Security model

* **Auth.** `Authorization: Bearer <JWT>`. Firebase ID tokens are verified against Google's JWKS for
  `CASELENS_FIREBASE_PROJECT_ID` (RS256; iss, aud, exp, iat, sub). Dev tokens (HS256,
  `CASELENS_DEV_AUTH_SECRET`) are accepted only when `CASELENS_ENVIRONMENT=local`; the app refuses
  to start with a dev secret in any other environment. Dev tokens are minted only by the CLI.
  On first sign-in the `users` row is created as `caselens_app` (users RLS allows it).
* **Never the owner.** Every request runs as `caselens_app` with `app.user_id` set; `/ask` adds a
  second checkout as `caselens_readonly` for semantic-layer SQL. RLS is the second wall.
* **Roles.** Non-members get 404 on every case route (existence never leaks); too-low roles get 403.
  Lead: case update, members, audit. Analyst+: upload, entity-link revert. Viewer+: everything else,
  including `/ask`. The demo user (`users.is_demo`) cannot create cases. Admin routes need
  `users.is_admin`.
* **Audit.** Written as the user, in the same transaction as the action: `case.create`,
  `case.update`, `member.add|update|remove`, `evidence.upload`, `evidence.view` (content redirect,
  with `integrity_ok`), `timeline.view`, `entity_link.revert`, `audit.view`, `admin.audit.view`,
  `admin.audit.verify`; the agent runner writes `agent.ask` / `agent.tool_call`. Audit inserts never
  use `RETURNING` (only leads/admins may read rows back), and case ids are generated in the API
  (D-026).
* **Evidence content.** `GET .../content` re-hashes the stored object (streamed, 1 MiB chunks)
  against `evidence.sha256`; on mismatch or a missing object it answers 409 and never serves the
  file. Otherwise 307 to a presigned GET URL valid `CASELENS_SIGNED_URL_TTL_S` (300 s) with
  `Cache-Control: no-store`. The URL host is `CASELENS_S3_ENDPOINT`, so it must be reachable by the
  browser (127.0.0.1:9000 in docker compose).

## Behaviour notes

* **Pagination.** Opaque base64url keyset cursors; `limit` 1..100 (default 50); `{items, next_cursor}`.
  Cases, runs and conflicts are newest first; members, evidence, timeline (`t_start`), entities
  (name, or score with `q`) and audit (`id`) ascending.
* **Upload.** Multipart `file` (+ optional `description`, `recorded_at`). Modality from the file part's
  content type: `application/pdf`, `audio/*`, `video/*`, `text/csv`, else 415 (browsers on Windows
  may label CSV `application/vnd.ms-excel`: set `type: "text/csv"` on the `File`). Max
  `CASELENS_MAX_UPLOAD_BYTES` (413). Streams to storage, SHA-256, 409 on a duplicate in the case,
  then in one transaction: evidence row with the first custody entry, `evidence.upload` audit, and
  the modality's first ingest job (`queued`). Later stages are queued by the workers as each stage
  succeeds, so the 201 `ingest_jobs` lists that one job.
* **SSE** (`/ingest/events`). There is no LISTEN/NOTIFY trigger (that needs a migration), so the
  stream polls the case's jobs every `CASELENS_SSE_POLL_S` (1.5 s) and diffs snapshots, emitting
  `job.updated`, `evidence.ready`, `evidence.failed`, and `: heartbeat` every 15 s. Ids are
  monotonic per stream; with `Last-Event-ID: N` numbering resumes at N+1 and the current state of
  every job is re-sent first. Streams end after `CASELENS_SSE_MAX_STREAM_S` (30 min); clients
  reconnect. Each poll briefly checks out a pooled connection.
* **`/ask`.** Runs `caselens_agent.runner.ask` synchronously in the threadpool (20-100 s on the free
  Gemini tier). Rate limit: `CASELENS_ASK_RATE_LIMIT` questions per user per
  `CASELENS_ASK_RATE_WINDOW_S` (6 / 60 s), counted in-process (one uvicorn worker locally; the edge
  limiter covers prod), 429 with `Retry-After`. If the agent raises, the run (marked failed by the
  runner) is returned with `status: failed`. Token counts and cost are null (the router does not
  report them yet). `plan` is the agent's thoughts; tool calls outside the contract's six tools are
  omitted from `tool_calls`.

## Tests

```
CASELENS_TEST_DATABASE_URL=postgresql+psycopg://caselens:change-me-local-only@127.0.0.1:5432/caselens_test_api \
  python -m pytest apps/api/tests/test_api.py
```

DB tests (`db` marker) seed a fresh SYNTHETIC world per module and commit (the API's pooled
connections cannot share a rolled-back transaction), so use a throwaway database. Every response
body is validated against the contract component schema; the agent runs with a scripted fake model.

## Migrations

The schema, RLS policies, roles and the audit hash chain are in `alembic/versions/` (0001-0003).
These files are a contract: change them only with tech-lead approval.

```
make migrate                                            # upgrade the dev DB (CASELENS_DATABASE_URL)
CASELENS_TEST_DATABASE_URL=... pytest apps/api/tests    # migration + RLS + audit-chain + API tests
```

Note: `test_migration.py` runs `alembic downgrade base` on its database; point it at its own
throwaway database, not the one the API tests use.
