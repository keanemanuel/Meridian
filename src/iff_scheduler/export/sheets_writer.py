"""Google Sheets timetable export (SPEC.md §9 room view, FR-50).

The committee runs the two evenings off a printed/shared Sheet, not off the
XLSX in `runs/`, so this builds that Sheet: one tab per event day, one
section per interview panel, one row per 20-minute slot, with tick columns
for the door.

Split in two on purpose. `build_tabs` / `tab_values` / `format_requests` are
pure functions over the same `RoomView` objects `xlsx_writer` consumes, so
the layout is testable with no credentials and no network. `export_timetable`
is the only part that talks to gspread, and it takes an already-authorised
client so a test can hand it a fake (CLAUDE.md, "Architecture rule").

One deliberate departure from a naive "one section per room": a room runs up
to three panels at once (`rooms.yaml: max_concurrent_panels`), and the fixed
column set has room for exactly one applicant per row. So a section is one
panel, and its header names the room it sits in. A per-room section would
have to either drop concurrent interviews or grow columns per room.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from iff_scheduler.export.room_view import RoomView

# Read/write on both: the export creates a new spreadsheet (Drive) and writes
# and formats it (Sheets). `ingest/sheets_source.py` deliberately keeps the
# narrower read-only pair.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

COLUMNS = [
    "Time",
    "Interviewer 1",
    "Interviewer 2",
    "Applicant Name",
    "Arrived?",
    "Interview?",
]

COLUMN_COUNT = len(COLUMNS)

# Google Sheets tab titles cannot contain these, and are capped at 100 chars.
_ILLEGAL_TITLE_CHARS = ":\\/?*[]"


@dataclass(frozen=True)
class SheetRow:
    """One 20-minute slot line. `applicant_name` is blank for a free slot;
    the interviewer columns are always blank for the panel to fill in by
    hand, since no interviewer names exist anywhere in the data model."""

    time_label: str
    applicant_name: str
    is_clash: bool


@dataclass(frozen=True)
class SheetSection:
    """One panel's timetable for one day, under a merged title row."""

    title: str
    room_id: str
    panel_id: str
    rows: list[SheetRow]


@dataclass(frozen=True)
class SheetTab:
    """One event day."""

    title: str
    date: Date
    sections: list[SheetSection]

    @property
    def clash_count(self) -> int:
        return sum(1 for s in self.sections for r in s.rows if r.is_clash)


@dataclass(frozen=True)
class ExportedSheet:
    sheet_id: str
    sheet_url: str
    tabs: list[str]
    rows_written: int
    clashes: int


class GspreadClient(Protocol):
    """The single gspread.Client method this writer needs."""

    def create(self, title: str, folder_id: str | None = None) -> Any: ...


def timezone_label(timezone: str, on: Date) -> str:
    """The abbreviation the committee actually writes on the timetable
    ("AEST", "WIB"), derived from `event.timezone` rather than hard-coded
    (CLAUDE.md invariant 5). Falls back to the raw zone name if the platform
    has no abbreviation for it."""
    try:
        stamp = datetime.combine(on, Time(12, 0), tzinfo=ZoneInfo(timezone))
    except Exception:
        return timezone
    return stamp.strftime("%Z") or timezone


def _hhmm(t: Time) -> str:
    return t.strftime("%H:%M")


# Built by hand rather than with strftime's "%-d": the no-pad flag is a
# platform extension and is not available everywhere Python runs.
def _long_date(d: Date) -> str:
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%B')} {d.year}"


def _short_date(d: Date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def sanitise_tab_title(title: str) -> str:
    cleaned = "".join(" " if c in _ILLEGAL_TITLE_CHARS else c for c in title)
    return " ".join(cleaned.split())[:100]


def build_tabs(views: Sequence[RoomView], timezone: str) -> list[SheetTab]:
    """Turn the published room views into one tab per day.

    A panel that has no interview at all on a day is left out entirely: an
    all-blank section is noise on a printed timetable. Free slots inside a
    panel that *is* running that day are kept as blank rows, because the
    committee uses them to slot in a latecomer.
    """
    by_date: dict[Date, list[SheetSection]] = {}
    labels: dict[Date, str] = {}

    for view in sorted(views, key=lambda v: (v.date, v.room_id)):
        labels.setdefault(view.date, view.day_label)
        for panel_id in view.panel_ids:
            if all(row.cells.get(panel_id) is None for row in view.rows):
                continue
            rows = [
                SheetRow(
                    time_label=f"{_hhmm(row.start_time)} - {_hhmm(row.end_time)}",
                    applicant_name=(cell.full_name if (cell := row.cells.get(panel_id)) else ""),
                    is_clash=bool(cell and cell.is_clash),
                )
                for row in view.rows
            ]
            by_date.setdefault(view.date, []).append(
                SheetSection(
                    title=(f"{_long_date(view.date)} · Room {view.room_id} · Panel {panel_id}"),
                    room_id=view.room_id,
                    panel_id=panel_id,
                    rows=rows,
                )
            )

    return [
        SheetTab(
            title=sanitise_tab_title(f"{labels[date]} {_short_date(date)}"),
            date=date,
            sections=sorted(by_date[date], key=lambda s: (s.room_id, s.panel_id)),
        )
        for date in sorted(by_date)
    ]


@dataclass(frozen=True)
class TabLayout:
    """A tab flattened to cell values plus the 0-based row indexes that need
    formatting. Separated from the gspread call so the layout can be asserted
    on directly in tests."""

    values: list[list[str]]
    title_rows: list[int]
    header_rows: list[int]
    clash_rows: list[int]
    # Slot rows only: titles, column headers and the spacer between sections
    # are chrome, and reporting them as "rows written" would overstate the
    # size of the timetable.
    data_row_count: int

    @property
    def row_count(self) -> int:
        return len(self.values)


def layout_tab(tab: SheetTab, timezone: str) -> TabLayout:
    """Flatten one day into the exact cell grid to upload.

    Each section is a merged title row, a bold header row, then one row per
    slot, with a blank spacer row between sections.
    """
    time_header = f"Time ({timezone_label(timezone, tab.date)})"
    headers = [time_header, *COLUMNS[1:]]

    values: list[list[str]] = []
    title_rows: list[int] = []
    header_rows: list[int] = []
    clash_rows: list[int] = []
    data_rows = 0

    for i, section in enumerate(tab.sections):
        if i > 0:
            values.append([""] * COLUMN_COUNT)
        title_rows.append(len(values))
        values.append([section.title, *[""] * (COLUMN_COUNT - 1)])
        header_rows.append(len(values))
        values.append(list(headers))
        for row in section.rows:
            if row.is_clash:
                clash_rows.append(len(values))
            values.append([row.time_label, "", "", row.applicant_name, "", ""])
            data_rows += 1

    return TabLayout(
        values=values,
        title_rows=title_rows,
        header_rows=header_rows,
        clash_rows=clash_rows,
        data_row_count=data_rows,
    )


_GREY = {"red": 0.91, "green": 0.91, "blue": 0.91}
_RED = {"red": 0.98, "green": 0.80, "blue": 0.80}


def _row_range(sheet_id: int, row: int) -> dict[str, Any]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": row,
        "endRowIndex": row + 1,
        "startColumnIndex": 0,
        "endColumnIndex": COLUMN_COUNT,
    }


