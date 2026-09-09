# Session 1 — Railway Fix + Backend CSV Fix
## Model: Sonnet | Priority: CRITICAL — do this first

```
Read CLAUDE.md.

Two critical fixes needed:

## Fix 1: Railway deployment (URGENT)
Railway is failing with "No start command detected".
The Procfile and railway.json exist but Railway isn't reading them.

Add a nixpacks.toml to the repo root:
```toml
[phases.install]
cmds = ["pip install -r requirements.txt"]

[start]
cmd = "cd /app/src && uvicorn api.main:app --host 0.0.0.0 --port $PORT"
```

Also update Procfile to:
```
web: cd /app/src && uvicorn api.main:app --host 0.0.0.0 --port $PORT
```

Also update railway.json to:
```json
{
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "cd /app/src && uvicorn api.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/api/health",
    "healthcheckTimeout": 300,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

## Fix 2: CSV rejection bug (CRITICAL)
Currently only <10% of participants pass ingest. ALL valid
registrants must be accepted. The real IFF form columns are:

Timestamp, Full Name, University, Major, State of Degree,
Student ID, Proof of Student Enrolment, Phone Number (WhatsApp),
Email Address, Current Location (e.g.: CBD, etc),
Social Media Accounts (Optional), First Preference,
Second Preference, Preferred Interview Date,
Why do you want to join IFF?, CV / Resume,
Google Drive Link, Required Files, Email Confirmation, Column 1

Key issues to fix in src/iff_scheduler/ingest/normalize.py:

1. "Preferred Interview Date" values look like:
   "Thursday, 18 September 2025, 18.00 - 21.30 AEST"
   "Friday, 19 September 2025, 18.00 - 21.30 AEST"
   "Thursday, 18 September 2025, 18.00 - 21.30 AEST"
   Parse ONLY the day name (Thursday/Friday) — map to event date.
   Ignore the year entirely — match by weekday name only.
   Thursday → all slots on 2026-09-17
   Friday → all slots on 2026-09-18

2. Sub-division values from the real form:
   "Logistics" → LOGISTICS
   "Creative and Decor (Design and Decor)" → CREATIVE
   "Creative and Decor (WebMaster)" → CREATIVE  
   "Media Marketing and Documentation (Documentation)" → MEDMARDOC
   "Media Marketing and Documentation (Media Marketing)" → MEDMARDOC
   "Finance and Booth" → FNB
   "Program" → PROGRAM
   "Liaison" → LIAISON
   
   Update config/divisions.yaml sub_division_mapping to include
   ALL these exact string values from the real form.

3. Column "Current Location (e.g.: CBD, etc)" has a comma in the
   name — make sure the CSV parser handles quoted headers correctly.

4. "Column 1" and other extra trailing columns should be silently
   ignored, not cause rejections.

5. Any row with a valid email, full name, first preference,
   second preference, and preferred interview date should PASS.
   Only reject: blank email, blank name, blank both preferences,
   or EXACT duplicate sub-division (same sub-div chosen twice).
   
6. The NO_AVAILABILITY rejection must never trigger for real form
   data — if Preferred Interview Date contains "Thursday" or
   "Friday", that day's full window is available. Only reject
   NO_AVAILABILITY if the date field is completely blank.

After fixing normalize.py and divisions.yaml, also fix
the email sending limit:
- Gmail API via OAuth2 sends ~500/day for consumer, 2000/day
  for Workspace
- For 300 emails/day we need to confirm we are within limits
- Add throttle: 2 seconds between sends (config/notify.yaml:
  throttle_seconds: 2)
- This gives ~150 emails/hour, well within Gmail limits

Run pytest (excluding test_api.py Supabase-flaky tests) and
confirm 153+ tests pass. Commit and push.
```
