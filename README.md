# Meridian — IFF Recruitment Scheduler

Automated interview scheduling for IFF recruitment. Takes applicant preferences from a Google Form, builds a conflict-minimised two-day interview timetable across rooms and interviewer panels, lets a recruiter hand-edit it without losing the solver's work, and emails every applicant their personal schedule and later their result.

Built for **up to 120 applicants × 2 interviews each = 240 interviews** across 2 evenings.

> **Status:** Alpha — planning complete, implementation in progress.
> Full specification: [`docs/SPEC.md`](docs/SPEC.md)

---

## The problem

Scheduling 240 interviews by hand is a bad evening. Every applicant picks two roles and their available times; every division has its own interviewers and its own room; nobody can be in two places at once; and no interviewer can run two interviews at the same moment. Doing this in a spreadsheet means either a lot of manual tetris or a lot of people showing up at the wrong time.

This tool does the tetris. It **guarantees** every applicant gets both of their interviews, and it does its best to place them inside the times they said they were free. Where that's impossible, it says so loudly instead of quietly double-booking someone.

## What it does

- **Ingests** form responses from Google Sheets (API or CSV export)
- **Validates** everything and refuses to guess — bad data is reported, never inferred
- **Checks capacity** before solving, and tells you how many interviewer panels each division actually needs
- **Solves** the timetable with constraint programming (Google OR-Tools CP-SAT), proving optimality rather than approximating
- **Flags clashes** in red where an applicant had to be placed outside their stated availability
- **Respects manual edits** — recruiter decisions become locks that survive every re-solve
- **Exports** room, applicant and panel views to Sheets, XLSX, printable HTML and `.ics`
- **Emails** personalised invites and results, with a send ledger so a crash never causes duplicates

## Core guarantees

| Guarantee | How |
|---|---|
| Every applicant gets **exactly 2 interviews** | Hard constraint, never traded away |
| Both interviews happen even if the two chosen roles share a parent division | Model is indexed by *choice*, not by division |
| Nobody is ever double-booked | Hard constraints on applicant, panel and room concurrency |
| Availability is honoured wherever possible | Dominant objective term — the solver minimises out-of-availability placements before optimising anything else |
| Unavoidable clashes are visible, not hidden | Flagged red in every export, with the reason |
| Manual edits are never overwritten | Locks are hard constraints on re-solve |
| Nobody gets a duplicate email | Idempotent send ledger |

---

## Quickstart

### Requirements

- Python 3.11+
- A Google service account with read access to the responses sheet (or just a CSV export)
- Google Workspace account for sending email, **or** a transactional provider API key

### Install

```bash
git clone https://github.com/<org>/Meridian.git
cd Meridian

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .
cp .env.example .env               # then fill in your secrets
```

### Run the pipeline

```bash
# 1. Pull and clean form responses
iffsched ingest --source sheets          # or: --source csv --path data/raw/responses.csv

# 2. Check you have enough interviewer panels BEFORE solving
iffsched check

# 3. Build the timetable
iffsched solve

# 4. Generate room / applicant / panel views
iffsched publish --run latest

# 5. Make manual edits in the exported sheet, then freeze them
iffsched lock --from runs/latest/assignments.csv
iffsched solve                            # re-solves around your locked rows

# 6. Send interview invites — dry run first, always
iffsched notify invite --run latest --dry-run
iffsched notify invite --run latest --send

# 7. After interviews: ingest scores and send results
iffsched notify result --run latest --dry-run
iffsched notify result --run latest --send
```

`--send` requires typing the expected recipient count to confirm. This is deliberate.

---

## Configuration

Everything tuneable lives in `config/`. **No schedule parameter is hard-coded.**

| File | Controls |
|---|---|
| `event.yaml` | Dates, daily start/end times, interview duration, breaks, timezone, minimum gap |
| `divisions.yaml` | The 6 parent divisions and the sub-division → parent mapping |
| `rooms.yaml` | Rooms, which divisions sit in them, max simultaneous panels per room |
| `panels.yaml` | Interviewer panels — **this is how you add parallel interviewers** |
| `solver.yaml` | Objective weights, time limit, random seed, target utilisation |
| `notify.yaml` | Sender identity, throttle rate, template mapping |

### Changing the interview length

```yaml
# config/event.yaml
interview_duration_minutes: 20    # change to 30
```

The entire slot grid regenerates. `18:00–18:20, 18:20–18:40, …` becomes `18:00–18:30, 18:30–19:00, …`, and the next solve uses the new grid. No code change.

