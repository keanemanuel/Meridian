# CLAUDE.md

Context for Claude Code working on this repository.

## What this is

An interview scheduler for IFF recruitment. Reads applicant preferences from a Google Form, solves a two-day interview timetable with constraint programming, allows manual override, and sends personalised emails.

Scale: **120 applicants × 2 interviews = 240 interviews** across 24 twenty-minute slots.

**Full specification: `docs/SPEC.md`.** Read the relevant section before implementing anything non-trivial. Requirements are numbered (FR-xx, NFR-xx) and constraints are numbered (C1–C8) — reference these IDs in commit messages, docstrings and test names.

## Non-negotiable invariants

Violating any of these is a bug regardless of what else works:

1. **Every applicant gets exactly two interviews.** Hard constraint. Never relaxed, never traded for a better objective value.
2. **The scheduling unit is the applicant's *choice*, not their division.** Decision variables are indexed `x[applicant, choice_index, panel, slot]`. Indexing by division silently collapses same-parent pairs (Media Marketing + Media Documentation both map to MEDMARDOC) into one interview. Do not "simplify" this.
3. **Nothing is guessed.** Bad or ambiguous input is rejected to a validation report. The pipeline never infers availability, never fills a default division, never assumes a missing decision means rejection.
4. **Manual edits are sacred.** Locked assignments are hard constraints on every subsequent solve. The solver never overwrites a human decision.
5. **No hard-coded schedule parameters.** Dates, times, interview duration, rooms, divisions, panels, weights all live in `config/*.yaml`. If you find yourself typing `20` or `"2014"` in `src/`, it belongs in config.
6. **Emails are idempotent.** Every send writes to the ledger before moving on. A re-run must never double-send.
7. **No PII in git.** `data/`, `runs/`, `credentials/`, `.env` are gitignored. Test fixtures are anonymised.

## Architecture rule

The core (`domain/`, `scheduling/`, `review/`) is pure — it imports no adapter, performs no I/O, and touches no network. It takes plain objects in and returns plain objects out. All I/O lives in `ingest/`, `export/` and `notify/` behind protocols.

This is what makes the solver testable without Google credentials. Do not import `gspread` or `googleapiclient` from anywhere in the core.

**Workspace support.** Every command accepts a `--workspace` flag. All data paths are namespaced under `data/workspaces/<workspace-name>/` — there is no shared, un-namespaced data directory. This isolates concurrent recruitment cycles (e.g. different divisions or intake rounds) from each other.

## Tech stack

```
Python 3.11+
ortools          CP-SAT solver — the primary scheduler
pydantic v2      domain models + config schema validation
pyyaml           config loading
typer + rich     CLI and terminal output
pandas           tabular wrangling in ingest/export
jinja2           email and HTML timetable templates
openpyxl         XLSX export
icalendar        .ics generation
gspread          Google Sheets read/write
google-auth      service account credentials
google-api-python-client   Gmail API sending
python-dotenv    secrets from .env

pytest           tests
ruff             lint + format
mypy             type checking (strict on src/iff_scheduler/domain and scheduling)
```

**No database in alpha.** At 120 applicants the data is a few hundred rows. CSV + YAML on disk is correct here — it's inspectable, diffable, and a committee member can open it in Excel. Do not add SQLite, Postgres or an ORM in alpha.

**Beta will add a database for Vercel deployment.** When the web UI lands, the CSV/YAML store is replaced with **PostgreSQL via Supabase** (Vercel-native, free tier covers this scale). All adapter I/O already sits behind protocols — the swap is `CsvSource` → `PostgresSource`, not a rewrite. Design domain models and data contracts now with this in mind: no path strings, no file handles, no `pd.read_csv` leaking into the core.

**No web framework in alpha.** CLI only. Beta target is **Next.js on Vercel** (frontend) + **FastAPI** (scheduler API). The core is pure Python with no I/O, so FastAPI wraps it as thin route handlers with no refactoring needed.

