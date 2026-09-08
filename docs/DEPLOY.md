# Deploying Meridian to Vercel

One Vercel project serves both halves of the app from a single domain:
`frontend/` builds as the Next.js site, and `src/api/main.py` builds as a
Python serverless function that mounts the exact FastAPI app the CLI's
`api/` layer already defines. `vercel.json` at the repo root wires the two
together — every `/api/*` request goes to the Python function, everything
else goes to Next.js.

This assumes the three one-time steps are already done:

- [x] Supabase project created
- [x] Migration SQL run (the 4 tables: `workspaces`, `runs`, `assignments`, `send_ledger`)
- [x] Repo pushed to GitHub

---

## 1. Import the project

1. Go to [vercel.com](https://vercel.com) → **New Project** → import the
   `Meridian` repo.
2. **Framework Preset:** Next.js.
3. **Root Directory:** `frontend`.
   Vercel builds the Next.js app from here; the Python function is picked
   up separately via `vercel.json` at the repo root regardless of this
   setting.

## 2. Environment variables

Add these in **Project Settings → Environment Variables** before the first
deploy (Production scope; add to Preview too if you want PR previews to hit
the same backend).

| Variable | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon/service key |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Paste the **entire contents** of the service account JSON key |
| `GMAIL_OAUTH_CREDENTIALS` | Paste the **entire contents** of the Gmail OAuth client-secret JSON |
| `GMAIL_TOKEN_CACHE` | Paste the **entire contents** of a token already generated locally (see below) — or leave unset if you don't need the deployed app to send email yet |
| `GMAIL_SENDER_EMAIL` | The Gmail address invites/results send from |
| `NEXT_PUBLIC_API_URL` | `/api` |

> **Note on the credential variables — read before pasting.** Locally these
> three are file *paths* (`.env.example`); nothing in the code reads JSON
> content directly. `src/api/credentials_bootstrap.py` is what makes pasting
> the JSON into the **same variable names** work on Vercel: at cold start it
> checks whether a value looks like JSON (starts with `{`) rather than a
> path, writes it to a file under `/tmp`, and repoints the variable at that
> file — so `iff_scheduler` still just sees a path, unchanged. Use the exact
> names above, not `GOOGLE_SERVICE_ACCOUNT_JSON` or similar — the code has
> no variable by that name.
>
> **`GMAIL_TOKEN_CACHE` can't be generated on Vercel.** The first-run flow
> opens a real browser (`InstalledAppFlow.run_local_server`) — impossible
> inside a serverless function. Run the CLI's one-time OAuth setup
> (`docs/RUNBOOK.md`, "One-time OAuth2 setup for Gmail") on your own machine
> once, then paste the resulting `credentials/gmail_token.json` in here. As
> long as that token's refresh token stays valid, sending keeps working
> silently — no re-consent — because `GmailMailer` refreshes it automatically
> on expiry.

## 3. Deploy

Click **Deploy**. Vercel builds both targets from the one `vercel.json`.

## 4. Verify the live URL

Once the deploy finishes:

1. Open the deployed URL — the sidebar should load and show your Supabase
   workspaces (confirms `NEXT_PUBLIC_API_URL` and `SUPABASE_URL`/`SUPABASE_KEY`
   are wired correctly).
2. `curl https://<your-app>.vercel.app/api/health` → `{"status":"ok"}`
   confirms the Python function is live independent of the frontend.
3. Open a workspace and click through to a run's schedule view — this
   exercises the Postgres read path (`GET /api/workspaces/{id}/runs/...`).

---

## Known limitations of this deploy

Both of these are pre-existing gaps in the alpha→beta transition, not
something this configuration step fixes — flagging them so they don't look
like a broken deploy.

- **File writes still target local disk, even in Supabase mode.**
  `iff_scheduler/workspace.py` resolves every workspace path from
  `Path("data/workspaces")` — relative to the process's working directory,
  not the repo root — and `execute_solve`/ingest write `assignments.csv`,
  `conflicts.csv`, `metrics.json`, locks and the send ledger there
  unconditionally (CLAUDE.md: "the workspace's local directory skeleton is
  still laid down... regardless of backend"). Vercel's function filesystem
  is read-only outside `/tmp`, and `/tmp` doesn't persist between
  invocations. **Import Data, Solve and manual edits will likely fail or
  behave inconsistently on the deployed API** until these writes are
  redirected to Supabase Storage (the migration CLAUDE.md already calls out
  as beta scope) or to Postgres directly. Read-only views backed by
  Postgres (listing workspaces/runs, viewing a solved schedule) are not
  affected.
- **CORS is wide open** (`allow_origins=["*"]` in `src/api/main.py`) —
  fine for same-origin `/api` calls on the deployed domain, but worth
  tightening if the API is ever exposed to other callers.
