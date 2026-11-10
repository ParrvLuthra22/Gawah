# Integration issues

Findings from System 2 about code that other owners maintain (`system1/`, `system3/`, `web/`).
System 2 does not edit those directories; each item says who needs to act.

## Checks that came back clean (2026-11-10, after merging System 3)

- `pytest` (engine + contract): 425 passed, 1 skipped before this work; `ruff check .` clean.
- System 3 tests (Counsel, Python 3.14 and 3.12): 77 passed.
- `web`: `npx vitest run` 28 passed; `npm run build` succeeds.
- **No key can reach a browser.** `grep` over `web/` (excluding `node_modules` and `.next`) for
  `NEXT_PUBLIC`, `SYSTEM3_KEY`, `X-Gawah-Key` and `process.env`: no `NEXT_PUBLIC_` variable exists;
  `SYSTEM3_KEY` is read only in the server route `web/app/api/[...segments]/route.ts` (and tests);
  the only other `process.env` use is `NODE_ENV` in two page components. Nothing sets a key in a
  client component. (System 2's own `.env.example` had an unused `NEXT_PUBLIC_ENGINE_URL`; removed.)

## Open

### 1. `npm start` binds to loopback (web, Navya)
`web/package.json` has `"start": "next start --hostname 127.0.0.1"`. On Render the service is
unreachable from outside the container. Workaround in `render.yaml`: the start command is
`cd web && npx next start --hostname 0.0.0.0 --port $PORT`. Suggested fix: `"start": "next start"`
(Next reads `HOSTNAME`/`PORT`) or `--hostname 0.0.0.0` behind an env switch.

### 2. The hosted dashboard has no user login (web + Counsel, Navya)
`web/README.md` says a shared deployment "requires its own user access controls". The web server
adds `SYSTEM3_KEY` to every allow-listed request for any visitor, including `POST /api/disputes/sync`
(calls PayPal), `POST /api/evidence/{id}/assemble` and `POST /api/counsel/{id}/analyze` (calls
Groq). Same-origin checks stop cross-site posts, not a visitor using the site. For the hackathon
demo this is accepted (sandbox data only, unlisted URL), but anyone with the URL can spend the
Groq/PayPal sandbox quota. Suggested: a shared demo password (middleware) before wider sharing.

### 3. Resolution actions are disabled when hosted (web, by design)
`route.ts` allows resolution POSTs only when `NODE_ENV === "development"` and the host is
localhost, so the hosted Resolution Center is read-only. `ENABLE_SANDBOX_RESOLUTION=false` is set
in `render.yaml` to match. Not a bug; recorded so nobody expects refunds from the hosted UI.

### 4. Untracked `web/AGENTS.md`
`npm ci`/the Next build leaves an untracked `web/AGENTS.md` in the working tree. System 2 did not
commit it. Navya: commit it or add it to `.gitignore`.
