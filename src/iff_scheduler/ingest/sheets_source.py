"""Google Sheets adapter for ApplicantSource, plus incremental ingest (FR-01,
FR-07; SPEC.md §4.2 Stage 3 "API path (preferred)"; CLAUDE.md M10).

Only this module touches gspread/google-auth to read applicant data — the
core stays free of adapter imports, and `ingest/validate.py` never knows
whether a row came from a CSV export or a live Sheet (CLAUDE.md, "Architecture
rule").

**Incremental model.** The Sheet is append-only raw data (SPEC.md §4.2 Stage
2): people only ever add new form responses, never edit past rows. So "new
data since last time" is exactly the rows after a row-count watermark, stored
in `data/workspaces/<workspace>/last_ingested_row.txt`
(`workspace.last_ingested_row_path`). Each ingest run:

1. Reads the *entire* sheet (gspread has no cheap "rows after N" read).
2. Slices off everything at or before the watermark.
3. Runs only the new slice through the normal parse/validate pipeline,
   continuing the row-number and applicant-ID sequence of the existing
   `applicants.clean.csv` (`run_ingest`'s `row_number_offset` /
   `applicant_id_offset`), and rejecting any email already present in it
   (`known_emails`) rather than guessing which submission is authoritative.
4. Appends the new clean rows and report rows, then advances the watermark
   past every row just read — rejected rows are not retried on the next run.

`--force` resets the watermark to zero and known_emails to empty, so the
whole sheet is re-parsed from scratch and the outputs are overwritten rather
than appended — this is the only path that re-applies FR-05's cross-submission
"most recent wins" dedupe across the full history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.ingest.base import ApplicantSource
from iff_scheduler.ingest.validate import IngestResult, run_ingest
from iff_scheduler.settings import DivisionsConfig, EventConfig

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class GspreadWorksheet(Protocol):
    """The one gspread.Worksheet method this adapter needs — narrow enough
    that tests can pass an in-memory fake instead of a real Sheet."""

    def get_all_values(self) -> list[list[str]]: ...


def open_worksheet(
    service_account_file: str, sheet_id: str, worksheet_name: str | None = None
) -> GspreadWorksheet:
    """Build a real gspread worksheet handle from a service account key
    (mirrors `notify/gmail_mailer.py`'s credential pattern)."""
    credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        service_account_file, scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(worksheet_name) if worksheet_name else spreadsheet.sheet1


@dataclass(frozen=True)
class SheetsApplicantSource:
    """Reads the linked Google Form response sheet in full.

    Same string-dtype, no-NaN contract as `CsvApplicantSource` — a blank
    cell stays `""`, never becomes the literal text "nan" downstream.
    """

    worksheet: GspreadWorksheet

    def read_raw(self) -> pd.DataFrame:
        values = self.worksheet.get_all_values()
        if not values:
            return pd.DataFrame()
        header, *rows = values
        return pd.DataFrame(rows, columns=header, dtype=str).fillna("")


@dataclass(frozen=True)
class _FrameSource:
    """Wraps an already-sliced DataFrame as an ApplicantSource so the
    incremental batch can go through the same `run_ingest` entry point as a
    full read."""

    frame: pd.DataFrame

    def read_raw(self) -> pd.DataFrame:
        return self.frame


def read_watermark(path: Path) -> int:
    """Number of raw sheet rows already processed. Missing file means
    nothing has been ingested from this Sheet yet — not an error."""
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    return int(text) if text else 0


def write_watermark(path: Path, row_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(row_count), encoding="utf-8")


def _existing_applicant_emails(clean_path: Path) -> frozenset[str]:
    if not clean_path.exists():
        return frozenset()
    df = pd.read_csv(clean_path, dtype=str, keep_default_na=False, na_filter=False)
    if "email" not in df.columns:
        return frozenset()
    return frozenset(df["email"])


def _existing_applicant_count(clean_path: Path) -> int:
    if not clean_path.exists():
        return 0
    return len(pd.read_csv(clean_path, dtype=str, keep_default_na=False, na_filter=False))


@dataclass
class IncrementalIngestResult:
    """What one `run_incremental_sheets_ingest` call did, for the CLI to
    report and to decide append-vs-overwrite and the next watermark."""

    result: IngestResult
    new_row_count: int
    watermark_before: int
    watermark_after: int


def run_incremental_sheets_ingest(
    source: ApplicantSource,
    event: EventConfig,
    divisions: DivisionsConfig,
    grid: SlotGrid,
    clean_path: Path,
    watermark_path: Path,
    force: bool = False,
) -> IncrementalIngestResult:
    raw_df = source.read_raw()
    watermark_before = 0 if force else read_watermark(watermark_path)
    new_raw_df = raw_df.iloc[watermark_before:].reset_index(drop=True)

    known_emails = frozenset() if force else _existing_applicant_emails(clean_path)
    applicant_id_offset = 0 if force else _existing_applicant_count(clean_path)

    result = run_ingest(
        source=_FrameSource(new_raw_df),
        event=event,
        divisions=divisions,
        grid=grid,
        row_number_offset=watermark_before,
        applicant_id_offset=applicant_id_offset,
        known_emails=known_emails,
    )

    return IncrementalIngestResult(
        result=result,
        new_row_count=len(new_raw_df),
        watermark_before=watermark_before,
        watermark_after=watermark_before + len(new_raw_df),
    )