### Adding more interviewers

Demand for Program is high and one panel can't keep up? Add lines:

```yaml
# config/panels.yaml
panels:
  - {id: PROGRAM-A, division: PROGRAM, room: "2016"}
  - {id: PROGRAM-B, division: PROGRAM, room: "2016"}   # 2× throughput
  - {id: PROGRAM-C, division: PROGRAM, room: "2016"}   # 3× throughput
```

Three panels means three Program interviews running simultaneously. Rooms and division-to-room mapping are equally interchangeable — move a division to another room by editing `rooms.yaml`.

---

## Capacity: read this before your first run

The maths is fixed and unforgiving:

```
120 applicants × 2 interviews          = 240 interviews
2 days × 4 hours ÷ 20 min              =  24 slots
one panel's total throughput           =  24 interviews

break-even                             = 240 ÷ 24 = 10 panels (100% utilisation — unreachable)
balanced baseline                      = 2 panels per division = 12 panels
                                       = 288 capacity vs 240 demand = 83% utilisation ✓
```

**You need roughly 12 interviewer panels.** With only 2 rooms, that's 6 simultaneous interviews per room. If 2014 and 2016 are classrooms rather than halls, you need more rooms — 6 rooms × 2 panels is the cleanest fit.

Levers if 12 panels isn't staffable:

| Change | Panels needed |
|---|---|
| 15-minute interviews | 10 |
| Extend to 17:00–22:00 | 10 |
| Add a third day | 9 |
| 30-minute interviews | 18 |

`iffsched check` computes all of this against your real data and tells you the shortfall per division before wasting time on a solve.

---

## How the solver works

The problem is a variant of class–teacher timetabling — NP-hard in general, but tiny at this scale, so an exact solver is the right call.

**Decision variable:** `x[applicant, choice, panel, slot] ∈ {0,1}`

Indexing by *choice* rather than by division is the key design decision. An applicant who picks Media Marketing **and** Media Documentation has chosen two roles that share the parent division MedMarDoc. Indexed by division, that collapses to one key and they'd silently get one interview. Indexed by choice, both interviews exist regardless.

**Hard constraints**

| ID | Constraint |
|---|---|
| C1 | Every choice gets exactly one interview |
| C2 | A panel runs at most one interview per slot |
| C3 | An applicant is in at most one place per slot |
| C4 | A room never exceeds its concurrent-panel capacity |
| C5 | Minimum gap between an applicant's two interviews |
| C6 | Locked (manually placed) assignments are fixed |
| C7 | Panels only work inside their active windows |
| C8 | Same-parent pairs prefer different panels (auto-relaxed if only one exists) |

**Objective** — lexicographic in practice, via ordered weights:

```
minimise   10000 · out-of-availability placements     ← dominant
            + 50 · repeated panel for same-parent pairs
            + 10 · dead time between an applicant's two interviews
            +  5 · load imbalance across panels of a division
            +  1 · lateness (prefer earlier slots)
```

The clash weight dwarfs everything else, so the solver will never accept an extra clash to buy a prettier schedule. That is "prioritise time first, guarantee both interviews always", expressed as arithmetic.

**Two-phase strategy:** first attempt forbids out-of-availability placements entirely. If a solution exists, it has zero clashes. Only if that's infeasible does it relax and minimise the number of clashes.

A greedy fallback solver (MRV ordering + backtracking + local search) is available via `--solver greedy` for environments where OR-Tools can't be installed.

---

## Repository layout

```
config/                  YAML configuration — all tuneable parameters
data/                    gitignored — contains applicant PII
  raw/                   untouched form exports
  interim/               cleaned applicants + validation report
  locks/                 manual decisions that survive re-solves
runs/                    gitignored — one immutable directory per solve
src/iff_scheduler/
  domain/                models, enums, slot grid
  ingest/                sheets/csv sources, normalisation, validation
  scheduling/            feasibility, CP-SAT solver, greedy solver, objectives
  review/                lock engine, manual-edit validator
  export/                room/applicant/panel views, xlsx, html, ics
  notify/                templates, mailers, send ledger, pre-send audit
  results/               score ingestion, decision logic
templates/               Jinja2 email and timetable templates
tests/                   unit tests + anonymised fixtures
docs/                    SPEC.md, RUNBOOK.md, FORM_DESIGN.md
```

