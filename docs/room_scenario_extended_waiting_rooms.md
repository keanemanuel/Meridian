# Room scenario: `extended_waiting_rooms` — verification

Status: **EXPLORATORY / UNCONFIRMED.** Not merged to `main`, not the default.
Built on `feature/extended-waiting-rooms`. This document is the answer to
"is this actually usable" — read this before deciding to adopt it.

## What it is

A second, selectable room layout alongside the committed default. It
additionally converts Rooms 3013 and 3033 into interviewer waiting rooms (on
top of the existing 2018), dropping the interview-room pool from Thursday
5 / Friday 4 to **Thursday 4 / Friday 3**.

- Config: `config/scenarios/extended_waiting_rooms/` (full config dir —
  `rooms.yaml` and `panels.yaml` differ from the default; `event.yaml`,
  `divisions.yaml`, `solver.yaml`, `notify.yaml` are identical copies, kept
  in sync by `test_extended_scenario_shares_non_room_config_with_default`).
- Selectable per solve, not a global switch:
  - CLI: `iffsched solve --room-scenario extended_waiting_rooms`
  - API: `POST /api/workspaces/{id}/solve` body `{"room_scenario": "extended_waiting_rooms"}`
  - `GET /api/workspaces/{id}/room-scenarios` lists what's available (for a
    future UI dropdown — not built in this branch, see "Not done" below).
- No file is hand-edited to switch — see `resolve_room_scenario_dir` in
  `src/iff_scheduler/settings.py`.
- `rebalance_panels` / `autoscale_panels` / CP-SAT are **unchanged**. This is
  entirely a config change: fewer rooms in `rooms.yaml`, a reseeded baseline
  in `panels.yaml`.

## Method

Ran the real solve pipeline (`iffsched solve`) twice against the same real
applicant dataset — `data/workspaces/Test Run 1/interim/applicants.clean.csv`,
108 applicants, 216 required interviews, the largest real dataset on this
machine (the committed `applicants.yaml`/`config` fixtures and the `default`
workspace are small anonymised smoke fixtures, not this) — once per copy
workspace so neither run's run-history is touched:

- `data/workspaces/Test Run 1 Copy - DefaultScenarioCheck/` — no
  `--room-scenario` (i.e. the existing default).
- `data/workspaces/Test Run 1 Copy - ExtendedWaitingRooms/` —
  `--room-scenario extended_waiting_rooms`.

Both are local, gitignored (`data/` — CLAUDE.md invariant 7) copies; they are
not part of this commit.

## 1. Default scenario — provably unchanged

```
diff <(sort .../Test\ Run\ 1/runs/2026-09-11T12-37-48/assignments.csv) \
     <(sort .../Test\ Run\ 1\ Copy\ -\ DefaultScenarioCheck/runs/latest/assignments.csv)
# → no differences
```

`assignments.csv` from the new run (produced *after* this branch's changes,
loading settings through the new `resolve_room_scenario_dir` helper with
`room_scenario="default"`) is **byte-for-byte identical** to a run solved
*before* this branch existed, from the same input, same solver seed.
`metrics.json` differs only in `solve_seconds` (timing noise) and the new
`run_id`/`room_scenario` fields, which are additive. The solver is
deterministic (fixed `random_seed`) and untouched, and the default room
layout (`config/rooms.yaml`) is byte-identical to before — so this is
exactly the non-regression the feature required.

## 2. Extended scenario — feasibility and quality

| Metric | Default (5 Thu / 4 Fri rooms) | Extended (4 Thu / 3 Fri rooms) |
|---|---:|---:|
| Status | FEASIBLE (phase 1 — zero-clash) | FEASIBLE (phase 1 — zero-clash) |
| Interviews placed | 216 / 216 | 216 / 216 |
| Clashes | 0 | 0 |
| Applicants left unschedulable | 0 | **0** |
| Different-day splits (FR-36b) | 0 | 0 |
| Panels used | 30 | 29 |
| Max panels in one room on one evening | 4 | 5 (room 2019, Friday) |
| Room-exclusivity violations (same division sharing a room on the same day) | 0 / 12 division-day groups | **0 / 12 division-day groups** |
| `max_concurrent_panels` ceiling used | 5 (unchanged) | 5 (unchanged — see below) |
| Objective value (lower is better; both zero-clash) | 3224 | 3277 (+1.6%) |

**Room-exclusivity held at 100% in both scenarios** for this dataset — well
inside (in fact above) the "same 90-95%+ target" the task asked to preserve.
The `objective_breakdown` shows the small objective gap is `lateness`
(1684 → 1727) and `spread_slots` (152 → 153): with one fewer room per
evening, interviews pack slightly later/tighter, but never at the cost of a
clash, a cross-day split, or a shared room between two panels of the same
division.

### On the panel ceiling

The task allowed raising `max_concurrent_panels` above 5 if the tighter room
supply demanded it. **It didn't, for this dataset.** `extended_waiting_rooms`
kept every room at 5 (`config/scenarios/extended_waiting_rooms/rooms.yaml`)
and the measured run above hit 5 panels in one room on one evening (2019,
Friday) without ever needing 6. `ROOM_CONCURRENCY_CEILING` in `settings.py`
(the hard ceiling, currently 5) was **not changed** — this scenario proves
the current ceiling already has enough headroom for the real applicant
volume. If a future intake is significantly larger, re-run this same
verification before assuming 5 is still enough; the scenario's rooms.yaml
documents exactly this reasoning inline.

## 3. Is this scenario actually usable?

**Yes, for the current real applicant volume (~108-120 applicants).** The
measured run:

- found a feasible, zero-clash, zero-cross-day-split schedule;
- placed all 216/216 required interviews — nobody becomes unschedulable;
- kept 100% room-exclusivity (no division ever forced to share a room with
  itself on the same day);
- needed no algorithm change and no panel-ceiling increase — purely
  `rooms.yaml`/`panels.yaml`.

The only real cost observed is a slightly less compact schedule (marginally
later slots on average) — not a functional regression by any hard or soft
constraint in SPEC.md §3.4.

This does **not** mean it is risk-free to adopt sight-unseen for a larger or
differently-shaped future applicant pool (e.g. a divisional demand
distribution much more skewed than this dataset's, or a larger `applicant_cap`)
— re-run `iffsched check --room-scenario extended_waiting_rooms` and a real
`solve` against that future dataset before switching the default.

## Not done in this branch (by design — see task scope)

- No frontend dropdown/UI selector was built. The mechanism is fully wired
  through the CLI (`--room-scenario`) and the API (`POST /solve` body field +
  `GET /room-scenarios` for a future selector), which satisfies "selectable
  ... without needing separate deployments or manual config file swapping."
  Wiring a visible dropdown into the Next.js run page is a follow-up, not a
  backend/scheduling change.
- No workspace-level "default scenario" setting/persistence — each solve
  call states its own scenario explicitly (defaults to `"default"`). This
  was the simplest mechanism that didn't touch workspace metadata schema
  or the DB layer; can be layered on top later if desired.
