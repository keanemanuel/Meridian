# Deploying Meridian

Beta runs on **two** hosts, not one:

| Half | Host | What it is |
|---|---|---|
| `frontend/` | **Vercel** | The Next.js site. Static-ish, tiny, deploys in seconds. |
| `src/api/` | **Railway** | The FastAPI scheduler API — the exact app the CLI's `api/` layer defines, running the CP-SAT solver. |

Postgres is **Supabase** (unchanged), read/written by the Railway service.

## Why the split

`ortools` (the CP-SAT solver, CLAUDE.md invariant: never traded away) is
~300 MB installed. Vercel's Python serverless functions cap at 250 MB, so
the API can't live there. Railway has no function-size limit and a
writable, optionally-persistent filesystem — which also clears the old
"function filesystem is read-only outside `/tmp`" problem that broke
Import / Solve / manual edits on the previous single-Vercel deploy.

The frontend still calls `/api/...` paths; the only thing that changes is
where those resolve. `NEXT_PUBLIC_API_URL` points the browser at the
Railway URL instead of the same origin.

```
Browser ──▶ Vercel (Next.js)            NEXT_PUBLIC_API_URL
        └─▶ Railway (FastAPI + CP-SAT) ──▶ Supabase (Postgres)
```

This assumes the one-time setup is done:

- [x] Supabase project created
- [x] Migration SQL run (`supabase/migrations/001_initial.sql` — `workspaces`, `runs`, `assignments`, `send_ledger`)
- [x] Repo pushed to GitHub

---

## 1. Deploy the API to Railway

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → pick `Meridian`.
2. **Root Directory:** leave as the repo root (`/`). `railway.json` drives
   the build: Nixpacks installs from the root `requirements.txt` (mirrored
   from `pyproject.toml`) and starts the service with
   `uvicorn api.main:app --host 0.0.0.0 --port $PORT --app-dir src`.
   `--app-dir src` puts the sibling `api/` and `iff_scheduler/` packages on
   the path without an editable install; `.python-version` pins Python 3.11.
3. **Environment variables** (Railway → the service → **Variables**):

   | Variable | Value |
   |---|---|
   | `SUPABASE_URL` | Your Supabase project URL |
   | `SUPABASE_KEY` | Your Supabase anon/service key |
   | `GOOGLE_SERVICE_ACCOUNT_FILE` | Paste the **entire contents** of the service-account JSON key |
   | `GMAIL_OAUTH_CREDENTIALS` | Paste the **entire contents** of the Gmail OAuth client-secret JSON |
   | `GMAIL_TOKEN_CACHE` | Paste the **entire contents** of a token generated locally (see below) — or leave unset until the deployed app needs to send email |
   | `GMAIL_SENDER_EMAIL` | The Gmail address invites/results send from |

   > **The three credential variables are file *paths* locally** (`.env.example`)
   > and nothing reads JSON content directly.
   > `src/api/credentials_bootstrap.py` bridges this: at startup it detects a
   > value that looks like JSON (starts with `{`) instead of a path, writes
   > it to `/tmp`, and repoints the variable at that file — so
   > `iff_scheduler` still just sees a path. Use the exact names above.
   >
   > **`GMAIL_TOKEN_CACHE` can't be minted in the cloud.** The first-run flow
   > opens a real browser (`InstalledAppFlow.run_local_server`). Run the
   > CLI's one-time OAuth setup (`docs/RUNBOOK.md`, "One-time OAuth2 setup
   > for Gmail") on your own machine once, then paste the resulting
   > `credentials/gmail_token.json` here. `GmailMailer` refreshes it
   > automatically on expiry, so sending keeps working with no re-consent
   > as long as the refresh token stays valid.

