# CLAUDE.md — CaseLens

Read this file first in every session. Reference docs live in `reference/`, which is local-only
(gitignored) and **read-only**: do not edit them unless the tech lead says so.
- `reference/CaseLens — Project Overview & Agent Build Guide.md`: the source of truth
- `reference/ROADMAP.md`: the 6 weeks and their gates
- `reference/SYSTEM_DESIGN.md`: requirements, entities, APIs, design
- `reference/DECISIONS.md`: the decision log (clear the read-only flag only to append a decision)

If these conflict, the project guide wins. If something is ambiguous, ask the tech lead instead of guessing.

## Project summary

CaseLens is a deployed evidence-search copilot. A detective uploads a mixed case file
(PDFs incl. scanned, audio, video, CSV) and asks questions in plain English. Every answer is a
cited, auditable timeline: each claim links to a page+bbox, an audio timestamp, a video frame, or
a table row. The system refuses ("insufficient evidence") rather than answer without a citation.

**North star:** a visitor opens the live URL, picks the demo case, asks
"Who was in the gray sedan on March 14?", and sees a cited timeline in under 15 seconds.

**All data is synthetic.** The demo case "The Riverside Lot Homicide" is generated from a hidden
ground-truth story in `data_gen/`. Every generated file carries a SYNTHETIC header.

### Scope

- **MVP:** multi-case workspace with login + per-case access control; ingest of 4 modalities;
  entity extraction/resolution into a case graph; investigator agent (doc search, video search,
  SQL, graph lookup); clickable citations; auto timeline; immutable audit log; 50-question eval
  harness in CI; live GCP deployment with a read-only demo login.
- **Stretch (only after MVP is live):** cross-case linking, re-ID on synthetic actors, lead
  generation, court-ready PDF export, streaming agent trace UI.
- **Non-goals:** real case data or real people, facial recognition against real identities,
  predictive policing or risk scoring of people, actual CJIS certification.

## Stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic |
| Database | Postgres 16 + pgvector (Cloud SQL in prod), row-level security per case |
| Object storage | GCS (prod), MinIO (local), behind a thin storage interface |
| Queue | Cloud Tasks, one queue per ingest stage, behind a thin queue interface |
| Auth | Firebase Auth (OIDC ID tokens, bearer JWT) |
| Agent | Plain Python loop with tool use; all LLM calls go through one model router (Claude / GPT / self-hosted vLLM swappable by config) |
| Frontend | React + TypeScript (Vite), Tailwind + shadcn/ui, TanStack Query |
| Infra | Terraform on GCP, GitHub Actions CI/CD, `docker compose` for local dev |

Prefer free / open-source tools and free tiers (see `reference/DECISIONS.md`).

## Repo map and ownership

| Path | Owner role | Contents |
| --- | --- | --- |
| `apps/api` | Backend agent | FastAPI app, auth, RBAC, audit, endpoints, SSE, Alembic migrations |
| `apps/web` | Frontend agent | React app |
| `workers/` | Ingestion agent | Ingest job framework and per-modality pipelines |
| `packages/retrieval` | Retrieval agent | Hybrid search + reranker |
| `packages/semantic_layer` | Retrieval agent | Semantic layer YAML + compiler to parameterized SQL |
| `packages/agent` | Agent-core agent | Tool schemas, tools, loop, answer validator, model router |
| `evals/` | Eval agent | Gold set, metrics, CI gate, report |
| `data_gen/` | Data-gen agent | Ground-truth story and synthetic evidence generators |
| `infra/` | Infra agent | Terraform; CI/CD lives in `.github/workflows` |
| `docs/` | Tech lead | Contracts (`docs/contracts/`) and docs |

### Contracts (frozen; change only with tech-lead approval)

- API: `docs/contracts/openapi.yaml`
- DB schema + RLS: `apps/api/alembic/versions/` (0001 initial, 0002 admin + run updates, 0003 worker system audit)
- Agent tool schemas: `packages/agent/caselens_agent/schemas/tools/*.json`
- Answer contract: `packages/agent/caselens_agent/schemas/answer.schema.json`
- Semantic layer: `packages/semantic_layer/caselens_semantic_layer/semantic_layer.yaml`
  (validated by `semantic_layer.schema.json`)

