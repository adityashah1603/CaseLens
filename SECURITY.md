# Security

CaseLens is a portfolio proof of concept. **All data is SYNTHETIC**: the demo case, every person, plate, phone
number and file is fictional. Do not load real case data or real personal data into it.

This page maps each control to the CJIS Security Policy area it mirrors, and says plainly what is demonstrated
here versus what a real CJIS deployment would additionally require. CaseLens is **not** CJIS-certified.

## Controls

| Control | How it is implemented | CJIS area it mirrors | Where |
| --- | --- | --- | --- |
| Authentication | Firebase Auth ID tokens (RS256, verified against Google's keys: issuer, audience, expiry). Public demo visitors sign in anonymously and become the shared read-only demo viewer. Local-only HS256 dev tokens are refused outside `CASELENS_ENVIRONMENT=local`, and settings default to production | Identification and authentication | `apps/api/caselens_api/auth.py`, `settings.py` |
| Authorization (RBAC) | Per-case roles (lead > analyst > viewer). Non-members get 404 (a case's existence never leaks); a role that is too low gets 403. The demo user can never be promoted above viewer | Access control | `apps/api/caselens_api/deps.py`, `routes/` |
| Row-level security (second wall) | Postgres RLS is ENABLEd and FORCEd on every table. Requests run as `caselens_app` with `app.user_id` set; with no user set, nothing is visible. The API logs in as a dedicated non-superuser login and refuses to start as a superuser outside local | Access control | `apps/api/alembic/versions/0001_initial_schema.py`, `apps/api/caselens_api/db.py` |
| Read-only SQL for the agent | The LLM never writes SQL. It picks a semantic-layer operation that compiles to parameterized SQL with identifiers from YAML only. That SQL runs as `caselens_readonly` in a read-only, time-limited, rolled-back transaction, always scoped to the case | Access control / least privilege | `packages/semantic_layer/` |
| Audit | Every question, agent tool call, evidence view and mutation is written to an append-only `audit_log`. A BEFORE INSERT trigger hash-chains the rows; UPDATE, DELETE and TRUNCATE are blocked even for the owner. Leads can read their case's log; admins can verify the whole chain | Auditing and accountability | migrations 0001–0003, `routes/audit.py` |
| Evidence integrity | SHA-256 at upload, re-checked by workers before every stage and before every download. A mismatch blocks the file (409) and is audited. Originals are immutable (no UPDATE or DELETE grants); signed URLs live 5 minutes and are `no-store` | Media protection | `workers/caselens_workers/context.py`, `routes/evidence.py` |
| Citation validator | Every claim must cite a source a tool actually returned in that run (ids, evidence and locator are checked against the run's ledger; quotes must appear word for word). Otherwise the answer is `insufficient_evidence` | Information integrity | `packages/agent/caselens_agent/validator.py` |
| Prompt-injection defense | Evidence text, tool errors and model-written descriptions reach the LLM only inside delimited evidence blocks, framed as data. Tool permissions are fixed and read-only, apart from `flag_conflict`. The case id is never a tool input. Demo users' conflicts are never stored | (modern addition) | `packages/agent/` |
| Secrets | Never in code or git (history scanned). `.env` files are gitignored, with only `.env.example` committed. Hosted secrets live in the platform's secret store (Space secrets); production on GCP would use Secret Manager | Configuration management | `.gitignore`, `infra/` |
| Data minimization | The LLM receives only the top reranked passages it needs, trimmed to the matching window. Errors never echo request bodies or keys | Information protection | `packages/agent/caselens_agent/sources.py`, `router.py` |
| Availability limits | Concurrent questions are capped, the connection pool is sized to match, demo visitors are rate-limited per IP, and uploads are size-capped while streaming | System and communications protection | `apps/api/` |

## What this demo does not do (and real CJIS use would require)

- **MFA:** not enforced (tech-lead decision for a demo). CJIS requires advanced authentication for access to
  criminal justice information.
- **Hosting:** the public demo runs on free tiers (Hugging Face Spaces, Neon, Firebase). There is no
  customer-managed encryption keys (CMEK), private networking, WAF (Cloud Armor), or data-residency guarantees. The
  GCP Terraform in `infra/terraform` sketches the production path (Cloud Run, Cloud SQL private IP, GCS with
  retention lock, KMS, Secret Manager).
- **LLM data handling:** the free Gemini tier may use prompts to improve its models. That is acceptable only
  because every byte is SYNTHETIC. Real use needs a zero-retention contract or a self-hosted model (the model
  router supports swapping providers).
- **Audit anchoring:** the hash chain detects edits by anyone without database-owner rights. An owner could
  drop the triggers and rewrite the chain, so real use should periodically export the chain head to external
  write-once storage.
- **Personnel, physical and agency controls:** background checks, security awareness training, physical
  security, and formal agency agreements (CJIS Security Addendum) are outside the scope of software.
- **Tamper-resistant evidence storage:** the demo serves baked-in files. Production would use GCS object
  versioning plus a retention lock.

## Reporting

This is a personal portfolio project. Please report security issues by opening a GitHub issue without
exploit details, and the maintainer will follow up.
