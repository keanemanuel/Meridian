# Session 3 — Export to Google Sheets + 404 Fixes
## Model: Sonnet | Priority: HIGH

```
Read CLAUDE.md.

Two backend tasks:

## Task 1: Export to Google Sheets endpoint

Add POST /api/workspaces/{id}/runs/{runId}/export/sheets

This endpoint should:
1. Load the run's assignments from Supabase (or CSV fallback)
2. Create a new Google Sheet using the service account
3. Format it like a real interview timetable:

   Sheet name: "Thu 17 Sep" and "Fri 19 Sep" (one sheet per day)
   
   Layout per sheet:
   Row 1: Merged header — "Thursday, 18 September 2026 — Room 2016"
   Row 2: Headers — Time (AEST) | Interviewer 1 | Interviewer 2 |
           Applicant Name | Arrived? | Interview?
   Rows 3+: One row per 20-min slot
             Time | (blank) | (blank) | Applicant full name | □ | □
   
   - Create one section per room per day
   - Clash rows highlighted in red (background color)
   - Empty slots left blank
   - Bold headers
   - Auto-resize columns

4. Return the Google Sheet URL in the response:
   {"sheet_url": "https://docs.google.com/spreadsheets/d/..."}

5. Share the created sheet with anyone who has the link (viewer)
   so committee members can open it without a Google account

Use gspread for sheet creation (already installed).
Use the same service account credentials as Sheets ingest.

Add to src/api/routers/pipeline.py or a new routers/export.py.
Add the route to src/api/main.py.

## Task 2: Fix all 404 errors in the frontend

The most common 404: clicking a run in the history list goes to
/workspace/[id]/runs/[runId] and shows 404.

Debug and fix:
1. Check the run_id format being passed to the URL
2. Check GET /api/workspaces/{id}/runs/{runId} returns the right data
3. Check the frontend is constructing the URL correctly
4. Also check: after "Schedule!", does the run appear in history?
   If not, fix the run list refresh after solving.

After both tasks:
- ruff check, mypy, pytest (excluding Supabase-flaky test_api tests)
- Commit and push
```