Implement **to** the contracts. Never invent or silently change them. If one needs to change,
stop and propose the change to the tech lead.

## Ground rules for every agent session

1. This `CLAUDE.md` holds the overview, the stack, coding conventions, and the definition of done.
   **Never commit secrets. Never use real personal data.**
2. Contracts first. The tech lead owns the OpenAPI spec, DB schema (Alembic migration), tool
   schemas, and answer-contract JSON schema. Agents implement to them and never invent them.
3. One ticket = one branch = one PR, ideally under 400 lines changed.
4. Definition of done (below) applies to every PR.
5. Run parallel agents only on non-overlapping directories (use git worktrees). Merge sequentially.
6. The tech lead reviews every PR, especially auth, RLS, audit, and SQL compilation.
   **Never "simplify" a security control** (RLS policies, audit hash chain, read-only SQL role,
   citation validator, auth checks). If one gets in your way, ask.
7. Log every architectural choice in `reference/DECISIONS.md`: the context, the alternatives, and why.

**Dependency order:** contracts → infra + data-gen (parallel) → ingestion + backend (parallel) →
retrieval → agent-core → frontend. Evals gate everything from week 2 onward.

## Definition of done

- [ ] Tests pass (`make test`)
- [ ] Lint and types pass (`make lint`, which runs ruff, mypy --strict, eslint, and tsc)
- [ ] Smoke eval passes (`make eval`; a stub until the gold set exists)
- [ ] README section for the touched area is updated
- [ ] No `TODO`s left in the diff
- [ ] No secrets, no real personal data, contracts unchanged (or the change is approved)

## Coding conventions

**General**
- Keep cloud calls (GCS, Cloud Tasks, Secret Manager, LLM providers) behind thin interfaces, so
  the `docker compose` path works with no GCP.
- Configuration comes from environment variables via Pydantic settings. In prod, secrets come
  from Secret Manager. Never hardcode keys, and never commit `.env` files (only `.env.example`
  with placeholders).
- Any fixture, sample, or test data must be synthetic and marked `SYNTHETIC`.

**Python**
- Python 3.11, fully typed, and `mypy --strict` clean. Format and lint with `ruff`.
- Use Pydantic models at every boundary (HTTP, queue messages, tool I/O, LLM output).
- SQL is always parameterized. Do not build SQL with f-strings or string concatenation. The
  only route from the LLM to SQL is the semantic layer compiler, which runs on the read-only role.
- The API sets `app.user_id` on every DB session, because RLS depends on it. Never use the
  owner/superuser connection in request paths.
- Ingest stages are idempotent and keyed on `(evidence_id, stage)`, so retries never duplicate rows.
- Evidence text is **data, not instructions**. Put it in delimited blocks in prompts and never
  let it change tool permissions.
- Tests are `pytest`, live under each package's `tests/` directory, and are named `test_*.py`.
  Tests that need Postgres use the `db` marker.

**TypeScript / React**
- Strict TS. Lint with `eslint`, test with `vitest`. Function components and hooks.
- API types are generated from `docs/contracts/openapi.yaml`. Do not hand-write them.
- Every claim rendered in the UI must link to its citation. Always show the demo-mode
  `SYNTHETIC DATA` banner.

**Git**
- Branch per ticket. Use small commits with imperative subjects (`feat(api): ...`,
  `fix(workers): ...`, `chore: ...`).

## Commands

```
make install   # create venv deps + web deps
make up        # start Postgres+pgvector and MinIO (docker compose)
make migrate   # alembic upgrade head
make test      # pytest + vitest
make lint      # ruff + mypy + eslint + tsc
make eval      # eval harness (stub until the gold set exists)
make down      # stop local services
```

On Windows without GNU make, use `mingw32-make` (from MSYS2) or run the commands in the Makefile directly.

## Ticket template

```markdown
## Ticket: <name>
Context: <link to relevant section of the project guide>
Contract: <schema / endpoint / interface it must satisfy>
Scope: <directories it may touch>
Out of scope: <explicitly forbidden changes>
Acceptance: <tests to write, eval metric to hit, demo behavior>
Done when: tests + types + smoke eval green, README updated
```
