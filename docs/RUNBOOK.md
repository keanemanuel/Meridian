# RUNBOOK

Step-by-step instructions for running the IFF interview scheduler end to
end — from the raw form export to the last result email. Written for
whoever is running the event, not whoever built the tool. If a step asks
you to open a CSV, any spreadsheet program (Excel, Google Sheets, Numbers)
works fine.

If something goes wrong, **stop and read the red text on screen** — the
tool is designed to fail loudly with a specific reason rather than guess.
Every failure message tells you what to fix. Nothing you do here can
double-send an email or silently overwrite a manual edit; that is enforced
by the tool itself (see `CLAUDE.md`, "Non-negotiable invariants," if you
want the technical version).

---

## 0. One-time setup

You only do this once, when the tool is first installed on a machine.

1. Make sure Python 3.11 or newer is installed (`python3 --version`).
2. From the project folder:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```
3. Copy `.env.example` to `.env` and fill in the values someone on the
   tech team gives you (a Google service account file path for reading the
   applicant Sheet, and either the Gmail OAuth2 setup below or a
   transactional email provider key). **Never** share this file or commit
   it to git — it stays on your machine.
4. Open every file in `config/` and check the placeholder values (empty
   strings `""`) have been filled in for your event: dates, rooms, panels,
   sender name, contact person, RSVP deadline, and — new for results —
   `next_steps_accepted`, `next_steps_waitlist`, `next_steps_rejected` and
   `result_deadline` in `config/notify.yaml`. **The result-send step
   refuses to run at all if any of the three `next_steps_*` fields are
   blank** — this is deliberate, so nobody accidentally sends a rejection
   with no real next step.
5. If you're sending via Gmail, do the **one-time OAuth2 setup** below.
   Skip it if you're using a transactional provider instead.

You're set up. Steps 1 onward are what you run for every event, and step 7
(results) again after interviews are scored.

### One-time OAuth2 setup for Gmail sending

Invite and result emails send from *your own* Gmail account via OAuth2 —
there is no shared service account for sending, so each sender authorises
themselves once.

1. Ask the tech team for the project's OAuth2 **client credentials** file
   (created in Google Cloud Console as an "OAuth client ID" of type
   "Desktop app", with the Gmail API enabled and the
   `gmail.send` scope requested). Save it locally, e.g. at
   `credentials/gmail_oauth.json`.
2. In `.env`, set:
   ```
   GMAIL_OAUTH_CREDENTIALS=credentials/gmail_oauth.json
   GMAIL_TOKEN_CACHE=credentials/gmail_token.json
   GMAIL_SENDER_EMAIL=you@yourorganisation.org
   ```
   `GMAIL_SENDER_EMAIL` must be the Gmail address you're about to
   authorise in the next step — it's what recipients see in the "From"
   line.
3. The **first** time you run `notify invite --send` or
   `notify result --send`, a browser tab opens automatically asking you to
   sign in to that Gmail account and grant "Send email on your behalf".
   Approve it. The tool caches the resulting token at
   `GMAIL_TOKEN_CACHE` (`credentials/gmail_token.json` by default) and
   never shows the browser prompt again on that machine, unless that file
   is deleted or Google revokes the grant.
4. **Never** commit `credentials/gmail_oauth.json` or
   `credentials/gmail_token.json` — both are already covered by
   `.gitignore`. Anyone with the token file can send email as you until
   you revoke it at
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

If you ever need to re-authorise as a different Gmail account (e.g. handing
the role to someone else), delete `credentials/gmail_token.json` and the
next send will prompt for consent again.

---

## 1. Bring in applicants

```bash
iffsched ingest --source csv --input data/raw/responses.csv
```

(Or `--source sheets` once Google Sheets access is configured.)

This reads the raw form export and writes two files to `data/interim/`:

- `applicants.clean.csv` — everyone who passed validation
- `validation_report.csv` — everyone who didn't, and why

**Open `validation_report.csv`.** Every REJECTED row is someone who is
**not** in the schedule until you fix the underlying problem (usually: they
need to be contacted to fix a typo'd email, or re-submit availability) and
re-run this step. The tool never guesses a missing answer — it would
rather leave someone out and tell you than schedule them wrong.

---

## 2. Check you have enough interviewers

```bash
iffsched check
```

This prints a table: for every division, how many interviews are needed
vs. how many your configured panels can actually run. If anything shows
**INFEASIBLE**, the command stops here (exit code 1) — do not proceed to
step 3 with your current panel configuration.

Fix it by editing `config/panels.yaml` (add panels), `config/rooms.yaml`
(add rooms or room capacity), or `config/event.yaml` (extend hours). Then
run `iffsched check` again until every division is OK or TIGHT.

---

## 3. Build the timetable

```bash
iffsched solve
```

This can take anywhere from a few seconds to about a minute. When it
finishes, it writes an immutable folder under `runs/<timestamp>/` — the
`assignments.csv` inside is the actual schedule. The command also prints:

- interviews placed (should read `240 / 240`, or `2 × applicants`)
- **clashes** — applicants placed outside a time they said they were free.
  Zero is the goal, but a handful is sometimes unavoidable if someone
  ticked very few availability boxes. Every clash is listed by name and
  reason in `runs/<timestamp>/conflicts.csv`.

---

## 4. Generate readable views

```bash
iffsched publish --run latest
```

Writes `data/output/<run>/schedule.xlsx` (one workbook, multiple tabs:
room views, the full applicant list, panel views) and matching HTML pages.
Clashes are highlighted **red** in the spreadsheet. Share this with room
coordinators and panel leads.

---

## 5. Manual adjustments (optional)

If a recruiter needs to hand-move someone (e.g. swap two applicants'
rooms), edit `runs/latest/assignments.csv` directly (or edit the
equivalent Google Sheet if that's your workflow), then freeze the edit so
the solver never undoes it:

```bash
iffsched lock --from runs/latest/assignments.csv
```

If the edit created a double-booking or another illegal state, this
command **refuses** and tells you exactly which row and why — nothing is
locked until it's fixed. Once it's clean:

```bash
iffsched solve      # re-solves around your locked rows; nothing you fixed can move
iffsched publish --run latest
```

---

## 6. Send interview invites

**Always dry-run first:**

```bash
iffsched notify invite --run latest --dry-run
```

This renders every applicant's invite email to
`runs/latest/emails/<applicant_id>.html` — open a few and read them.
Anyone whose schedule has a clash (outside their stated availability) is
held out of the automated batch and listed in
`runs/latest/emails/manual_review.csv` — **send those personally, with an
apology**, don't let the machine do it silently.

When you're satisfied, send for real:

```bash
iffsched notify invite --run latest --send
```

You'll be asked to **type the number of recipients** shown on screen to
confirm — this is intentional friction, not a bug. If the command is
interrupted partway (crash, closed laptop, whatever), just run it again:
already-sent applicants are automatically skipped, so nobody gets a
duplicate.

---

## 7. After interviews: send results

This is the highest-stakes step in the whole pipeline — you are telling
people whether they got in. Take it slowly.

### 7.1 Get the scores into a CSV

Once the committee has scored everyone, produce a CSV with exactly these
columns and save it somewhere findable (e.g. `data/raw/scores.csv` —
this folder is gitignored, so it's safe to put real names here):

| Column | Required when | Example |
|---|---|---|
| `applicant_id` | always | `A014` (from `applicants.clean.csv`) |
| `decision` | always | `Accepted`, `Waitlist`, or `Reject` |
| `division_placed` | only if `decision` is Accepted | `CREATIVE` (a division code from `config/divisions.yaml`) |

**Every single applicant in `applicants.clean.csv` needs a row here.** A
blank or missing decision is not treated as "reject" — the tool refuses to
send anything at all until every applicant has an explicit decision. This
is deliberate: a spreadsheet formula error should never silently turn into
a rejection nobody meant to send.

### 7.2 Dry-run

```bash
iffsched notify result --scores data/raw/scores.csv --run latest --dry-run
```

If anything in the scores CSV is wrong — a blank decision, an unrecognised
word, an ACCEPTED row with no division, two rows for the same person — the
command **stops immediately, sends nothing, and prints exactly which
row(s) and why**. Fix the scores CSV and re-run this command until it
succeeds cleanly.

Once it succeeds, it writes to `runs/latest/results_emails/`:

- `accepted_list.csv`, `waitlist_list.csv`, `rejected_list.csv` — one row
  per applicant per outcome
- one rendered `.html` + `.txt` email per applicant
- a summary table of how many are in each bucket

### 7.3 Get a second person to check the lists — mandatory

**Before you send anything**, hand `accepted_list.csv` and
`rejected_list.csv` to a second committee member and have them check both
against the committee's own scoring records, independently of you. This
is the step most likely to save you from an embarrassing mistake, and the
tool enforces it structurally: **`--send` will refuse to run unless you
name that person.**

### 7.4 Send

```bash
iffsched notify result --scores data/raw/scores.csv --run latest \
  --send --verified-by "Their Full Name"