4. **(Recommended) Persist run artefacts.** In Supabase mode, workspaces,
   runs, assignments and the send ledger live in Postgres, but each solve
   also writes an immutable `data/workspaces/<ws>/runs/<timestamp>/`
   directory (config snapshot, `solve.log`, `schedule.xlsx`, HTML views —
   CLAUDE.md: "the workspace's local directory skeleton is still laid down
   regardless of backend"). Railway's filesystem is wiped on each redeploy.
   Attach a **Volume** mounted at `/app/data` to keep those artefacts
   across deploys. Read paths the web UI uses (list workspaces / runs, view
   a solved schedule) are Postgres-backed and work without the volume; only
   the downloadable `publish` outputs and the reproducibility snapshot need
   it.

5. Deploy. When it's green, note the public URL
   (**Settings → Networking → Public Domain**, e.g.
   `https://meridian-api-production.up.railway.app`).

6. Verify the API alone:

   ```
   curl https://<your-railway-domain>/api/health      # → {"status":"ok"}
   ```

---

## 2. Deploy the frontend to Vercel

There is **no `vercel.json`** — Vercel hosts only the Next.js app and
zero-config detection handles the build. All configuration is in the
project settings.

1. [vercel.com](https://vercel.com) → **New Project** → import `Meridian`.
2. **Settings → Build & Deployment → Root Directory:** set to `frontend`
   and save. This is what points Vercel's Next.js detection at the app;
   without it the build runs from the repo root, finds no framework, and
   every route 404s.
3. **Framework Preset:** Next.js (auto-detected once Root Directory is set;
   leave Build/Output/Install commands on their defaults).
4. **Settings → Environment Variables** (Production + Preview):

   | Variable | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | The Railway public URL **including `/api`**, e.g. `https://meridian-production-f9c9.up.railway.app/api` — no trailing slash |
   | `SUPABASE_URL` | Same as Railway (only if a frontend component reads Supabase directly — none today) |
   | `SUPABASE_KEY` | Same as Railway (same caveat) |

   `NEXT_PUBLIC_API_URL` is read in `frontend/lib/api.ts` as the base for
   every call; each path there already omits the `/api` prefix, so the
   value must carry it. It is baked in at **build** time — set it before
   the first deploy, and after changing it use **Redeploy** without the
   build cache.

5. Deploy.

---

## 3. Verify the live site

1. Open the Vercel URL — the sidebar loads and lists your Supabase
   workspaces (confirms `NEXT_PUBLIC_API_URL` reaches Railway and Railway
   reaches Postgres).
2. Open a workspace → open a run → the schedule view renders (Postgres
   read path, `GET /api/workspaces/{id}/runs/...`).
3. Run **Check** then **Solve** from the workspace page. Solve exercises
   CP-SAT on Railway and writes a run row to Postgres. A short spinner then
   a "Solved: 240/240 interviews placed…" toast means the whole chain is
   live.

---

## Local development is unchanged

```
scripts/start_api.sh          # uvicorn on :8000
cd frontend && npm run dev     # Next.js on :3000
```

To run fully local, leave `NEXT_PUBLIC_API_URL` **unset** in
`frontend/.env.local` (or delete the file): `next.config.ts` then proxies
`/api/*` to `:8000` in development only (the rewrite returns `[]` outside
`NODE_ENV=development`, so a Vercel build carries no proxy rule). If
`frontend/.env.local` instead points at the Railway URL, `npm run dev`
talks to the deployed API directly and `start_api.sh` isn't needed.
`frontend/.env.local` is gitignored and never reaches Vercel — the
deployed value lives only in the Vercel project's env vars.

---

## Notes / limitations

- **CORS is wide open** (`allow_origins=["*"]` in `src/api/main.py`).
  Required now that the frontend and API are different origins; tighten to
  the Vercel domain if the API is ever exposed more broadly.
- **CLI still works against local files.** `iffsched solve --workspace <name>`
  on a laptop is unaffected by any of this and needs no Supabase or Railway
  config — it runs file-mode as in alpha.
- **Railway cold starts.** The hobby plan may sleep an idle service; the
  first request after a sleep waits a few seconds for `ortools` to import.
  The healthcheck on `/api/health` keeps deploys honest but doesn't prevent
  sleeping — upgrade the plan if that matters for event day.
