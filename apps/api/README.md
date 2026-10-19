# apps/api: CaseLens API

FastAPI service. Owner: backend agent. API contract: `docs/contracts/openapi.yaml`. Schema + RLS:
`alembic/versions/`.

```
uvicorn caselens_api.main:app --reload   # GET /healthz
```
