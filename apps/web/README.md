# apps/web — CaseLens frontend

**Owner:** frontend agent.

React + TypeScript app built with Vite. This is currently a toolchain stub: it renders the
`SYNTHETIC DATA` banner and the `CaseLens` heading only. Tailwind, shadcn/ui, and TanStack Query
come in a later ticket.

## Requirements

- Node 24
- pnpm 9.15 (pinned via `packageManager` in `package.json`)

## Commands

Run from `apps/web`:

```
pnpm install --frozen-lockfile   # install deps from pnpm-lock.yaml
pnpm dev                         # start the Vite dev server
pnpm build                       # type-check (tsc -b) and build to dist/
pnpm preview                     # serve the production build locally
pnpm lint                        # eslint (typescript-eslint strict, react-hooks)
pnpm typecheck                   # tsc -b --noEmit
pnpm test                        # vitest run (jsdom)
```

## Notes

- TypeScript is strict (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, ...).
- TypeScript is pinned to the 6.0 line because `typescript-eslint` does not yet support 7.x.
- API types will be generated from `docs/contracts/openapi.yaml` in a later ticket. Do not
  hand-write them.
- Every page must show the demo-mode `SYNTHETIC DATA` banner.