**Architectural rule:** the core (`domain/`, `scheduling/`, `review/`) never imports an adapter. It takes plain objects in and returns plain objects out. That's what makes it testable without Google credentials, and what lets you swap Gmail for SendGrid, or CSV for Sheets, without touching the algorithm.

## Tech Stack

| Layer | Library | Purpose |
|---|---|---|
| Language | Python 3.11+ | Core runtime |
| Solver | `ortools` (CP-SAT) | Constraint programming — builds and proves the optimal timetable |
| Models & config | `pydantic v2` + `pyyaml` | Typed domain models; malformed config fails at load, not inside the solver |
| CLI | `typer` + `rich` | Commands and readable terminal output |
| Data wrangling | `pandas` | Ingest normalisation and export tabulation |
| Email templates | `jinja2` | Personalised invite and result emails |
| XLSX export | `openpyxl` | Room, applicant and panel view spreadsheets |
| Calendar export | `icalendar` | `.ics` files per applicant and per panel |
| Google Sheets | `gspread` + `google-auth` | Read form responses; write published timetables |
| Gmail sending | `google-api-python-client` | Automated email dispatch via Gmail API |
| Secrets | `python-dotenv` | Loads credentials from `.env`, never from code |
| Testing | `pytest` | Unit tests — each hard constraint C1–C8 has a dedicated test |
| Lint & format | `ruff` | Enforced code style |
| Type checking | `mypy` | Strict mode on `domain/` and `scheduling/` |

> **No database** — at 120 applicants, data is a few hundred rows. CSV + YAML on disk is correct: it's inspectable, diffable, and openable in Excel.
> **No web framework (alpha)** — CLI only. A Streamlit or React UI is planned for beta and the architecture already accommodates it as an added interface layer.

---

## Email sending

Invites and results use the same machinery — only the template and the data source differ.

```
BUILD → RENDER → AUDIT → APPROVE → SEND → RETRY → REPORT
```

**Safety features, all non-optional:**

- **Send ledger.** Every send is recorded. A crash at recipient 63 is recoverable — re-run and rows 1–62 are skipped automatically.
- **Dry-run.** Renders every email to disk without sending. Always run this first.
- **Pre-send audit.** Hard-fails on empty merge fields, `None`/`nan` leakage, invalid or duplicate addresses, or any applicant missing an assignment or a decision.
- **Approval gate.** `--send` alone isn't enough; you must type the expected recipient count.
- **One message per person.** Never CC, never BCC a batch.
- **Clash emails are held back** for a human to send personally, with an explanation.

Result emails use separate templates per outcome (accepted / waitlisted / rejected) routed by the decision column. A blank decision is a hard failure — it never defaults to a rejection.

**Provider note:** consumer Gmail caps at roughly 100 recipients/day, which sits right on top of 120 applicants. Google Workspace (~1,500/day) is fine. Verify current quotas before relying on them. If you're on consumer Gmail, use a transactional provider (Resend, SendGrid, SES) with a verified domain.

---

## Data privacy

- `data/`, `runs/`, `.env` and `credentials/` are gitignored. **Never commit applicant PII.**
- Only anonymised fixtures in `tests/fixtures/` are version-controlled.
- Secrets load from environment variables, never from code.
- Use `scripts/anonymise_fixture.py` to generate safe test data from a real export.

---

## Roadmap

**Alpha (current)**
- [ ] Config + slot grid
- [ ] Ingest + validation
- [ ] Capacity Advisor
- [ ] CP-SAT solver
- [ ] Exports (room / applicant / panel views)
- [ ] Lock engine + re-solve
- [ ] Invite emails
- [ ] Result emails
- [ ] Runbook

**Beta**
- **Vercel deployment** — Next.js frontend + FastAPI scheduler API
- **PostgreSQL via Supabase** — replaces the CSV/YAML store so data persists across deploys. All I/O sits behind adapter protocols in alpha, so this is a layer swap, not a rewrite.
- Drag-and-drop timetable editor
- Live event-day dashboard with attendance tracking
- WhatsApp notifications
- Interviewer availability self-service
- Multi-round recruitment support

---

## Contributing

1. Branch from `main` using `feat/`, `fix/` or `docs/` prefixes
2. Add or update tests for anything touching `scheduling/` — each hard constraint C1–C8 has its own test
3. Run `pytest` and the formatter before opening a PR
4. Never commit anything from `data/`, `runs/`, `credentials/` or `.env`

## License

_TBD — add before the repository is made public._

## Maintainers

_TBD — add IFF tech team contacts._
