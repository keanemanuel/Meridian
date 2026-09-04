# Meridian — IFF Recruitment Scheduler — Alpha Planning Document

**Organisation:** IFF
**Project:** Recruitment Interview Scheduler (up to 120 applicants)
**Phase:** Alpha
**Status:** Planning / pre-implementation — no code written yet
**Document version:** 1.1
**Last updated:** 5 September 2026

---

## 0. How to read this document

| Section | What it answers |
|---|---|
| 1 | What we're building and the two problems you need to decide on before anything else |
| 2 | Vocabulary — so "division", "panel", "slot" mean exactly one thing |
| 3 | The refined, testable specification (this replaces the original spec) |
| 4 | The pipeline, rewritten end-to-end |
| 5 | The scheduling algorithm — which one, why, and how it's scored |
| 6 | System architecture |
| 7 | Repository architecture |
| 8 | Configuration files (the "changeable" parts) |
| 9 | Google Form design — this determines whether the whole thing works |
| 10 | Automated email: interview invites and results |
| 11 | Manual adjustment model |
| 12 | Edge cases and failure modes |
| 13 | Phasing, milestones, definition of done |
| 14 | Open questions I need answers to |

---

## 1. Executive summary

### 1.1 What this is

A scheduling system that takes applicant preferences from a Google Form, automatically builds a two-day interview timetable across rooms and interviewer panels, lets a recruiter hand-edit it without losing the machine's work, and emails every applicant their personal schedule and later their result.

The point of view is the **hirer**. The system's job is to remove the manual work of "who goes where, when, with whom".

### 1.2 Two findings you should read before anything else

**Finding A — the capacity maths, at the confirmed cap of 120 applicants.**

**Fixed inputs**

```
applicant cap              = 120
interviews per applicant   = 2
TOTAL INTERVIEW DEMAND     = 240

event grid (placeholder)   = 2 days × 18:00–22:00 × 20 min
slots per day              = 240 min ÷ 20 = 12
TOTAL SLOTS                = 24
one panel's throughput     = 24 interviews across the whole event
```

**The equality point**

Supply equals demand when `panels × slots = interviews`:

```
panels = 240 ÷ 24 = 10 panels
```

Ten panels is the theoretical break-even — but it requires **100% utilisation**, i.e. every panel busy in all 24 slots with no idle time anywhere. That is unreachable in practice, because applicants only tick some blocks and the solver has to fit two non-overlapping interviews per person. Treat 10 as the floor, never the plan.

**The balanced baseline (recommended starting configuration)**

Assuming demand spreads roughly evenly across the 6 divisions, each division carries `240 ÷ 6 = 40` interviews. A division needs `40 ÷ 24 = 1.67` panels, so **2 panels per division**:

```
6 divisions × 2 panels   = 12 panels
capacity                 = 12 × 24 = 288 interview slots
demand                   = 240
UTILISATION              = 83%   ← workable, with ~48 slots of slack
per division             = 48 capacity vs 40 demand (8 spare)
```

**12 panels is the baseline to build and staff against.** The 17% slack is what absorbs uneven availability. Divisions with heavier demand (you mentioned Program) get a third panel taken from a lighter division, or added on top:

```
panels_needed(division) = ceil( demand(division) ÷ (24 × 0.83) )
```

The Capacity Advisor (§5.5) computes this per division from the real form data, so the split stops being a guess once submissions are in.

**The room problem**

12 panels across your current 2 rooms means **6 panels running simultaneously in each room** — six interviews happening at once in room 2014. That is a hall, not a classroom. Your realistic options:

| Configuration | Panels/room | Verdict |
|---|---|---|
| 2 rooms × 6 panels | 6 | Only works if 2014/2016 are large halls with separated corners |
| 3 rooms × 4 panels | 4 | Tight but plausible |
| **6 rooms × 2 panels** | **2** | **Cleanest — one room per division, matches the baseline exactly** |
| 12 small rooms × 1 panel | 1 | Ideal acoustically, hardest to book |

**Alternative levers if 12 panels can't be staffed**

| Change | Slots | Panels at equality | Panels at 83% util |
|---|---|---|---|
| 15-min interviews | 32 | 8 | 10 |
| **20-min interviews (current)** | **24** | **10** | **12** |
| 30-min interviews | 16 | 15 | 18 |
| 20-min, extend to 17:00–22:00 | 30 | 8 | 10 |
| 20-min, add a third day | 36 | 7 | 9 |

Shortening interviews to 15 minutes or adding an hour each evening both bring you to 10 panels. Adding a third day brings you to 9. These are the cheapest fixes if interviewer headcount is the binding constraint.

The system computes all of this automatically before it tries to solve — but the constraint is physical, not algorithmic. No scheduler can create capacity that doesn't exist.

**Finding B — same-parent pairs are valid, and the model must be built around that.**

Your rule is: Media Marketing and Media Documentation both roll up to **MedMarDoc**; Creative and WebMaster both roll up to **Creative**. An applicant may pick *Media Marketing* **and** *Media Documentation* — and when they do, they still get **two separate interviews**, both handled by MedMarDoc panels in the MedMarDoc room.

This is confirmed behaviour, not an error. But it has a real design consequence that is easy to get wrong:

> **The unit of scheduling is the applicant's *choice*, not their parent division.**

If you index the model by `(applicant, division)`, an applicant who picks two sub-divisions of the same parent produces only **one** key — and the solver silently gives them one interview. Indexing by `(applicant, choice_1)` and `(applicant, choice_2)` makes both interviews first-class regardless of whether the parents happen to match. The parent division is then just an attribute of the choice, used to decide which panels are eligible.

Two follow-on rules fall out of this:

- **Distinct sub-divisions required.** The two choices must be different *sub-divisions*. Picking Media Marketing twice is a data error; picking Media Marketing + Media Documentation is fine.
- **Prefer different panels.** When both interviews land on the same parent division, the applicant should ideally not face the same interviewer panel twice — the two conversations are about different roles. Enforced as a soft constraint that degrades gracefully when a division has only one panel (see C8 in §5.2).

Handled in §3.1 (FR-04), §5.2 (variable definition and C8), §9.3 (form design) and §12 (E-01).

### 1.3 Alpha scope

**In scope:** ingest → normalise → validate → feasibility check → solve → review/manual edit → export timetables → email invites → collect results → email results.

**Out of scope for alpha:** drag-and-drop web UI, live re-scheduling during the event, SMS/WhatsApp notifications, interviewer self-service availability portal, multi-round recruitment.

---

## 2. Domain model and glossary

These terms are used with exactly this meaning throughout.