def format_requests(sheet_id: int, layout: TabLayout) -> list[dict[str, Any]]:
    """The batch_update requests that make one tab readable: merged and bold
    section titles, bold column headers, red clash rows, auto-sized columns,
    and a frozen nothing (each section carries its own header, so freezing
    the top row would pin only the first section's)."""
    requests: list[dict[str, Any]] = []

    for row in layout.title_rows:
        requests.append(
            {"mergeCells": {"range": _row_range(sheet_id, row), "mergeType": "MERGE_ROW"}}
        )
        requests.append(
            {
                "repeatCell": {
                    "range": _row_range(sheet_id, row),
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _GREY,
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": ("userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"),
                }
            }
        )

    for row in layout.header_rows:
        requests.append(
            {
                "repeatCell": {
                    "range": _row_range(sheet_id, row),
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat",
                }
            }
        )

    for row in layout.clash_rows:
        requests.append(
            {
                "repeatCell": {
                    "range": _row_range(sheet_id, row),
                    "cell": {"userEnteredFormat": {"backgroundColor": _RED}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )

    requests.append(
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": COLUMN_COUNT,
                }
            }
        }
    )
    return requests


def export_timetable(
    client: GspreadClient,
    title: str,
    tabs: Sequence[SheetTab],
    timezone: str,
    *,
    share_with_link: bool = True,
    folder_id: str | None = None,
) -> ExportedSheet:
    """Create a new spreadsheet holding `tabs` and return where it landed.

    The Sheet is shared as "anyone with the link can view" so a committee
    member can open it without a Google account and without being added
    individually. It is never shared as editable: the scheduler is the system
    of record, and an edit made in the Sheet would not come back.

    `folder_id`, when given, creates the spreadsheet as a child of that Drive
    folder instead of the service account's own Drive root. A plain service
    account has no personal Drive storage of its own (this is a Google
    platform limit, not a quota that fills up), so every export 403s with
    storageQuotaExceeded until it is pointed at storage that isn't the service
    account's: a Shared Drive folder (`config/export`, `GOOGLE_DRIVE_FOLDER_ID`
    in `.env.example`; see docs/DEPLOY.md for why it has to be a Shared Drive
    and not an ordinary "My Drive" folder shared as Editor).
    """
    if not tabs:
        raise ValueError("Nothing to export: this run has no scheduled interviews.")

    spreadsheet = client.create(title, folder_id=folder_id)
    existing = list(spreadsheet.worksheets())

    rows_written = 0
    requests: list[dict[str, Any]] = []
    for tab in tabs:
        layout = layout_tab(tab, timezone)
        worksheet = spreadsheet.add_worksheet(
            title=tab.title, rows=max(layout.row_count + 10, 20), cols=COLUMN_COUNT
        )
        worksheet.update(layout.values, "A1")
        requests.extend(format_requests(worksheet.id, layout))
        rows_written += layout.data_row_count

    # Drop the empty default worksheet only after the real ones exist — a
    # spreadsheet must always have at least one sheet.
    for worksheet in existing:
        spreadsheet.del_worksheet(worksheet)

    if requests:
        spreadsheet.batch_update({"requests": requests})

    if share_with_link:
        # notify=False: there is no recipient to email for an "anyone" grant.
        spreadsheet.share(None, perm_type="anyone", role="reader", notify=False)

    return ExportedSheet(
        sheet_id=spreadsheet.id,
        sheet_url=spreadsheet.url,
        tabs=[tab.title for tab in tabs],
        rows_written=rows_written,
        clashes=sum(tab.clash_count for tab in tabs),
    )


def open_export_client(service_account_file: str) -> GspreadClient:
    """Authorise gspread for read/write, mirroring `ingest.sheets_source`'s
    credential pattern. Imported lazily by callers that may not have
    credentials configured."""
    import gspread
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        service_account_file, scopes=SCOPES
    )
    client: GspreadClient = gspread.authorize(credentials)
    return client