**Beta will add a multi-workspace tab UI.** Each tab is an isolated workspace with its own Sheet, data dir, and solve history. Tab creation flow: name + Google Sheet URL + group assignment. Workspace metadata moves from `workspaces.json` to Postgres in beta. File storage moves to Supabase Storage, namespaced by `workspace_id`.

## Repo structure

```
data/workspaces/
├── workspaces.json        ← workspace metadata (alpha)
└── <workspace-name>/
    ├── interim/
    ├── runs/
    └── last_ingested_row.txt
```

## Commands

```bash
iffsched ingest --source sheets|csv     # → applicants.clean.csv + validation_report.csv
iffsched check                          # Capacity Advisor — run before solving
iffsched solve [--solver cpsat|greedy]  # → runs/<timestamp>/
iffsched publish --run latest           # room / applicant / panel views
iffsched lock --from runs/latest/assignments.csv
iffsched notify invite --run latest --dry-run | --send
iffsched notify result --run latest --dry-run | --send
iffsched status                         # ledger summary
```

Development:

```bash
pytest                    # all tests
pytest tests/test_solver_constraints.py -v
ruff check src/ tests/
ruff format src/ tests/
mypy src/iff_scheduler/
```

## Build order

Implement in this sequence. Each milestone should be independently testable before moving on.

| M | Deliverable | Done when |
|---|---|---|
| M0 | `config/`, `settings.py`, `domain/models.py`, `domain/grid.py` | Changing `interview_duration_minutes` 20→30 regenerates the grid correctly; tests pass |
| M1 | `ingest/` — sources, normalise, validate | A real form export produces clean applicants + a validation report; edge cases E-01 to E-05 all caught |
| M2 | `scheduling/feasibility.py` | Per-division demand vs supply table; flags shortfall with a recommended panel count |
| M3 | `scheduling/solver_cpsat.py` | All 240 interviews placed; C1–C8 each have a passing test; solves in <60s |
| M4 | `export/` | Room, applicant and panel views; clashes highlighted red; conflict report |
| M5 | `review/` — locks + edit validator | A manual edit survives a re-solve; illegal edits rejected with a specific message |
| M6 | `notify/` — invites | Dry-run renders 120 correct emails; ledger prevents duplicates |
| M7 | `results/` + result emails | Score ingestion → decision column → three templates → audited send |
| M8 | `docs/RUNBOOK.md` | Someone who didn't build it can run the whole thing |
| M9 | Workspace support (`--workspace` flag, `workspace create`/`list`/`set-sheet` commands, namespaced data paths) | |
| M10 | Incremental Google Sheets ingest (`sheets_source.py`, watermark, `--force` flag) | |

`scheduling/solver_greedy.py` (MRV + backtracking + local search) is a lower-priority fallback for environments without OR-Tools. Same `Solver` protocol as CP-SAT.

## Conventions

- Type hints everywhere. `domain/` and `scheduling/` pass mypy strict.
- Pydantic models for every config file and every domain entity. Fail loudly on malformed config at load time, not deep in the solver.
- Every hard constraint (C1–C8) gets a dedicated test that constructs a scenario violating it and asserts the solver refuses.
- Every solve writes an immutable `runs/<timestamp>/` directory containing a config snapshot, so any published schedule can be reproduced exactly.
- Solver is deterministic: fixed `random_seed` in `solver.yaml`, same inputs → same output.
- Log decisions, not noise. When the solver forces a clash, log which applicant, which choice, and why no in-availability slot existed.

## Things that look like bugs but aren't

- **An applicant with both interviews in the same division.** Valid — they picked two roles under one parent (e.g. Creative + WebMaster). C8 tries to give them different panels.
- **The Capacity Advisor refusing to proceed.** Working as designed. 240 interviews ÷ 24 slots means ~12 panels are needed; if fewer are configured, stopping early is better than producing a schedule full of red clashes.
- **Red clashes in the output.** Sometimes unavoidable when an applicant ticks very few availability blocks. The solver minimises them; it cannot eliminate them.
- **`data/workspaces/` has no content on a fresh clone.** Correct — it's created on first `iffsched workspace create`.