| Term | Definition |
|---|---|
| **Applicant** | A person who submitted the recruitment form. Identified uniquely by email address. |
| **Sub-division** | What the applicant sees and selects on the form: Media Marketing, Media Documentation, Creative, WebMaster, Logistics, Liaison, Finance & Booth, Program. |
| **Division (parent division)** | What the scheduler operates on. Six of them: `MEDMARDOC`, `CREATIVE`, `LOGISTICS`, `LIAISON`, `FNB`, `PROGRAM`. |
| **Division mapping** | The many-to-one function from sub-division to division. Configurable, not hard-coded. |
| **Choice** | One of an applicant's two selections, identified by `(applicant_id, choice_index ∈ {1,2})`. Carries a sub-division and its parent division. **This is the unit of scheduling** — every choice becomes exactly one interview, even when both choices share a parent division. |
| **Room** | A physical location, e.g. `2014`, `2016`. Has a concurrency capacity (how many panels can sit in it at once). |
| **Interviewer Panel** | One set of interviewers who can conduct one interview at a time. Defined by `(panel_id, division, room, active_windows)`. **This is the unit of parallelism.** Spawning "3 groups for Program" means creating 3 panels with `division = PROGRAM`. |
| **Slot** | An atomic, indivisible unit of time on the grid, e.g. `Thu 18:20–18:40`. All slots have the same length. |
| **Interview** | A single applicant *choice* realised as a meeting, occupying exactly one `(panel, slot)` pair. Two per applicant, always. |
| **Assignment** | A concrete `(applicant, choice_index, sub_division, division, panel, slot)` tuple in the final schedule. |
| **Availability window** | A time range an applicant declared as free, e.g. `Fri 18:30–20:30`. An applicant may have several. |
| **Preferred assignment** | An assignment whose slot falls entirely inside one of the applicant's availability windows. |
| **Forced assignment (clash)** | An assignment made outside the applicant's declared availability, because no preferred option existed. **Flagged red.** |
| **Lock / pin** | A manual decision by the recruiter that the solver must treat as fixed on subsequent runs. |
| **Solve run** | One execution of the scheduler producing a complete timetable. |

### 2.1 Division mapping (default, configurable)

```
Media Marketing      ─┐
Media Documentation  ─┴─→  MEDMARDOC
Creative             ─┐
WebMaster            ─┴─→  CREATIVE
Logistics            ────→  LOGISTICS
Liaison              ────→  LIAISON
Finance & Booth      ────→  FNB
Program              ────→  PROGRAM
```

The applicant's original sub-division choice is **retained** on the record (for the interviewers' context and for later placement) even though scheduling happens at parent level.

---

## 3. Refined project specification

Requirements are given IDs so you can reference them in issues and tests.
Priority: **M** = must have for alpha, **S** = should have, **C** = could have.

### 3.1 Functional requirements — data intake

| ID | Pri | Requirement |
|---|---|---|
| FR-01 | M | The system shall accept applicant data from a Google Sheet populated by a Google Form, either via direct API read or via an exported CSV/XLSX file. |
| FR-02 | M | The system shall parse availability from **structured** form fields (checkbox grid of discrete blocks), not free text. Availability shall never be inferred or guessed. |
| FR-03 | M | The system shall map each selected sub-division to its parent division using a configurable mapping. |
| FR-04 | M | The system shall enforce that each applicant has exactly two **distinct sub-divisions**. Two sub-divisions sharing a parent division (e.g. Media Marketing + Media Documentation) are **valid** and shall yield two separate interviews. Records with a repeated sub-division are rejected to an error report and excluded from the solve. |
| FR-04b | M | The system shall model each applicant's two selections as two independent **choices**, so that both interviews exist regardless of whether the choices share a parent division. |
| FR-05 | M | The system shall deduplicate applicants by email address, keeping the most recent submission by timestamp, and report every duplicate it collapsed. |
| FR-06 | M | The system shall produce a validation report listing every rejected or suspicious record with the reason, before any scheduling occurs. |
| FR-07 | S | The system shall support re-running ingest after new submissions arrive, merging with existing locked assignments. |

### 3.2 Functional requirements — time grid

| ID | Pri | Requirement |
|---|---|---|
| FR-10 | M | The system shall generate a slot grid from configuration: a list of event days, each with a start time and end time, and a global interview duration. |
| FR-11 | M | Changing `interview_duration_minutes` shall regenerate the entire grid with no code change. E.g. `20` → slots 18:00–18:20, 18:20–18:40 …; `30` → 18:00–18:30, 18:30–19:00 …. |
| FR-12 | M | Event dates shall be configurable. Placeholder: **Thu 17 Sep 2026** and **Fri 18 Sep 2026**. |
| FR-13 | M | Daily start/end times shall be configurable, and may differ per day. Placeholder: **18:00–22:00** on both days. |
| FR-14 | S | The system shall support an optional configurable **break window** per day (e.g. 19:40–20:00) during which no slots are generated. |
| FR-15 | S | If the day length is not an exact multiple of the interview duration, the trailing partial slot shall be discarded and reported. |
| FR-16 | S | All times shall be interpreted in a single configurable timezone, used consistently in exports and calendar invites. |

### 3.3 Functional requirements — rooms and panels

| ID | Pri | Requirement |
|---|---|---|
| FR-20 | M | Rooms shall be defined in configuration. Placeholder: `2014`, `2016`. |
| FR-21 | M | The mapping of divisions to rooms shall be configuration, not code. Placeholder: `2014 → {MEDMARDOC, CREATIVE, LOGISTICS}`, `2016 → {FNB, PROGRAM, LIAISON}`. Adding a room, or moving a division, shall require only a config edit. |
| FR-22 | M | The recruiter shall be able to define **interviewer panels**, each with at minimum a division and a room. Multiple panels of the same division in the same room shall be permitted, giving *n* simultaneous interviews for that division. |
| FR-23 | M | Each panel shall conduct at most one interview per slot. |
| FR-24 | S | Each room shall have a `max_concurrent_panels` capacity. The solver shall never exceed it. |
| FR-25 | S | Each panel shall have optional active windows (days/times it is available), defaulting to the full event. |
| FR-26 | C | The system shall report per-panel utilisation and idle slots after solving. |

### 3.4 Functional requirements — scheduling

| ID | Pri | Requirement |
|---|---|---|
| FR-30 | M | **Guarantee:** every valid applicant shall receive exactly **two** interviews — one for each of their two chosen sub-divisions, each conducted by a panel of that choice's parent division. This is a hard constraint and is never traded away. |
| FR-30b | S | Where both of an applicant's choices resolve to the same parent division, the two interviews shall preferably be conducted by **different panels** of that division. This preference is relaxed automatically when the division has only one panel. |
| FR-31 | M | No applicant shall have two interviews in the same slot. |
| FR-32 | M | An applicant's two interviews shall be separated by at least `min_gap_slots` (configurable; default `0` = back-to-back permitted, `1` recommended when the two interviews are in different rooms). |
| FR-33 | M | The solver shall **prioritise time first**: it shall maximise the number of interviews that fall inside the applicant's declared availability, then allocate divisions/panels available at those times. |
| FR-34 | M | Where no in-availability placement exists, the solver shall place the applicant anyway (to satisfy FR-30) and mark the assignment as a **CLASH**, highlighted red in every export. |
| FR-35 | M | The solver shall be deterministic: the same inputs, config and random seed produce the same schedule. |
| FR-36 | S | The solver shall prefer compact schedules for applicants (minimise dead time between their two interviews) as a secondary objective. |
| FR-37 | S | The solver shall balance load across panels of the same division rather than filling one panel first. |
| FR-38 | S | The solver shall prefer earlier slots when all else is equal, so the event can finish early if attendance drops. |
| FR-39 | M | The solver shall complete a 240-interview instance in under 60 seconds on a standard laptop. |

