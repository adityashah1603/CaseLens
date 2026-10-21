# infra/space: free public demo (Hugging Face Space + Neon + Firebase)

The $0 hosting path from decision D-066: **one Docker container** on a free Hugging Face Docker
Space serves the FastAPI API and the built web app, with the fastembed models and the 18
SYNTHETIC demo evidence files baked in, and talks to a free **Neon** Postgres 16 + pgvector.
Visitors sign in with **Firebase anonymous auth** and act as the shared read-only demo viewer.
Workers are not deployed: the demo case is ingested locally and copied into Neon. The GCP
Terraform in `infra/terraform` stays the documented production path.

| File | What |
| --- | --- |
| `../../Dockerfile.space` | the image: node build of `apps/web` (+ `check:bundle`), Python runtime packages only (no pdf/audio/video/extract extras, no torch), models pre-downloaded, non-root uid 1000, uvicorn on `:7860`, 1 worker |
| `../../.dockerignore` | allow-list build context (no `.env*`, keys, caches, `node_modules`, `dist`) |
| `constraints.txt` | pins the image's Python packages to the versions tested locally (incl. `fastembed`, so queries embed exactly like the ingested chunks) |
| `export_evidence.py` | copies the demo case's originals from MinIO to `evidence/<key>` (gitignored build input), SHA-256 verified, plus content-type sidecars and `manifest.json` |
| `load_neon.py` | migrates the target DB, creates the API login, copies the demo case from the local dev DB |
| `stage.py` | assembles `build/` (gitignored): the Space repo = build context + `Dockerfile` + Space card |

## How the container works

* `CASELENS_STORAGE_BACKEND=local`: evidence is read from `/app/evidence` by object key. The
  content endpoint still checks membership, re-hashes the file, writes `evidence.view` to the
  audit log, then 307-redirects to `/storage/<token>`, an expiring HMAC-signed URL (not part of
  the API contract; see `apps/api/README.md`). Range requests work, so audio/video seek.
* `CASELENS_WEB_DIST=/app/web`: the SPA is served at `/` (index.html fallback); `/v1`, `/healthz`
  and `/storage` are never shadowed. The web build uses same-origin relative API URLs.
* `CASELENS_ANONYMOUS_DEMO=true`: a verified Firebase token with
  `firebase.sign_in_provider == "anonymous"` is served as the user `demo:viewer` (is_demo, viewer
  of the demo case only). Every demo restriction applies: viewer role, no stored conflicts,
  per-IP `/ask` limit, and each visitor sees only the runs it created in this container process.
  Any other Firebase sign-in is a normal user with no case memberships.
* `CASELENS_ENVIRONMENT=prod`: no dev tokens; the API refuses to start unless its DB login is
  NOSUPERUSER and NOBYPASSRLS.

## Environment of the Space

Baked into the image (no action): `CASELENS_ENVIRONMENT=prod`, `CASELENS_STORAGE_BACKEND=local`,
`CASELENS_LOCAL_STORAGE_ROOT`, `CASELENS_BUCKET`, `CASELENS_WEB_DIST`, `CASELENS_ANONYMOUS_DEMO=true`,
`CASELENS_MODEL_CACHE_DIR`, `HF_HUB_OFFLINE=1`, `CASELENS_LLM_PROVIDER=gemini`,
`CASELENS_TRUSTED_PROXY_HOPS=1`, `CASELENS_ASK_MAX_CONCURRENT=2`.

Set in the Space settings:

