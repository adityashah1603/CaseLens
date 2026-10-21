# apps/web: CaseLens frontend

**Owner:** frontend agent.

React 19 + TypeScript (strict) on Vite, Tailwind v4 with copied-in shadcn/ui components,
TanStack Query, React Router, pdf.js, Firebase Auth (modular SDK, lazy-loaded). Every page shows the `SYNTHETIC DATA` banner.

## Requirements

- Node 24
- pnpm 9.15 (pinned via `packageManager` in `package.json`)

## Run it

```
cp .env.example .env.local        # gitignored; fill in the dev tokens
pnpm install --frozen-lockfile
pnpm dev                          # http://localhost:5173
```

| Variable | Purpose |
| --- | --- |
| `VITE_API_BASE_URL` | API origin. Unset or blank: `http://127.0.0.1:8000` in dev, and same-origin relative URLs in a production build (the API serves the SPA) |
| `VITE_DEV_TOKEN_LEAD` / `_ANALYST` / `_VIEWER` | Local dev tokens from `python -m caselens_api seed-dev-users` (dev builds only) |
| `VITE_FIREBASE_API_KEY`, `_AUTH_DOMAIN`, `_PROJECT_ID`, `_APP_ID` | Firebase web config. All four set: the "Try the demo" button. Any blank: hidden |

Vite also reads `.env.local` in `vite build`. For a same-origin production build, leave
`VITE_API_BASE_URL` blank there (or keep the dev value in `.env.development.local`), or build with
`VITE_API_BASE_URL= pnpm build`. The Firebase web config is public by design (it identifies the
project, it is not a secret); the dev tokens never reach a production bundle (`pnpm check:bundle`).

In dev the API must allow CORS from the dev origin (with the `Authorization` header), and the
object store must allow CORS GETs for the signed content URLs (the browser follows the 307 from
`/evidence/{id}/content`). In production everything is same-origin, signed URLs included
(`/storage/...`), so no CORS is needed.

## Commands

```
pnpm gen:api     # regenerate src/api/schema.d.ts from docs/contracts/openapi.yaml
pnpm lint        # eslint (typescript-eslint strict type-checked, react-hooks)
pnpm typecheck   # tsc -b --noEmit
pnpm test        # vitest + Testing Library + MSW (jsdom)
pnpm build       # tsc -b && vite build
pnpm check:bundle  # after build: fail if dist/ contains a JWT-looking string or a dev token
```

## Layout

| Path | Contents |
| --- | --- |
| `src/api/schema.d.ts` | **Generated** contract types. Never edit by hand; run `pnpm gen:api`. |
| `src/api/client.ts` | Typed `openapi-fetch` client with the bearer token; `ApiError` from RFC 9457 problems |
| `src/api/content.ts` | Evidence originals: fetch `/content` with the token, follow the 307, 409 becomes `IntegrityError`. PDF/CSV come back as a Blob; audio/video resolve to the signed URL |
| `src/api/sse.ts` | Fetch-based SSE reader for `/ingest/events` (EventSource cannot send auth headers), reconnects with `Last-Event-ID` |
| `src/api/queries.ts` | TanStack Query hooks for every contract route the UI uses |
| `src/auth/` | `AuthProvider` interface, dev sign-in, Firebase anonymous demo sign-in, tab-scoped session |
| `src/features/ask/` | Ask panel, answer rendering (claims, citations, conflicts, confidence, open questions), agent trace |
| `src/features/viewer/` | Evidence viewer: PDF page + bbox (pdf.js), audio span, video frame + bbox, CSV row |
| `src/features/timeline/`, `entities/`, `evidence/`, `audit/`, `admin/` | The other workspace tabs and the admin page |
| `src/test/` | MSW mock API and SYNTHETIC fixtures typed by the generated schema |

## Screens