### 3.5 Functional requirements — review and manual adjustment

| ID | Pri | Requirement |
|---|---|---|
| FR-40 | M | The recruiter shall be able to move an interview to a different slot and/or panel. |
| FR-41 | M | Manual edits shall be recorded as **locks**. A subsequent solve shall treat locked assignments as hard constraints and schedule everything else around them. |
| FR-42 | M | The system shall validate manual edits and refuse (with a clear message) any edit that creates a double-booking of an applicant or panel, or exceeds room capacity. |
| FR-43 | M | Every export shall include a **conflict report**: all clashes (FR-34), all unfilled requirements, all capacity warnings. |
| FR-44 | S | The system shall show a diff between the previous and current solve so the recruiter can see what moved. |
| FR-45 | C | The recruiter shall be able to mark an applicant as withdrawn/no-show and re-solve the remainder. |

### 3.6 Functional requirements — outputs

| ID | Pri | Requirement |
|---|---|---|
| FR-50 | M | **Room view:** one timetable per room per day — rows are slots, columns are panels, cells are applicant + division. |
| FR-51 | M | **Applicant view:** one row per applicant with both interview times, rooms, panels and divisions. |
| FR-52 | M | **Panel view:** one sheet per interviewer panel, being their running order for the day. |
| FR-53 | M | Outputs shall be written back to Google Sheets and/or exported as XLSX and printable HTML. |
| FR-54 | S | Clashes shall be visually highlighted red in all outputs. |
| FR-55 | C | The system shall generate `.ics` calendar files per applicant and per panel. |

### 3.7 Functional requirements — notifications

| ID | Pri | Requirement |
|---|---|---|
| FR-60 | M | The system shall send each applicant a personalised email stating both interview times, dates, rooms and divisions. |
| FR-61 | M | Emails shall be sent individually. No applicant shall ever see another applicant's address. |
| FR-62 | M | The system shall maintain a **send ledger** so a re-run never double-sends. Each row records recipient, template, timestamp, status, provider message ID. |
| FR-63 | M | The system shall support a **dry-run** mode that renders every email to disk without sending. |
| FR-64 | M | Sending shall require an explicit human approval step after a pre-send audit (counts, blank-field check, sample render). |
| FR-65 | M | The system shall send each applicant a personalised **result** email at the end of the process. |
| FR-66 | S | Failed sends shall be retried with exponential backoff and surfaced in a failure report. |
| FR-67 | S | Sending shall be throttled to stay within provider quotas. |

### 3.8 Non-functional requirements

| ID | Pri | Requirement |
|---|---|---|
| NFR-01 | M | All tuneable values (dates, times, duration, rooms, divisions, mapping, panels, penalties) live in config files. No magic numbers in code. |
| NFR-02 | M | Applicant PII shall not be committed to version control. `data/` is gitignored; only anonymised fixtures are committed. |
| NFR-03 | M | Every solve run writes a timestamped, immutable artefact directory so any published schedule can be reproduced. |
| NFR-04 | S | Core scheduling logic shall have unit tests including the constraint-violation cases in §12. |
| NFR-05 | S | A non-technical committee member shall be able to run the whole pipeline from documented commands. |
| NFR-06 | S | Secrets (service account keys, API keys) shall be loaded from environment variables, never from code. |

---

## 4. Pipeline and flow (rewritten)

### 4.1 Stage diagram

```
  ┌─────────────────────────────────────────────────────────────────┐
  │ STAGE 0 · CONFIGURE                                             │
  │ event.yaml · divisions.yaml · rooms.yaml · panels.yaml          │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 1 · COLLECT        Google Form (structured availability)  │
  │ STAGE 2 · LAND           Google Sheet — raw responses           │
  │ STAGE 3 · EXTRACT        Sheets API read  ·or·  CSV export      │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 4 · NORMALISE & VALIDATE                                  │
  │  • dedupe by email (keep latest)                                │
  │  • map sub-division → parent division                           │
  │  • reject: <2 divisions, collapsed pair, no availability        │
  │  • parse availability blocks → slot bitmap                      │
  │  OUT: applicants.clean.csv  +  validation_report.csv            │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 5 · FEASIBILITY / CAPACITY ADVISOR                        │
  │  demand per division vs panel-slot supply                       │
  │  → "PROGRAM needs 3 panels, you configured 1"                   │
  │  HARD STOP if infeasible. Recruiter adjusts panels/rooms/time.  │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 6 · SOLVE                                                 │
  │  load locks → build model → optimise → write assignments        │
  │  OUT: runs/<timestamp>/assignments.csv, metrics.json            │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 7 · REVIEW & MANUAL ADJUST                     ◄──┐       │
  │  recruiter edits in Sheet/XLSX → validate → save as lock │       │
  │  re-solve with locks fixed ────────────────────────────►─┘       │
  └─────────────────────────────┬───────────────────────────────────┘
                                │  [recruiter approves]
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 8 · PUBLISH        room / applicant / panel views · ICS   │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 9 · NOTIFY (invites)                                      │
  │  render → dry-run → audit → APPROVE → send → ledger → retry     │
  └─────────────────────────────┬───────────────────────────────────┘
                                │
  ┌─────────────────────────────▼───────────────────────────────────┐
  │ STAGE 10 · EVENT DAY OPS  attendance, no-shows, live moves      │
  │ STAGE 11 · SCORING        interviewer scoring form → sheet      │
  │ STAGE 12 · NOTIFY (results)  same machinery, result templates   │
  └─────────────────────────────────────────────────────────────────┘
```

### 4.2 Stage detail

**Stage 0 — Configure.** Everything tuneable is set here before a run: dates, hours, interview length, rooms, division→room mapping, panels, solver penalties, timezone. This stage exists so that "the placeholder is tentative and subject to change" costs a config edit, not a rewrite.

**Stage 1 — Collect.** The Google Form is not a passive input; it is the first line of data quality. Availability is captured as a checkbox grid of discrete blocks; division choice is capped at exactly two and validated against collapsing pairs. See §9.

**Stage 2 — Land.** Form responses land in a linked Google Sheet. This sheet is treated as **append-only raw data** and is never edited by hand.

**Stage 3 — Extract.** Two supported paths, both producing the same internal shape:
- **API path (preferred):** service account reads the sheet directly. No manual export, always current.
- **File path (fallback):** `File → Download → CSV`, dropped into `data/raw/`. Works with zero credentials — useful when someone else needs to run it.

**Stage 4 — Normalise & validate.** Raw rows become typed `Applicant` objects. Availability strings become a boolean bitmap over the slot grid. Everything ambiguous is **rejected loudly**, never guessed. Output is two files: clean applicants, and a validation report the recruiter must clear before proceeding.

