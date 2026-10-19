# apps/api: CaseLens API

FastAPI service. Owner: backend agent. API contract: `docs/contracts/openapi.yaml`. Schema + RLS:
`alembic/versions/`.

```
uvicorn caselens_api.main:app --reload   # GET /healthz
```

## Migrations

The schema, RLS policies, roles and the audit hash chain are all in `alembic/versions/0001_initial_schema.py`.
That file is a contract: change it only with tech-lead approval.

```
make migrate                                            # upgrade the dev DB (CASELENS_DATABASE_URL)
CASELENS_TEST_DATABASE_URL=... pytest apps/api/tests    # live migration + RLS + audit-chain tests
```
