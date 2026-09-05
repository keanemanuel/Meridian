"""Tests for M10 incremental Google Sheets ingest (CLAUDE.md M10; SPEC.md
§4.2 Stage 3 "API path").

Uses an in-memory fake in place of a real gspread worksheet — `sheets_source`
only depends on `get_all_values()`, so no credentials or network are needed.
These tests cover:

1. `SheetsApplicantSource.read_raw()` shapes a Sheet the same way
   `CsvApplicantSource` shapes a CSV export.
2. Watermark round-trips, and defaults to 0 when absent.
3. A run only processes rows after the watermark, continues the row-number
   and applicant-ID sequence of the existing clean CSV, and advances the
   watermark past every row it read (not just the accepted ones).
4. A resubmission that lands in a later incremental batch is rejected
   (invariant 3: nothing is guessed about which submission is authoritative)
   rather than silently appended as a second record.
5. `--force` (force=True) ignores the watermark and known emails, reprocessing
   the whole Sheet from scratch with FR-05 cross-submission dedupe restored.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.ingest.sheets_source import (
    SheetsApplicantSource,
    read_watermark,
    run_incremental_sheets_ingest,
    write_watermark,
)
from iff_scheduler.ingest.validate import append_outputs, write_outputs
from iff_scheduler.settings import load_settings

HEADER = [
    "Timestamp",
    "Email address",
    "Full name",
    "Phone / WhatsApp",
    "First-choice sub-division",
    "Second-choice sub-division",
    "Availability — Thu",
    "Availability — Fri",
    "Accessibility / scheduling notes",
]

THU_AVAILABILITY = "18:00-18:20, 18:20-18:40, 18:40-19:00, 19:00-19:20"


def _row(
    timestamp: str, email: str, full_name: str, sub_1: str = "Creative", sub_2: str = "WebMaster"
) -> list[str]:
    return [timestamp, email, full_name, "+62-812", sub_1, sub_2, THU_AVAILABILITY, "", ""]


class FakeWorksheet:
    """Stands in for gspread.Worksheet — mutable so a test can simulate new
    form responses landing between two ingest runs."""

    def __init__(self, header: list[str], rows: list[list[str]]) -> None:
        self.header = header
        self.rows = rows

    def get_all_values(self) -> list[list[str]]:
        return [self.header, *self.rows]


def _settings_and_grid():  # type: ignore[no-untyped-def]
    settings = load_settings()
    return settings, build_slot_grid(settings.event)


# --------------------------------------------------------------- read_raw


def test_read_raw_shapes_sheet_like_csv_source() -> None:
    worksheet = FakeWorksheet(HEADER, [_row("2026-08-01T09:00:00", "ayu@example.com", "Ayu")])
    df = SheetsApplicantSource(worksheet=worksheet).read_raw()

    assert list(df.columns) == HEADER
    assert len(df) == 1
    assert df.iloc[0]["Email address"] == "ayu@example.com"


def test_read_raw_empty_sheet_is_empty_frame() -> None:
    worksheet = FakeWorksheet(HEADER, [])
    assert SheetsApplicantSource(worksheet=worksheet).read_raw().empty


def test_read_raw_no_values_at_all_is_empty_frame() -> None:
    class BlankWorksheet:
        def get_all_values(self) -> list[list[str]]:
            return []

    assert SheetsApplicantSource(worksheet=BlankWorksheet()).read_raw().empty


# --------------------------------------------------------------- watermark


def test_read_watermark_missing_file_is_zero(tmp_path: Path) -> None:
    assert read_watermark(tmp_path / "last_ingested_row.txt") == 0


def test_watermark_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "last_ingested_row.txt"
    write_watermark(path, 7)
    assert read_watermark(path) == 7


# --------------------------------------------------------- incremental flow


def test_first_run_processes_every_row_from_watermark_zero(tmp_path: Path) -> None:
    settings, grid = _settings_and_grid()
    worksheet = FakeWorksheet(
        HEADER,
        [
            _row("2026-08-01T09:00:00", "ayu@example.com", "Ayu"),
            _row("2026-08-01T09:05:00", "bagas@example.com", "Bagas"),
        ],
    )
    clean_path = tmp_path / "applicants.clean.csv"
    watermark_path = tmp_path / "last_ingested_row.txt"

    incremental = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )

    assert incremental.new_row_count == 2
    assert incremental.watermark_before == 0
    assert incremental.watermark_after == 2
    assert [a.applicant_id for a in incremental.result.applicants] == ["A001", "A002"]
    assert {a.email for a in incremental.result.applicants} == {
        "ayu@example.com",
        "bagas@example.com",
    }


def test_second_run_only_sees_rows_after_the_watermark(tmp_path: Path) -> None:
    settings, grid = _settings_and_grid()
    worksheet = FakeWorksheet(
        HEADER,
        [
            _row("2026-08-01T09:00:00", "ayu@example.com", "Ayu"),
            _row("2026-08-01T09:05:00", "bagas@example.com", "Bagas"),
        ],
    )
    clean_path = tmp_path / "applicants.clean.csv"
    report_path = tmp_path / "validation_report.csv"
    watermark_path = tmp_path / "last_ingested_row.txt"

    first = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )
    append_outputs(first.result, clean_path, report_path)
    write_watermark(watermark_path, first.watermark_after)

    # A new form response lands.
    worksheet.rows.append(_row("2026-08-02T10:00:00", "citra@example.com", "Citra"))

    second = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )

    assert second.new_row_count == 1
    assert second.watermark_before == 2
    assert second.watermark_after == 3
    assert [a.applicant_id for a in second.result.applicants] == ["A003"]
    assert second.result.applicants[0].email == "citra@example.com"

    append_outputs(second.result, clean_path, report_path)
    write_watermark(watermark_path, second.watermark_after)

    clean_df = pd.read_csv(clean_path, dtype=str, keep_default_na=False)
    assert list(clean_df["applicant_id"]) == ["A001", "A002", "A003"]
    assert list(clean_df["email"]) == ["ayu@example.com", "bagas@example.com", "citra@example.com"]
    assert read_watermark(watermark_path) == 3


def test_no_new_rows_is_a_no_op(tmp_path: Path) -> None:
    settings, grid = _settings_and_grid()
    worksheet = FakeWorksheet(HEADER, [_row("2026-08-01T09:00:00", "ayu@example.com", "Ayu")])
    clean_path = tmp_path / "applicants.clean.csv"
    watermark_path = tmp_path / "last_ingested_row.txt"
    write_watermark(watermark_path, 1)

    incremental = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )

    assert incremental.new_row_count == 0
    assert incremental.result.applicants == []


def test_resubmission_in_a_later_batch_is_rejected_not_guessed(tmp_path: Path) -> None:
    """A returning applicant's earlier row already made it into the clean
    CSV in a prior run; the incremental batch can't re-run FR-05's
    "most recent wins" dedupe against a row it never re-reads, so it must
    reject rather than silently append a second record for the same email."""
    settings, grid = _settings_and_grid()
    worksheet = FakeWorksheet(HEADER, [_row("2026-08-01T09:00:00", "ayu@example.com", "Ayu")])
    clean_path = tmp_path / "applicants.clean.csv"
    report_path = tmp_path / "validation_report.csv"
    watermark_path = tmp_path / "last_ingested_row.txt"

    first = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )
    append_outputs(first.result, clean_path, report_path)
    write_watermark(watermark_path, first.watermark_after)

    # Ayu resubmits — same email, new row further down the sheet.
    worksheet.rows.append(_row("2026-08-05T09:00:00", "ayu@example.com", "Ayu Updated"))

    second = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )

    assert second.new_row_count == 1
    assert second.result.applicants == []
    assert any(
        r.reason_code == "DUPLICATE_OF_EXISTING_APPLICANT" and r.outcome == "REJECTED"
        for r in second.result.report
    )
    # The watermark still advances — this row is never retried.
    assert second.watermark_after == 2


def test_force_reprocesses_everything_and_restores_cross_submission_dedupe(
    tmp_path: Path,
) -> None:
    settings, grid = _settings_and_grid()
    worksheet = FakeWorksheet(
        HEADER,
        [
            _row("2026-08-01T09:00:00", "ayu@example.com", "Ayu"),
            _row("2026-08-01T09:05:00", "bagas@example.com", "Bagas"),
        ],
    )
    clean_path = tmp_path / "applicants.clean.csv"
    report_path = tmp_path / "validation_report.csv"
    watermark_path = tmp_path / "last_ingested_row.txt"

    first = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
    )
    write_outputs(first.result, clean_path, report_path)
    write_watermark(watermark_path, first.watermark_after)

    # Ayu resubmits with a later timestamp — under --force this is a normal
    # FR-05 dedupe collapse, not a rejection.
    worksheet.rows.append(_row("2026-08-05T09:00:00", "ayu@example.com", "Ayu Updated"))

    forced = run_incremental_sheets_ingest(
        source=SheetsApplicantSource(worksheet=worksheet),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
        force=True,
    )

    assert forced.watermark_before == 0
    assert forced.watermark_after == 3
    assert [a.applicant_id for a in forced.result.applicants] == ["A001", "A002"]
    assert {a.full_name for a in forced.result.applicants} == {"Ayu Updated", "Bagas"}
    assert not any(r.reason_code == "DUPLICATE_OF_EXISTING_APPLICANT" for r in forced.result.report)
    assert any(
        r.reason_code == "DUPLICATE_EMAIL" and r.outcome == "COLLAPSED"
        for r in forced.result.report
    )