**Stage 5 — Feasibility / Capacity Advisor.** Before solving, compute per division: interviews demanded vs `panels × available slots`. Also compute availability-weighted supply (a slot only counts if applicants are actually free then). Emit a table with a recommended panel count per division. If demand exceeds supply anywhere, **stop** and tell the recruiter exactly how many panels to spawn. This turns "the solver failed" into "spawn two more Program panels".

**Stage 6 — Solve.** See §5.

**Stage 7 — Review & manual adjust.** The recruiter sees the timetable, moves what they want, and those moves become locks. Re-solving respects locks absolutely. This is the loop that makes the tool trustworthy: human decisions are never overwritten by the machine.

**Stage 8 — Publish.** Three views generated from one source of truth, plus the conflict report. Nothing is published while unresolved red clashes exist unless the recruiter explicitly acknowledges them.

**Stage 9 — Notify (invites).** See §10.

**Stage 10 — Event day ops.** Print panel run-sheets. Mark attendance. Handle no-shows by freeing the slot and, optionally, pulling a later applicant forward.

**Stage 11 — Scoring.** Each panel fills a short scoring form per applicant (applicant ID, division, score, notes, recommendation). Responses land in a scoring sheet keyed by applicant ID.

**Stage 12 — Notify (results).** Join scores to applicants, produce a decision column, and reuse the Stage 9 machinery with result templates. Same ledger, same dry-run, same approval gate — and given what these emails say, the approval gate matters more here than anywhere else.

---

## 5. Scheduling algorithm

### 5.1 What kind of problem this is

This is a variant of the **class–teacher timetabling problem**: assign events (applicant × division) to resources (panel × slot) subject to no-overlap constraints on two independent dimensions (the applicant and the panel), while optimising a soft preference (availability). This family is **NP-hard** in general, so there is no clever polynomial algorithm to reach for. The practical question is which solver technology fits the problem size.

Your instance is small: ~240 interviews, ~24 slots, ~12 panels. That is comfortably within exact-solver territory.

### 5.2 Recommended approach — Constraint Programming (CP-SAT)

**Use Google OR-Tools CP-SAT.** It is the right tool because it handles all your constraints natively, finds a provably optimal (or near-optimal with a proven bound) solution, and solves an instance this size in seconds.

**Decision variables**

Indexed by **choice**, not by division — this is what guarantees two interviews even when both choices share a parent division (§1.2, Finding B).

```
x[a, c, p, s] ∈ {0, 1}
  a = applicant
  c = choice index ∈ {1, 2}          ← NOT the division
  p = panel where panel.division == parent_division(a, c)
  s = slot where panel p is active
```

**Hard constraints**

```
C1  Completeness (FR-30):
    for every (a, c):   Σ_{p,s} x[a,c,p,s] = 1
    ── holds for both choices even when they map to the same parent division

C2  Panel exclusivity (FR-23):
    for every (p, s):   Σ_{a,c} x[a,c,p,s] ≤ 1

C3  Applicant exclusivity (FR-31):
    for every (a, s):   Σ_{c,p} x[a,c,p,s] ≤ 1

C4  Room concurrency (FR-24):
    for every (room, s): Σ_{p in room} Σ_{a,c} x[a,c,p,s] ≤ room.max_concurrent_panels

C5  Minimum gap (FR-32):
    for every a, for every slot pair (s1, s2) with |s1 − s2| < min_gap_slots:
        Σ_c Σ_p (x[a,c,p,s1] + x[a,c,p,s2]) ≤ 1

C6  Locks (FR-41):
    for every locked assignment:  x[a,c,p,s] = 1

C7  Panel active windows (FR-25):
    x[a,c,p,s] = 0 wherever slot s ∉ panel p's active windows

C8  Distinct panels for same-parent pairs (FR-30b) — SOFT, auto-relaxed:
    where parent_division(a,1) == parent_division(a,2)
      and that division has ≥ 2 panels:
        for every panel p:  Σ_s (x[a,1,p,s] + x[a,2,p,s]) ≤ 1
    ── skipped entirely when the division has only one panel
```

**Objective — time-first, per FR-33**

```
minimise
      W_CLASH   · Σ x[a,c,p,s] · (1 if s ∉ availability(a) else 0)     ← dominant term
    + W_REPEAT  · Σ_a  (1 if both interviews used the same panel else 0)
    + W_SPREAD  · Σ_a  gap_slots_between_a's_two_interviews
    + W_BALANCE · Σ_d  (max_panel_load(d) − min_panel_load(d))
    + W_LATE    · Σ x[a,c,p,s] · slot_index(s)
```

With weights ordered `W_CLASH ≫ W_REPEAT > W_SPREAD > W_BALANCE > W_LATE` (e.g. 10 000 / 50 / 10 / 5 / 1) this is lexicographic in practice: the solver will never accept an extra clash to gain compactness, and never drop an interview to avoid a repeated panel. That encodes "prioritise time first, guarantee both interviews always" exactly.

**Why not something simpler?**

