# Session 2 — Frontend UI Overhaul
## Model: Opus | Priority: HIGH

```
Read CLAUDE.md.

Major frontend UI update. The app is deployed at
meridian-pink-three.vercel.app with Railway API at
meridian-production-f9c9.up.railway.app.

## Change 1: Rename "Meridian" → "IFF Recruitment"
- Update the top bar title from "Meridian" to "IFF Recruitment"
- Update page title/metadata in app/layout.tsx
- Do NOT change any variable names, API routes, or code — only
  display text changes

## Change 2: Remove em dashes from all UI text
Replace all " — " and "—" in displayed text with ", " or ". "
or rewrite naturally. The app should sound human-written.
Check: landing page description, tooltips, empty states,
error messages, button labels.

## Change 3: Workspace rename/delete with warnings
- Test Environment group: rename and delete allowed with a
  simple "Are you sure?" confirm dialog
- IFF Submissions group: rename allowed but show a strong
  warning modal: "This is the live IFF recruitment workspace.
  Are you absolutely sure you want to rename it?"
  Delete NOT allowed for IFF Submissions — show error:
  "Live submission workspaces cannot be deleted."
- Implement in frontend/components/Sidebar.tsx

## Change 4: Import Data modal improvements
- Show two clear options: "Google Sheet" and "CSV Upload"
- If a Google Sheet URL is already linked to the workspace,
  show it in the Google Sheet option (pre-filled, still editable)
- If no sheet linked, Google Sheet option shows empty input
- CSV Upload remains as file picker
- Remove the yellow warning about set-sheet CLI command

## Change 5: Drag-and-drop schedule adjustment
In the schedule view (Room View tab):
- Make each applicant cell draggable
- When dragging, show valid drop targets (other empty slots
  in the same room) highlighted in blue
- On drop: call PATCH /api/workspaces/{id}/runs/{runId}/
  assignments/{assignmentId} with new panel_id and slot_id
- Show a lock icon on manually moved cells
- Add "Undo" button in the top bar that reverts the last
  manual move (keep a local history stack of up to 10 moves)
- Use HTML5 drag and drop API (no external library needed)

## Change 6: Thursday/Friday swipe navigation
The Room View should show one day at a time with navigation:
- Default: show Thursday (2026-09-17) timetable
- ">" button on the right side → swipe/slide to Friday view
- "<" button on the left side of Friday → back to Thursday
- Show current day label clearly: "Thursday 17 Sep" / "Friday 18 Sep"
- Smooth CSS transition between days
- Each day shows only the rooms active that day

## Change 7: Rename "Solve" → "Schedule!"
- Change the Solve button label to "Schedule!"
- Update any related toast messages: "Scheduling..." while running,
  "Schedule complete! X/Y interviews placed" on success
- The X/Y counter: X = interviews placed, Y = total required
  (show this prominently after scheduling)

## Change 8: Export to Google Sheets button
Add an "Export" button next to "Schedule!" button.
On click:
- Call POST /api/workspaces/{id}/runs/{runId}/export/sheets
- Show a loading state "Exporting to Google Sheets..."
- On success: show a toast with a link to the Google Sheet
- The export format should match the reference timetable:
  - One sheet per room
  - Columns: Time slot, Interviewer 1, Interviewer 2,
    Applicant Name, Arrived?, Interview?
  - Rows: each 20-minute time slot
  - Header row: day and date
  - Color coding: scheduled=white, clash=red highlight

## Change 9: Landing page copy cleanup
Update the landing page (app/page.tsx) empty state text:
- Remove all em dashes
- Make it sound friendly and human
- Something like: "Welcome to IFF Recruitment. Select a workspace
  from the sidebar or create a new one to get started."

Verify: tsc, eslint, next build all pass. Commit and push.
```