```

- If `--verified-by` is missing or blank, the command refuses and tells
  you which two files to have checked first.
- You'll then be asked to type the number of pending recipients, same as
  invites.
- Once confirmed, the verifier's name and a timestamp are written to
  `runs/latest/results_emails/verification_log.csv` — a permanent record
  of who signed off, for later reference.
- Send everyone in one sitting, close together in time — don't let
  accepted and rejected applicants compare notes hours apart.

As with invites, a crash partway through is safe to recover from: just
re-run the same command and already-sent applicants are skipped
automatically.

---

## Command reference

```
iffsched ingest --source csv --input <path>      # → applicants.clean.csv + validation_report.csv
iffsched check                                   # Capacity Advisor — run before every solve
iffsched solve                                   # → runs/<timestamp>/
iffsched publish --run latest                    # → room / applicant / panel views
iffsched lock --from runs/latest/assignments.csv # freeze a manual edit
iffsched notify invite --run latest --dry-run
iffsched notify invite --run latest --send
iffsched notify result --scores <path> --run latest --dry-run
iffsched notify result --scores <path> --run latest --send --verified-by "Name"
```

Every command accepts `--config-dir` to point at a different `config/`
folder, and `--help` for the full option list, e.g. `iffsched notify
result --help`.

## If something looks wrong

- **Red text on screen**: read it fully before doing anything else — it
  names the specific applicant/row and the specific rule that was
  violated. Fix that, then re-run the same command.
- **An email looks wrong after sending**: the ledger at
  `data/ledger/send_ledger.csv` records exactly what was sent to whom and
  when. It never gets rewritten retroactively — treat it as the source of
  truth for "did X get email Y."
- **You need to undo a lock**: `iffsched lock --clear` (asks for
  confirmation before deleting).
- **Still stuck**: check `docs/SPEC.md` §12 ("Edge cases and failure
  modes") — most surprising behaviour is there, documented as intentional.