| Approach | Verdict |
|---|---|
| Hungarian algorithm / min-cost bipartite matching | Exact and fast — but only solves **one division at a time**. It cannot express C3 (an applicant's two interviews must not collide across divisions). Solving divisions sequentially makes it greedy and demonstrably sub-optimal. Useful as a per-division subroutine, not as the whole answer. |
| Min-Cost Max-Flow | Same limitation as above, plus harder to extend with room capacity and gap constraints. |
| Pure greedy | Fast, easy, and will strand applicants — the last ones scheduled get forced clashes that a smarter assignment would have avoided. Acceptable only as a starting point for local search. |
| Genetic algorithm / simulated annealing | Works, common in timetabling literature, but for an instance this small CP-SAT gives you a *proven optimum* instead of "the best we found in 5 minutes". Not worth the tuning effort here. |
| **CP-SAT (recommended)** | Expresses every constraint directly, proves optimality, solves in seconds, supports locks trivially, and degrades gracefully via a time limit. |

### 5.3 Fallback solver (no external dependency)

Ship a second solver behind the same interface, for environments where OR-Tools can't be installed:

1. **Pre-check** feasibility (§5.5).
2. **Order applicants by Most-Constrained-Variable (MRV):** fewest feasible `(panel, slot)` combinations first. Scarce availability gets first pick.
3. **Assign by Least-Constraining-Value:** among feasible slots, choose the one that removes the fewest options from other applicants.
4. **Backtrack with forward checking** when a dead end is hit.
5. **Local search:** repeatedly attempt 2-swaps and slot-moves that reduce total penalty; stop when no improving move exists or a time budget expires.

Expect this to land within a few percent of optimal on instances of this size. Both solvers implement `Solver.solve(problem) -> Schedule`, so they are interchangeable and comparable.

### 5.4 Two-phase strategy (recommended runtime behaviour)

1. **Phase 1 — availability-only.** Forbid out-of-availability placements entirely. If a full solution exists, it has zero clashes. Done.
2. **Phase 2 — relaxed.** If Phase 1 is infeasible, re-run with clashes permitted but heavily penalised. The solver now minimises the number of clashes, and every clash is reported with the reason ("no PROGRAM panel free during any of this applicant's 3 declared blocks").

This gives clean output in the good case and an explainable, minimal-damage output in the bad case.

### 5.5 Capacity Advisor (runs before the solver)

For each division `d`:

```
demand(d)          = number of interviews required for d
raw_supply(d)      = panels(d) × active_slots(d)
effective_supply(d)= Σ_slots  min( panels(d), applicants_of_d_available_at_slot )
recommended_panels(d) = ceil( demand(d) / (active_slots × target_utilisation) )
```

Output table: division · demand · panels configured · raw supply · effective supply · recommended panels · verdict (OK / TIGHT / INFEASIBLE). This is what turns Finding A from a nasty surprise into a planning input.

---

## 6. System architecture

### 6.1 Options considered

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Python core + Google adapters** | OR-Tools available, testable, fast, portable; CSV/XLSX/Sheets all supported | Requires someone with a Python environment | **Chosen for alpha** |
| B. Pure Google Apps Script | Lives in the Sheet, zero setup, easy handoff | No OR-Tools; weak algorithm; poor testing; 6-min execution limit; painful for 240 interviews | Rejected as core; may be used for a "send emails" button later |
| C. Python core + web UI (Streamlit/React) | Drag-and-drop editing, best recruiter UX | Significant extra build | **Beta** — architecture below keeps this open |

The architecture deliberately isolates the core so that C is an added front-end, not a rewrite.

### 6.2 Layering

```
┌───────────────────────────────────────────────────────────────┐
│  INTERFACE          CLI  ·  (beta: Streamlit / web UI)        │
├───────────────────────────────────────────────────────────────┤
│  ORCHESTRATION      pipeline runner · run artefacts · logging │
├───────────────────────────────────────────────────────────────┤
│  CORE (pure, no I/O, fully unit-tested)                       │
│    domain models · slot grid · validation rules               │
│    feasibility · solvers (cpsat | greedy) · lock engine       │
├───────────────────────────────────────────────────────────────┤
│  ADAPTERS (all I/O, swappable)                                │
│    ingest: SheetsReader | CsvReader                           │
│    export: SheetsWriter | XlsxWriter | HtmlWriter | IcsWriter │
│    notify: GmailMailer | SmtpMailer | ResendMailer            │
├───────────────────────────────────────────────────────────────┤
│  EXTERNAL           Google Forms · Sheets · Gmail/ESP         │
└───────────────────────────────────────────────────────────────┘
```

**The rule:** the core never imports an adapter. It takes plain objects in and returns plain objects out. That is what makes it testable without Google credentials, and what lets you swap Gmail for SendGrid, or CSV for Sheets, without touching the algorithm.

### 6.3 Data contracts (stable interfaces between stages)

```
applicants.clean.csv
  applicant_id, full_name, email, phone,
  sub_division_1, sub_division_2, division_1, division_2,
  availability_slots (pipe-separated slot IDs), submitted_at, notes

slots.csv
  slot_id, date, day_label, start_time, end_time, slot_index

panels.csv
  panel_id, division, room, active_slot_ids

assignments.csv
  applicant_id, full_name, email, choice_index (1|2),
  sub_division, division, panel_id, room,
  slot_id, date, start_time, end_time,
  is_clash (bool), is_locked (bool), same_parent_pair (bool), reason

conflicts.csv
  applicant_id, severity (RED|AMBER), type, message

send_ledger.csv
  ledger_id, applicant_id, email, template, run_id,
  status (PENDING|SENT|FAILED|SKIPPED), provider_message_id,
  attempt_count, sent_at, error
```

---

## 7. Repository architecture

```
Meridian/
├── README.md                         # quickstart + runbook
├── pyproject.toml                    # deps: ortools, pandas, pydantic,
│                                     #       gspread, jinja2, openpyxl, typer
├── .env.example                      # names of required secrets, no values
├── .gitignore                        # data/, .env, credentials/, runs/
│
├── config/
│   ├── event.yaml                    # dates, hours, duration, timezone, breaks
│   ├── divisions.yaml                # parent divisions + sub-division mapping
│   ├── rooms.yaml                    # rooms, capacity, division→room mapping
│   ├── panels.yaml                   # interviewer panels (the parallelism knob)
│   ├── solver.yaml                   # weights, gap, time limit, seed
│   └── notify.yaml                   # sender identity, throttle, template map
│
├── credentials/                      # gitignored
│   └── service_account.json
│
├── data/                             # gitignored — contains PII
│   ├── raw/                          # untouched form exports
│   ├── interim/                      # applicants.clean.csv, validation_report.csv
│   ├── locks/pinned_assignments.csv  # manual decisions, survives re-solves
│   └── output/                       # published artefacts
│
├── runs/                             # gitignored, one immutable dir per solve
│   └── 2026-09-05T14-30-00/
│       ├── config_snapshot/          # exact config used
│       ├── assignments.csv
│       ├── conflicts.csv
│       ├── metrics.json
│       └── solve.log
│
├── src/iff_scheduler/
│   ├── __init__.py
│   ├── cli.py                        # typer commands (see §7.1)
│   ├── settings.py                   # config loading + schema validation
│   │
│   ├── domain/
│   │   ├── models.py                 # Applicant, Division, Room, Panel, Slot,
│   │   │                             # Interview, Assignment, Schedule
│   │   ├── enums.py                  # DivisionCode, Severity, SendStatus
│   │   └── grid.py                   # slot grid generation from config
│   │
│   ├── ingest/
│   │   ├── base.py                   # ApplicantSource protocol
│   │   ├── sheets_source.py
│   │   ├── csv_source.py
│   │   ├── normalize.py              # dedupe, sub-div → parent, availability parse
│   │   └── validate.py               # all rejection rules → validation_report
│   │
│   ├── scheduling/
│   │   ├── feasibility.py            # Capacity Advisor
│   │   ├── base.py                   # Solver protocol
│   │   ├── solver_cpsat.py           # primary
│   │   ├── solver_greedy.py          # fallback: MRV + backtracking + local search
│   │   ├── objectives.py             # penalty weights and scoring
│   │   └── postprocess.py            # clash detection, metrics, diff vs previous
│   │
│   ├── review/
│   │   ├── locks.py                  # read/write pinned_assignments
│   │   └── edit_validator.py         # reject illegal manual edits
│   │
│   ├── export/
│   │   ├── room_view.py
│   │   ├── applicant_view.py
│   │   ├── panel_view.py
│   │   ├── xlsx_writer.py
│   │   ├── html_writer.py
│   │   ├── sheets_writer.py
│   │   └── ics_writer.py
│   │
│   ├── notify/
│   │   ├── base.py                   # Mailer protocol
│   │   ├── gmail_mailer.py
│   │   ├── smtp_mailer.py
│   │   ├── resend_mailer.py
│   │   ├── renderer.py               # jinja2 template rendering
│   │   ├── ledger.py                 # idempotency + retry state
│   │   └── audit.py                  # pre-send checks
│   │
│   └── results/
│       ├── ingest_scores.py
│       └── decide.py                 # join scores → decision column
│
├── templates/
│   ├── email/
│   │   ├── invite.html.j2
│   │   ├── invite.txt.j2
│   │   ├── result_accepted.html.j2
│   │   ├── result_waitlist.html.j2
│   │   └── result_rejected.html.j2
│   └── timetable/room_view.html.j2
│
├── tests/
│   ├── fixtures/                     # anonymised sample data (committed)
│   ├── test_grid.py
│   ├── test_normalize.py             # incl. same-parent pair case
│   ├── test_validate.py
│   ├── test_feasibility.py
│   ├── test_solver_constraints.py    # C1–C8 each get a test
│   ├── test_locks.py
│   └── test_ledger.py
│
├── docs/
│   ├── SPEC.md                       # this document
│   ├── RUNBOOK.md                    # what to do on the day
│   └── FORM_DESIGN.md
│
└── scripts/
    ├── bootstrap_form.md
    └── anonymise_fixture.py
```

### 7.1 CLI surface

```
iffsched ingest      --source sheets|csv     # → applicants.clean.csv + validation_report
iffsched check                               # → Capacity Advisor table
iffsched solve       [--solver cpsat|greedy] # → runs/<ts>/
iffsched publish     --run <ts>              # → room/applicant/panel views, ICS
iffsched lock        --from runs/<ts>/assignments.csv   # freeze manual edits
iffsched notify invite  --run <ts> --dry-run # render only
iffsched notify invite  --run <ts> --send    # requires typed confirmation
iffsched notify result  --run <ts> --dry-run
iffsched notify result  --run <ts> --send
iffsched status                              # ledger summary, sent/failed/pending
```

---

## 8. Configuration files

These are the "changeable" parts. Changing them requires no code edit.

**`config/event.yaml`**
```yaml
event_name: "IFF Recruitment 2026"
timezone: "Asia/Jakarta"          # ← confirm; drives ICS and all displayed times
interview_duration_minutes: 20    # ← change to 30 and the whole grid regenerates
min_gap_slots: 0                  # slots required between an applicant's 2 interviews
days:
  - date: 2026-09-17
    label: "Thu"
    start: "18:00"
    end:   "22:00"
    breaks: []                    # e.g. [{start: "19:40", end: "20:00"}]
  - date: 2026-09-18
    label: "Fri"
    start: "18:00"
    end:   "22:00"
    breaks: []
```

**`config/divisions.yaml`**
```yaml
divisions:
  - code: MEDMARDOC
    display: "Media Marketing & Documentation"
  - code: CREATIVE
    display: "Creative"
  - code: LOGISTICS
    display: "Logistics"
  - code: LIAISON
    display: "Liaison"
  - code: FNB
    display: "Finance & Booth"
  - code: PROGRAM
    display: "Program"

sub_division_mapping:
  "Media Marketing":     MEDMARDOC
  "Media Documentation": MEDMARDOC
  "Creative":            CREATIVE
  "WebMaster":           CREATIVE
  "Logistics":           LOGISTICS
  "Liaison":             LIAISON
  "Finance & Booth":     FNB
  "Program":             PROGRAM
```

**`config/rooms.yaml`**
```yaml
# Current 2-room plan. At the 12-panel baseline this means 6 simultaneous
# interviews per room — only viable if these are halls. See §1.2, Finding A.
rooms:
  - id: "2014"
    max_concurrent_panels: 6
    divisions: [MEDMARDOC, CREATIVE, LOGISTICS]
  - id: "2016"
    max_concurrent_panels: 6
    divisions: [FNB, PROGRAM, LIAISON]
```

**`config/panels.yaml`** — this is the "spawn more interviewers" control (FR-22)
```yaml
# BALANCED BASELINE for 120 applicants / 240 interviews / 24 slots:
# 2 panels per division = 12 panels = 288 capacity = 83% utilisation.
panels:
  - {id: MEDMARDOC-A, division: MEDMARDOC, room: "2014"}
  - {id: MEDMARDOC-B, division: MEDMARDOC, room: "2014"}
  - {id: CREATIVE-A,  division: CREATIVE,  room: "2014"}
  - {id: CREATIVE-B,  division: CREATIVE,  room: "2014"}
  - {id: LOGISTICS-A, division: LOGISTICS, room: "2014"}
  - {id: LOGISTICS-B, division: LOGISTICS, room: "2014"}
  - {id: FNB-A,       division: FNB,       room: "2016"}
  - {id: FNB-B,       division: FNB,       room: "2016"}
  - {id: PROGRAM-A,   division: PROGRAM,   room: "2016"}
  - {id: PROGRAM-B,   division: PROGRAM,   room: "2016"}
  - {id: PROGRAM-C,   division: PROGRAM,   room: "2016"}   # ← +1 for high demand
  - {id: LIAISON-A,   division: LIAISON,   room: "2016"}
  - {id: LIAISON-B,   division: LIAISON,   room: "2016"}

# Panels may also declare limited availability:
#  - id: LIAISON-B
#    division: LIAISON
#    room: "2016"
#    active_windows:
#      - {date: 2026-09-17, start: "18:00", end: "22:00"}   # Thursday only
```

Adding one line to this file adds one panel. Two panels of the same division in the same room means two simultaneous interviews for that division; three means three. This is the entire mechanism for FR-22, and it is also what satisfies C8 (an applicant with a same-parent pair can be given two different panels).

**`config/solver.yaml`**
```yaml
solver: cpsat
random_seed: 42
time_limit_seconds: 60
two_phase: true            # try zero-clash first, then relax
weights:
  clash:    10000          # dominant — never trade an extra clash for anything
  repeat_panel: 50         # C8: same panel for both interviews of a same-parent pair
  spread:      10
  balance:      5
  lateness:     1
applicant_cap: 120         # confirmed cap; Capacity Advisor plans against this
target_utilisation: 0.83   # baseline: 12 panels × 24 slots vs 240 interviews
```

---

## 9. Google Form design

The form is part of the system. If it collects free text, the pipeline cannot work without guessing — and FR-02 forbids guessing.

### 9.1 Fields

| # | Field | Type | Required | Notes |
|---|---|---|---|---|
| 1 | Email address | Auto-collect | Yes | Primary key. Enable "Collect email addresses". |
| 2 | Full name | Short answer | Yes | |
| 3 | Student/member ID | Short answer | Yes | Secondary identifier |
| 4 | Phone / WhatsApp | Short answer | Yes | Event-day contact |
| 5 | **First-choice sub-division** | Dropdown | Yes | 8 sub-division options |
| 6 | **Second-choice sub-division** | Dropdown | Yes | Same 8 options |
| 7 | **Availability — Thursday 17 Sep** | Checkbox grid or checkboxes | Yes | One box per block |
| 8 | **Availability — Friday 18 Sep** | Checkbox grid or checkboxes | Yes | One box per block |
| 9 | Accessibility / scheduling notes | Paragraph | No | Read by a human only |

### 9.2 Availability capture — the important one

Present availability as **fixed, discrete blocks**, one checkbox each. Use **30-minute blocks** if your interview length may change to 30; use blocks equal to your smallest likely interview length otherwise.

```
Thursday 17 September          Friday 18 September
[ ] 18:00 – 18:30              [ ] 18:00 – 18:30
[ ] 18:30 – 19:00              [ ] 18:30 – 19:00
[ ] 19:00 – 19:30              [ ] 19:00 – 19:30
[ ] 19:30 – 20:00              [ ] 19:30 – 20:00
[ ] 20:00 – 20:30              [ ] 20:00 – 20:30
[ ] 20:30 – 21:00              [ ] 20:30 – 21:00
[ ] 21:00 – 21:30              [ ] 21:00 – 21:30
[ ] 21:30 – 22:00              [ ] 21:30 – 22:00
```

Add: *"Tick every block you could attend. You will be given two interviews of 20 minutes each. Please tick at least 4 blocks — the more you tick, the more likely you get your preferred times."*

Set validation: **minimum 4 selections across the two questions** (or per day, whichever Forms allows in your setup). Applicants who tick two blocks are the ones who generate red clashes.

A 20-minute interview inside 30-minute declared blocks means some slots straddle a block boundary. Configure the rule explicitly — **strict** (the whole interview must sit inside ticked blocks) or **lenient** (majority overlap counts). Recommend strict for alpha; simpler to explain to an applicant who complains.

### 9.3 Sub-division choice rules (Finding B)

Because same-parent pairs are **valid**, the form is simpler than it would otherwise be. Applicants pick from the flat list of 8 sub-divisions; the only rule is that the two picks must differ.

1. **Only one prohibition:** the same sub-division cannot be chosen twice. `validate.py` rejects `sub_division_1 == sub_division_2` and lists the applicant in the validation report.
2. **Allowed and expected:** Media Marketing + Media Documentation, or Creative + WebMaster. These produce two interviews with two (preferably different) panels of the shared parent division.
3. **Wording:** label options with their parent for clarity, e.g. `Media Marketing (MedMarDoc)`, `WebMaster (Creative)`, and add a note: *"You may pick two roles within the same division — for example Media Marketing and Media Documentation. You'll be interviewed separately for each. Just don't pick the same role twice."*
4. **Structure:** two dropdowns, `First choice` and `Second choice`, both listing all 8 sub-divisions. Do not use a single checkbox question — ordered choices carry preference information that is useful later at placement time.

### 9.4 Additional form hygiene

- Turn on "Limit to 1 response" if your members have accounts; otherwise rely on email dedupe.
- Set an explicit closing date and time; the pipeline should not be run against a live-changing sheet.
- Never edit the raw response sheet by hand. Corrections go in a separate override sheet.

---

## 10. Automated email to 120 applicants

You asked specifically how to do this twice: once for interview schedules, once for results. The mechanism is identical; only the template and the data source differ. Build it once.

### 10.1 Choose a sending method

| Method | Setup | Daily limit | Deliverability | Best for |
|---|---|---|---|---|
| **Google Apps Script + Gmail** (`MailApp` / `GmailApp`) | Lowest — lives in the Sheet, no server | Consumer Gmail ≈ **100 recipients/day**; Google Workspace ≈ **1,500/day**. *Verify current quotas before you rely on them — Google changes them.* | Good (real Gmail sender) | Committees with a Workspace account and no engineer available |
| **Gmail API from Python** (service account with domain-wide delegation, or OAuth) | Medium | Same Gmail quotas as above | Good | Our chosen architecture — keeps sending in the same pipeline as scheduling |
| **Transactional provider** (Resend, SendGrid, Mailgun, Amazon SES) | Medium — needs domain verification (SPF/DKIM) | Thousands+ | Best; proper bounce/open tracking | If you have a domain and want reliability, or you are on consumer Gmail |
| Plain SMTP | Low | Depends on host | Variable | Local testing only |

**Recommendation for IFF:** if you have Google Workspace, use the **Gmail API from the Python pipeline** — 120 sends sits comfortably inside the Workspace quota and everything stays in one codebase. If you're on consumer Gmail, the ~100/day limit sits right on top of your 120 recipients; use a transactional provider instead.

**Critical:** whichever you pick, send **one message per applicant**. Never CC or BCC a batch — it leaks addresses and looks like spam.

### 10.2 The send procedure (identical for invites and results)

```
1. BUILD      Join assignments (or decisions) → recipients table.
              One row per applicant, all merge fields resolved.

2. RENDER     Jinja2 template → HTML + plain-text for each recipient.
              Written to runs/<ts>/emails/<applicant_id>.html

3. AUDIT      Automated pre-send checks — hard fail on any of:
                • any merge field rendered empty or as "None"/"nan"
                • any invalid or duplicate email address
                • any applicant missing an assignment (invites)
                  or a decision (results)
                • counts don't match expectation
                  (e.g. "182 invites, 3 accepted, 79 rejected — confirm?")
              Then a human reads 3 random rendered samples.

4. APPROVE    Explicit gate. `--send` alone is not enough; require typing
              the expected recipient count to proceed.

5. SEND       Loop with:
                • ledger check — skip anyone already SENT (idempotency)
                • throttle — small sleep between sends
                • try/except — record FAILED with the error, keep going
                • write ledger row immediately after each send

6. RETRY      `iffsched notify ... --retry-failed` re-attempts FAILED rows
              only, with exponential backoff.

7. REPORT     Final summary: sent / skipped / failed, with the failure list.
```

The **ledger** (`send_ledger.csv`) is what makes this safe. A crash at recipient 63 is recoverable: re-run, and rows 1–62 are skipped automatically. Without it, a re-run means 62 people get a duplicate.

### 10.3 Invite email content

Merge fields: `full_name`, and for each of the two interviews `division_display`, `sub_division`, `day_label`, `date`, `start_time`, `end_time`, `room`.

Must include: both interview times in full, both rooms, arrival instructions and how early to arrive, what to bring, a contact person and channel for problems, and a clear statement that times are fixed unless they reply by a stated deadline. Attach `.ics` files if you build FR-55 — it materially reduces no-shows.

If an applicant's assignment is a **clash** (outside their stated availability), that email should be flagged for a human to send personally, with an apology and an explanation. Do not let the machine send that one silently.

### 10.4 Result email content

Merge fields: `full_name`, `decision`, `division_placed` (if accepted), `next_steps`, `deadline`, `contact`.

Use **separate templates per outcome** — accepted, waitlisted, rejected — and route by the decision column. Practical safeguards, given what's at stake:

- Run the audit twice. A merge-field error here means telling someone the wrong outcome.
- Verify the decision column is populated for **every** applicant before sending; a blank must be a hard failure, never a default to "rejected".
- Have a second person eyeball the accepted list and the rejected list separately before approval.
- Send them all in one batch, close together in time, so people aren't comparing notes across hours.
- Give rejected applicants a real next step (feedback availability, next intake, other ways to get involved). Make the template warm; a form rejection that reads like a mail merge is the thing people remember about an organisation.

---

## 11. Manual adjustment model

The rule that makes the tool trustworthy: **the solver may never overwrite a human decision.**

```
solve #1  ─→  assignments.csv
                 │
                 ▼
          recruiter edits in Sheet/XLSX
                 │
                 ▼
          edit_validator  ──── rejects illegal edits with a reason
                 │              (double-booked applicant, panel busy,
                 │               room over capacity, slot outside grid)
                 ▼
          iffsched lock  ─→  data/locks/pinned_assignments.csv
                 │
                 ▼
solve #2  ─→  locked rows fixed (C6), everything else re-optimised
                 │
                 ▼
          diff report: what moved, what didn't
```

Locks are cumulative and explicit. `iffsched lock --clear` exists but requires confirmation. New applicants arriving late are simply added to the pool and slotted into whatever the locks left free.

---

## 12. Edge cases and failure modes

Every one of these should have a test.

| # | Case | Handling |
|---|---|---|
| E-01 | Both choices share a parent division (MedMar + MedDoc, Creative + WebMaster) | **Valid.** Two interviews scheduled under that parent division, preferably with different panels (C8). Model is indexed by choice, not division, so both survive. See §1.2 Finding B. |
| E-01b | The same sub-division picked twice | **Reject** at validation; report for manual follow-up. |
| E-01c | Same-parent pair, but that division has only one panel | C8 auto-relaxes; the applicant sees the same panel twice at different times. Flagged AMBER in the conflict report so the recruiter can brief the panel. |
| E-02 | Applicant ticks no availability | Reject at validation; contact applicant. |
| E-03 | Applicant ticks very few blocks and cannot fit two non-overlapping interviews | Solver forces a clash; flagged RED with reason. |
| E-04 | Duplicate submissions from the same email | Keep latest by timestamp; report the collapse. |
| E-05 | Availability blocks don't align with the interview grid (30-min blocks, 20-min interviews) | Apply the configured strict/lenient rule consistently; document it. |
| E-06 | Division demand exceeds panel capacity | Capacity Advisor hard-stops before solving with a recommended panel count. |
| E-07 | Applicant's two divisions are in different rooms and assigned back-to-back | Set `min_gap_slots: 1` for cross-room pairs, or accept and note travel time. |
| E-08 | Day length not divisible by interview duration | Discard the trailing partial slot; report it. |
| E-09 | Panel unavailable for part of the event | `active_windows` in `panels.yaml`; solver respects it (C7). |
| E-10 | Late submissions after the schedule is published | Re-solve with all existing assignments locked; only new applicants move. |
| E-11 | Room over-capacity from too many panels | Enforced by C4; Capacity Advisor warns earlier. |
| E-12 | Manual edit creates a double-booking | `edit_validator` rejects with a specific message; never silently accepted. |
| E-13 | Email send crashes partway through | Ledger allows safe resume; no duplicates. |
| E-14 | Applicant email bounces | Recorded FAILED in ledger; surfaced in the failure report for manual contact. |
| E-15 | No-show on the day | Mark attendance; slot freed; optionally pull a later applicant forward. |
| E-16 | Timezone confusion (form vs export vs ICS) | Single configured timezone used everywhere; stated explicitly in every email. |
| E-17 | Solver hits its time limit | Returns the best solution found with its optimality gap; never returns nothing. |
| E-18 | Solver proves infeasibility even with clashes allowed | Impossible if any panel-slot capacity exists ≥ demand; if it happens, it's a config error — report which constraint is binding. |

---

## 13. Phasing and milestones

### Alpha (this document)

| M | Milestone | Definition of done |
|---|---|---|
| M0 | Config + domain model | Slot grid generates correctly from `event.yaml`; changing duration 20→30 regenerates the grid; tests pass |
| M1 | Ingest + validate | Real form export produces `applicants.clean.csv` and a validation report; E-01…E-05 all caught |
| M2 | Capacity Advisor | Produces the per-division table; correctly flags the Finding A shortfall |
| M3 | CP-SAT solver | Every applicant gets 2 interviews; C1–C8 verified by tests; 240-interview instance solves <60s |
| M4 | Exports | Room, applicant and panel views produced; clashes highlighted red; conflict report generated |
| M5 | Locks + re-solve | Manual edit survives a re-solve; illegal edits rejected |
| M6 | Invite emails | Dry-run renders all 120 emails correctly; ledger prevents duplicates; live send succeeds |
| M7 | Results emails | Scoring intake → decision column → three templates → audited send |
| M8 | Runbook | A committee member who didn't build it can run the whole thing from `RUNBOOK.md` |

### Beta (not in scope now)
**Deployment:** Vercel (Next.js frontend) + FastAPI (scheduler API) + PostgreSQL via Supabase (persistent storage — replaces the CSV/YAML store so data survives deploys).

The alpha architecture is designed for this transition: all I/O sits behind adapter protocols, so `CsvSource` → `PostgresSource` and `XlsxWriter` → `PostgresWriter` are swaps, not rewrites. Domain models must be kept free of file handles and path strings.

Additional beta features: drag-and-drop timetable editor · live event-day dashboard · WhatsApp notifications · interviewer availability self-service · multi-round support.

---

## 14. Open questions

Please answer these before implementation starts; they change the design.

> **Resolved as of v1.1:**
> — *Applicant cap:* **120**. Baseline built against 240 interviews (§1.2, Finding A).
> — *Same-parent pairs:* **valid**; they produce two separate interviews (§1.2, Finding B).

**Capacity and logistics**
1. ~~How many applicants?~~ **Answered: 120 cap.**
2. Can IFF staff **12 interviewer panels**? This is the baseline the whole plan rests on. If not, say what number is realistic and we pick a lever from the alternatives table.
3. Can you get more than 2 rooms? This is the cheapest fix to the capacity problem.
4. **Are 2014 and 2016 classrooms or halls?** The baseline needs 6 simultaneous panels per room. If they're classrooms, `max_concurrent_panels` is 1–2 and you need 6 rooms, not 2. This is the single most important unanswered question.
5. Is a third day possible if the numbers don't fit?

**Scheduling rules**
6. What timezone? (Assumed Asia/Jakarta — confirm.)
7. Must the two interviews be on the same day, or is one on Thursday and one on Friday acceptable?
8. Should there be a minimum gap between an applicant's two interviews? What about travel time between rooms?
9. Do interviewers need scheduled breaks? How long, how often?
10. Is the second-choice division a genuine second interview, or a fallback only if the first fails? (Spec says both interviews always happen — confirming.)

**Data**
11. ~~Sub-divisions or parent divisions on the form?~~ **Answered: flat list of 8 sub-divisions, two ordered dropdowns, only rule is they must differ (§9.3).**
11b. If both choices share a parent division, should the applicant be told in advance that both interviews are with the same division team? (Affects invite email wording.)
12. What availability block size — 30 minutes, or 20 to match the interview length?
13. Is availability strict (whole interview inside a ticked block) or lenient?

**Email**
14. Is IFF on Google Workspace or consumer Gmail? This decides the sending method (§10.1).
15. Do you own a domain you can send from? (Needed for a transactional provider.)
16. What sender address and reply-to should applicants see?
17. What outcomes exist — accepted / rejected only, or is there a waitlist?

**Process**
18. Who is the single approver for the send gates?
19. When does the form close, and when must invites go out?
20. When are results due to be sent?

---

*End of document. Sections 3 through 9 are the authoritative spec — they supersede the original brief. Answers to §14 should be folded back in as version 1.2 before implementation begins.*