| Name | Kind | Value |
| --- | --- | --- |
| `CASELENS_API_DATABASE_URL` | secret | `postgresql://caselens_api_login:<password>@<neon direct host>/<db>?sslmode=require` |
| `CASELENS_STORAGE_SIGNING_SECRET` | secret | 48+ random characters (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `CASELENS_GEMINI_API_KEY` | secret | Google AI Studio key |
| `CASELENS_FIREBASE_PROJECT_ID` | variable | Firebase project id |
| `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_APP_ID` | variables | Firebase web-app config (public identifiers; Space variables are passed to the Docker build as build args) |

Never give the Space the Neon **owner** URL: the API only needs the least-privilege login.

## Deploy steps (once the accounts exist)

1. **Firebase**: create a project, enable Authentication > Sign-in method > **Anonymous**, add a
   Web app and note its config. Under Authentication > Settings > Authorized domains add
   `<hf-user>-<space-name>.hf.space`.
2. **Neon**: create a project on **Postgres 16** (pick the region closest to the Space, e.g. US
   East). Copy the owner connection string for the **direct** endpoint (the host *without*
   `-pooler`).
3. **Gemini**: create a free API key in Google AI Studio.
4. **Build inputs** (local stack up, demo case ingested):
   ```
   .venv/Scripts/python infra/space/export_evidence.py
   ```
5. **Load Neon** (bash; values come from your password manager, never from a file in the repo):
   ```
   export CASELENS_PROD_OWNER_DATABASE_URL='postgresql://<owner>:<pw>@<direct host>/<db>?sslmode=require'
   export CASELENS_PROD_API_DB_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   .venv/Scripts/python infra/space/load_neon.py
   ```
   Keep the API password: it goes into `CASELENS_API_DATABASE_URL`. Re-running is safe (it
   rotates the API password, upserts the case and replaces its derived rows).
6. **Hugging Face**: create a Space (SDK **Docker**, blank, hardware **CPU basic** (free)). Add the
   secrets and variables above.
7. **Push**:
   ```
   .venv/Scripts/python infra/space/stage.py
   .venv/Scripts/hf auth login            # a write token; huggingface_hub is already in the venv
   .venv/Scripts/hf upload <hf-user>/<space-name> infra/space/build . --repo-type space
   ```
   The Space builds the image (about 10 minutes) and starts it.
8. **Check**: `https://<hf-user>-<space-name>.hf.space/healthz` answers `{"status":"ok"}`; the
   app opens, signs in anonymously, shows the demo case, and an evidence citation opens its file.

## Rehearsal (no accounts)

`load_neon.py` was run against a throwaway `pgvector/pgvector:pg16` container whose owner login is
**not** a superuser (`CREATEDB CREATEROLE BYPASSRLS`, the shape of Neon's owner), and the image ran
against it with a test-only verifier mounted over the entry point (never in the image). See the
ticket report for the commands; to repeat it, create such a role and database, pre-create the
`vector` extension as the bootstrap superuser (Neon allows `CREATE EXTENSION vector` for its
owner), and point `CASELENS_PROD_OWNER_DATABASE_URL` at it.

## Neon-specific notes

* **Direct endpoint only.** The API sets `ROLE` and `app.user_id` per pooled connection (session
  level). Neon's `-pooler` endpoint (PgBouncer, transaction mode) would hand that session state
  to other clients; use the direct host in both URLs.
* **Non-superuser owner.** Migrations assume they can create `caselens_worker` and
  `caselens_definer` with BYPASSRLS and hand `caselens_definer` the SECURITY DEFINER functions.
  `load_neon.py` therefore (a) probes `CREATE ROLE ... BYPASSRLS` in a rolled-back transaction
  first, (b) creates the four group roles itself (so the owner holds ADMIN on them) and makes the
  owner a member of `caselens_definer`, which `ALTER FUNCTION ... OWNER TO` and the following
  `GRANT EXECUTE` require of a non-superuser, and (c) grants `caselens_definer` CREATE on schema
  `public` for the upgrade only, revoking it afterwards and checking that it is gone. The data copy
  runs as the owner, which BYPASSRLS lets write through the forced RLS policies; the script checks
  afterwards that RLS is still forced on every table.
* **If the owner cannot create BYPASSRLS roles** (the probe fails): the RLS design needs a
  BYPASSRLS `caselens_definer` (the policy helpers read `case_members` without recursing into its
  own policies), so do not work around it in the schema. Options: ask Neon support to grant the
  attribute or create the two roles; use another free Postgres 16 + pgvector whose owner has it
  (check that its owner role can create BYPASSRLS roles before switching); or, as a last resort, run Postgres
  inside the container, loading the demo case at boot (the audit log then resets on every
  restart). Any of these is a tech-lead decision.
* **Scale to zero.** The free compute suspends after a few idle minutes and drops connections;
  the pool re-checks connections on checkout and reconnects (the first request after idle pays the
  cold start, about a second). The startup login check waits up to 30 s.
* **Limits.** The demo case is about 25 MB in Postgres (0.5 GB free). The pool holds at most
  `2 * CASELENS_ASK_MAX_CONCURRENT + 4` = 8 connections.
* **Roles created by SQL** (`caselens_*`) are cluster roles of the Neon project; Neon's console
  password reset does not apply to them (re-run `load_neon.py` to rotate the API password).

## Hugging Face notes

* Free CPU Spaces sleep after 48 h without traffic (cold start about a minute: the container
  starts and loads the models). A restart clears the in-process `/ask` limiter and each visitor's
  run list (the runs stay in the database).
* `CASELENS_TRUSTED_PROXY_HOPS=1` takes the client address from the right-most
  `X-Forwarded-For` entry (the one HF's proxy appends). If HF puts more than one proxy in front,
  that entry is a proxy address and all visitors share one `/ask` budget: more restrictive, never
  spoofable. Verify the header once after the first deploy.
* The Space page embeds the app in an iframe from `*.hf.space`; the API and SPA are same-origin
  there, so no CORS configuration is needed.


## Cloud Run (current, D-067)

Hugging Face Docker Spaces need PRO, so the same image runs on Cloud Run's free tier in project
`caselens-72505` (region `us-west1`, next to Neon). gcloud runs in Docker with the login kept in the
`caselens-gcloud` volume (`docker run --rm -v caselens-gcloud:/root/.config
gcr.io/google.com/cloudsdktool/google-cloud-cli:stable gcloud ...`).

* Image: built from `Dockerfile.space` with the `VITE_FIREBASE_*` build args, pushed to
  `us-west1-docker.pkg.dev/caselens-72505/caselens/caselens:<git sha>`. Artifact Registry is free up to
  0.5 GB and the image is ~494 MB: **delete the previous image before pushing a new one.**
* Secrets (Secret Manager, readable only by `caselens-run@caselens-72505.iam.gserviceaccount.com`):
  `caselens-api-database-url`, `caselens-storage-signing-secret`, `caselens-gemini-api-key`.
* Service: `gcloud run deploy caselens --image <img> --region us-west1 --port 7860 --memory 2Gi
  --cpu 1 --min-instances 0 --max-instances 1 --concurrency 10 --timeout 300 --allow-unauthenticated
  --service-account caselens-run@... --set-env-vars CASELENS_FIREBASE_PROJECT_ID=caselens-72505
  --set-secrets CASELENS_API_DATABASE_URL=caselens-api-database-url:latest,...`
* Cloud Run reserves paths ending in `z`, so `/healthz` is answered by Google (404) on run.app; use `/`.
* Cost guard: scale to zero, max 1 instance, no CPU boost, $1 budget alert on the billing account.
* Add the run.app host to Firebase Authentication > Settings > Authorized domains.