1. **Sign-in.** With the Firebase config, a "Try the demo (read-only)" button signs in anonymously
   (the API maps that user to the shared read-only demo viewer), explains that all data is
   SYNTHETIC, and shows an "About this demo" panel. After sign-in a visitor whose case list holds a
   single case goes straight into it (the Cases link still shows the list). In dev builds only
   (`import.meta.env.DEV`) it also lists the dev lead, analyst and viewer identities (tokens from
   `.env.local`) and offers a paste-a-token field. The session lives in `sessionStorage` only. A
   401 from the API signs the user out with a notice (after one token refresh for Firebase).
2. **Cases.** Role, evidence count, ingest status, last activity.
3. **Case workspace.** Tabs on the left, evidence viewer on the right (stacked below on narrow screens).
   - **Ask.** Question box, suggested questions, elapsed timer and "Stop waiting" while the run is
     in flight (10 to 60 s on the free-tier LLM), recent runs. The demo case shows a collapsible
     "About this demo" panel in its header. Answers show a summary, claim cards with numbered
     citation chips, conflicts side by side, confidence, open questions, a source list and the
     tool trace (plan, inputs, SQL, latency, tokens, cost). A claim (or conflict side) whose
     citation ids do not resolve is withheld, and an `answered` result with no resolvable claim is
     not shown. Uncited text is never rendered as a supported claim.
   - **Evidence viewer.** PDF: pdf.js renders the cited page and outlines the bbox. The bbox is in
     PDF points with a top-left origin (as PyMuPDF and the OCR stage write it), and it is mapped
     through the pdf.js viewport (y flip, scale, rotation). Audio and video: the element is pointed
     at the short-lived signed URL (from the `/content` 307), so the browser streams it with Range
     requests and seeking does not download the whole file. A fresh URL is requested whenever a
     citation opens, and once more if the element fails to load it (expired while open). Audio:
     seeks to `start_ms`, shows the speaker and span, and "Play cited span" stops at `end_ms`. Video: paused at `start_ms` with
     the bbox (original frame pixels) scaled to the rendered size. CSV: the row whose `row_id`
     matches, with three neighbours each side, skipping the `# SYNTHETIC` comment line. A 409 shows
     an integrity-failure notice and nothing else.
   - **Timeline.** Events in time order, filter by entity and time window, each source opens the viewer.
   - **Entities.** Fuzzy search, type filter, list or graph view (a small deterministic SVG force
     layout), detail with aliases, attributes, mentions (open the viewer), merge links and
     "Revert merge" (lead and analyst, with a confirm step), and "Show on timeline".
   - **Evidence.** Files by modality, per-stage pipeline chips updated live over SSE, chain of
     custody, and an upload dropzone with a custody note and recording start (lead and analyst).
   - **Audit** (lead only). Filterable table; adjacent rows' `prev_hash` and `row_hash` are
     cross-checked on the page.
4. **Admin** (when `GET /v1/me` says `is_admin`). Cross-case audit log and full chain verification.

Role gating in the UI hides mutating actions; the server stays the authority (403s are shown as such).

## Firebase demo sign-in

`src/auth/firebase.ts` implements the `AuthProvider` with the modular SDK (`firebase/app` +
`firebase/auth` only). The SDK is imported dynamically on first use, so it is a separate chunk
(about 46 kB gzipped) that is not part of the first paint and never loads without the config.

- Config: the four `VITE_FIREBASE_*` variables, each read individually (never `import.meta.env`
  as a whole object). Persistence: `browserSessionPersistence` (ends with the tab).
- Sign-in: `signInAnonymously`. Enable the Anonymous provider in the Firebase console and add the
  Space's domain to the authorized domains.
- Token: the bearer token is the Firebase ID token from `getIdToken()`, which the SDK caches and
  refreshes near its ~1 h expiry. On a 401 the client forces one refresh (`getIdToken(true)`) and
  retries the request once; a second 401 signs out. Sign-out also signs out of Firebase.

## Notes

- TypeScript is pinned to the 6.0 line because `typescript-eslint` does not yet support 7.x.
  `openapi-typescript` 7.13 declares a TypeScript 5 peer but generates correctly with 6.0.
- Times are shown as written, with their own UTC offset, so evidence times are not converted to
  the viewer's zone. Filter inputs (`datetime-local`) use the viewer's zone.
