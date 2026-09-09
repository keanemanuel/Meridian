# Session 4 — Real Data Test + Google Sheets Ingest Live Test
## Model: Sonnet | Priority: DO THIS LAST, after Sessions 1-3

```
Read CLAUDE.md.

End-to-end real data verification. Do this after Sessions 1-3
are complete and Railway has redeployed successfully.

## Step 1: Verify Railway is up
Run:
  curl https://meridian-production-f9c9.up.railway.app/api/health

If not {"status":"ok"}, stop and report the error.

## Step 2: Test CSV ingest with real data
The real IFF form CSV has been analysed. A sample row looks like:
  12/09/2025 14:20:59, jessica mulyanto, monash, business,
  year 1 semester 2, 36021679, [proof link], 0421824484,
  jejes11223@gmail.com, 18 leicester street, jesjesi.ca (instagram),
  Logistics, Creative and Decor (Design and Decor),
  "Thursday, 18 September 2025, 18.00 - 21.30 AEST",
  [essay], [cv link], , [required files], TRUE, , ,

Run ingest against tests/fixtures/applicants_raw.csv and verify:
- 0 rejections for valid data (anyone with email + name +
  two preferences + a date should pass)
- Check the division mapping handles all these exact strings:
  "Creative and Decor (Design and Decor)"
  "Creative and Decor (WebMaster)"  
  "Media Marketing and Documentation (Documentation)"
  "Media Marketing and Documentation (Media Marketing)"
  "Finance and Booth"
  "Program"
  "Liaison"
  "Logistics"

If any of these are rejected as UNKNOWN_SUBDIVISION, fix
config/divisions.yaml sub_division_mapping.

## Step 3: Create a realistic 10-row test fixture
Create tests/fixtures/iff_real_format.csv with 10 rows
matching the exact real IFF form format (use fake names/emails).
Include:
- 2 rows choosing Thursday only
- 2 rows choosing Friday only  
- 2 rows with same-parent choices (e.g. Media Mktg + Media Doc)
- 1 row with duplicate email (to test deduplication)
- 1 row with identical choices (to test rejection)
- 2 normal rows with different divisions

Run ingest on this fixture and confirm:
- 8 clean applicants (or 7 after dedup)
- 1 rejected (duplicate choices)
- 1 collapsed (duplicate email)
- 0 unexpected rejections

## Step 4: Run full pipeline end-to-end
  iffsched ingest --source csv --input tests/fixtures/iff_real_format.csv --workspace "Test Run 1"
  iffsched check --workspace "Test Run 1"
  iffsched solve --workspace "Test Run 1"
  iffsched publish --workspace "Test Run 1" --run latest

Report the solve summary (interviews placed / total).

## Step 5: Update tests
Add tests/test_real_format.py that tests the real column
mapping end-to-end. This ensures a future code change never
silently breaks the real form parsing.

Commit and push everything.
```
