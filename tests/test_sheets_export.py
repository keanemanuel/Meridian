"""Tests for export/sheets_writer.py — the committee-facing Google Sheet.

Everything except `export_timetable` is pure, so the layout is asserted
directly. `export_timetable` is exercised against a fake gspread client:
the point is which calls it makes and in what order, not that Google
accepts them.
"""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.export.room_view import RoomView, RoomViewCell, RoomViewRow
from iff_scheduler.export.sheets_writer import (
    COLUMN_COUNT,
    build_tabs,
    export_timetable,
    format_requests,
    layout_tab,
    sanitise_tab_title,
    timezone_label,
)

THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)


def _cell(name: str, *, clash: bool = False) -> RoomViewCell:
    return RoomViewCell(
        applicant_id=f"A-{name}",
        full_name=name,
        sub_division="Logistics",
        division=DivisionCode.LOGISTICS,
        choice_index=1,
        is_clash=clash,
        is_locked=False,
    )


def _row(slot_index: int, cells: dict[str, RoomViewCell | None]) -> RoomViewRow:
    start = time(18, 0 + 20 * slot_index) if slot_index < 3 else time(19, 0)
    end = time(18, 20 + 20 * slot_index) if slot_index < 2 else time(19, 20)
    return RoomViewRow(
        slot_id=f"{THU}_{slot_index}",
        start_time=start,
        end_time=end,
        cells=cells,
    )


def _view(
    room_id: str,
    day: date,
    panel_ids: list[str],
    rows: list[RoomViewRow],
) -> RoomView:
    return RoomView(
        room_id=room_id,
        date=day,
        day_label="Thu" if day == THU else "Fri",
        panel_ids=panel_ids,
        panel_divisions={p: DivisionCode.LOGISTICS for p in panel_ids},
        rows=rows,
    )


# ---- build_tabs ----


def test_one_tab_per_day_and_one_section_per_panel() -> None:
    thu = _view(
        "2016",
        THU,
        ["LOGISTICS-T1", "LOGISTICS-T2"],
        [
            _row(0, {"LOGISTICS-T1": _cell("Ayu"), "LOGISTICS-T2": _cell("Bagas")}),
            _row(1, {"LOGISTICS-T1": None, "LOGISTICS-T2": _cell("Citra")}),
        ],
    )
    fri = _view(
        "2014",
        FRI,
        ["LOGISTICS-F1"],
        [_row(0, {"LOGISTICS-F1": _cell("Dimas")})],
    )

    tabs = build_tabs([fri, thu], "Australia/Melbourne")

    assert [t.title for t in tabs] == ["Thu 17 Sep", "Fri 18 Sep"]
    assert [s.panel_id for s in tabs[0].sections] == ["LOGISTICS-T1", "LOGISTICS-T2"]
    assert tabs[0].sections[0].title == (
        "Thursday, 17 September 2026 · Room 2016 · Panel LOGISTICS-T1"
    )
    # A free slot inside a running panel stays as a blank row.
    assert [r.applicant_name for r in tabs[0].sections[0].rows] == ["Ayu", ""]


def test_a_panel_with_no_interviews_that_day_is_left_out() -> None:
    view = _view(
        "2016",
        THU,
        ["LOGISTICS-T1", "LOGISTICS-T2"],
        [_row(0, {"LOGISTICS-T1": _cell("Ayu"), "LOGISTICS-T2": None})],
    )
    tabs = build_tabs([view], "Australia/Melbourne")
    assert [s.panel_id for s in tabs[0].sections] == ["LOGISTICS-T1"]


def test_a_run_with_nothing_scheduled_produces_no_tabs() -> None:
    view = _view("2016", THU, ["LOGISTICS-T1"], [_row(0, {"LOGISTICS-T1": None})])
    assert build_tabs([view], "Australia/Melbourne") == []


# ---- layout_tab ----


def test_layout_stacks_title_header_and_slot_rows_per_section() -> None:
    view = _view(
        "2016",
        THU,
        ["LOGISTICS-T1", "LOGISTICS-T2"],
        [
            _row(0, {"LOGISTICS-T1": _cell("Ayu"), "LOGISTICS-T2": _cell("Bagas")}),
            _row(1, {"LOGISTICS-T1": _cell("Citra", clash=True), "LOGISTICS-T2": None}),
        ],
    )
    layout = layout_tab(build_tabs([view], "Australia/Melbourne")[0], "Australia/Melbourne")

    assert all(len(row) == COLUMN_COUNT for row in layout.values)
    assert layout.title_rows == [0, 5]
    assert layout.header_rows == [1, 6]
    # Two panels x two slots = four data rows; the spacer row is not one.
    assert layout.data_row_count == 4
    assert layout.values[1][3] == "Applicant Name"
    assert layout.values[2] == ["18:00 - 18:20", "", "", "Ayu", "", ""]
    # Citra clashes, so her row is the one to paint red.
    assert layout.clash_rows == [3]
    assert layout.values[3][3] == "Citra"


def test_time_header_names_the_configured_timezone() -> None:
    view = _view("2016", THU, ["LOGISTICS-T1"], [_row(0, {"LOGISTICS-T1": _cell("Ayu")})])
    tab = build_tabs([view], "Australia/Melbourne")[0]
    assert layout_tab(tab, "Australia/Melbourne").values[1][0] == "Time (AEST)"
    assert layout_tab(tab, "Asia/Jakarta").values[1][0] == "Time (WIB)"


def test_timezone_label_falls_back_to_the_raw_zone_name() -> None:
    assert timezone_label("Not/AZone", THU) == "Not/AZone"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Thu 17 Sep", "Thu 17 Sep"), ("Thu: 17/Sep", "Thu 17 Sep"), ("a" * 200, "a" * 100)],
)
def test_tab_titles_are_legal_for_google_sheets(raw: str, expected: str) -> None:
    assert sanitise_tab_title(raw) == expected


# ---- format_requests ----


def test_every_title_row_is_merged_and_every_clash_row_painted() -> None:
    view = _view(
        "2016",
        THU,
        ["LOGISTICS-T1"],
        [
            _row(0, {"LOGISTICS-T1": _cell("Ayu", clash=True)}),
            _row(1, {"LOGISTICS-T1": _cell("Bagas")}),
        ],
    )
    layout = layout_tab(build_tabs([view], "Asia/Jakarta")[0], "Asia/Jakarta")
    requests = format_requests(7, layout)

    merges = [r for r in requests if "mergeCells" in r]
    assert len(merges) == len(layout.title_rows)
    assert merges[0]["mergeCells"]["range"]["sheetId"] == 7

    painted = [
        r["repeatCell"]["range"]["startRowIndex"]
        for r in requests
        if "repeatCell" in r
        and "backgroundColor" in r["repeatCell"]["fields"]
        and "textFormat" not in r["repeatCell"]["fields"]
    ]
    assert painted == layout.clash_rows
    assert any("autoResizeDimensions" in r for r in requests)


# ---- export_timetable, against a fake gspread client ----


class _FakeWorksheet:
    def __init__(self, title: str, sheet_id: int) -> None:
        self.title = title
        self.id = sheet_id
        self.values: list[list[str]] | None = None

    def update(self, values: list[list[str]], range_name: str) -> None:
        self.values = values
        self.range_name = range_name


class _FakeSpreadsheet:
    def __init__(self, title: str, folder_id: str | None = None) -> None:
        self.title = title
        self.folder_id = folder_id
        self.id = "sheet-123"
        self.url = "https://docs.google.com/spreadsheets/d/sheet-123"
        self._sheets = [_FakeWorksheet("Sheet1", 0)]
        self.batches: list[dict[str, Any]] = []
        self.shares: list[tuple[Any, str, str]] = []
        self.deleted: list[str] = []

    def worksheets(self) -> list[_FakeWorksheet]:
        return list(self._sheets)

    def add_worksheet(self, title: str, rows: int, cols: int) -> _FakeWorksheet:
        ws = _FakeWorksheet(title, len(self._sheets) + 100)
        self._sheets.append(ws)
        return ws

    def del_worksheet(self, worksheet: _FakeWorksheet) -> None:
        self.deleted.append(worksheet.title)
        self._sheets.remove(worksheet)

    def batch_update(self, body: dict[str, Any]) -> None:
        self.batches.append(body)

    def share(self, email_address: Any, perm_type: str, role: str, notify: bool = True) -> None:
        # `notify` mirrors gspread.Spreadsheet.share: an "anyone with the link"
        # grant has no recipient to email, so the writer passes notify=False.
        self.shares.append((email_address, perm_type, role, notify))


class _FakeClient:
    def __init__(self) -> None:
        self.created: list[_FakeSpreadsheet] = []

    def create(self, title: str, folder_id: str | None = None) -> _FakeSpreadsheet:
        sheet = _FakeSpreadsheet(title, folder_id)
        self.created.append(sheet)
        return sheet


def _two_day_tabs() -> list[Any]:
    thu = _view("2016", THU, ["LOGISTICS-T1"], [_row(0, {"LOGISTICS-T1": _cell("Ayu")})])
    fri = _view("2014", FRI, ["LOGISTICS-F1"], [_row(0, {"LOGISTICS-F1": _cell("Bagas")})])
    return build_tabs([thu, fri], "Asia/Jakarta")


def test_export_writes_a_tab_per_day_drops_the_default_and_shares_by_link() -> None:
    client = _FakeClient()
    result = export_timetable(client, "IFF timetable", _two_day_tabs(), "Asia/Jakarta")

    sheet = client.created[0]
    assert sheet.title == "IFF timetable"
    assert [ws.title for ws in sheet.worksheets()] == ["Thu 17 Sep", "Fri 18 Sep"]
    assert sheet.deleted == ["Sheet1"]
    assert sheet.shares == [(None, "anyone", "reader", False)]
    assert result.sheet_url == "https://docs.google.com/spreadsheets/d/sheet-123"
    assert result.tabs == ["Thu 17 Sep", "Fri 18 Sep"]
    assert result.rows_written == 2
    assert result.clashes == 0
    assert len(sheet.batches) == 1


def test_export_can_skip_link_sharing() -> None:
    client = _FakeClient()
    export_timetable(
        client, "IFF timetable", _two_day_tabs(), "Asia/Jakarta", share_with_link=False
    )
    assert client.created[0].shares == []


def test_export_refuses_an_empty_run_rather_than_creating_a_blank_sheet() -> None:
    client = _FakeClient()
    with pytest.raises(ValueError, match="no scheduled interviews"):
        export_timetable(client, "IFF timetable", [], "Asia/Jakarta")
    assert client.created == []


def test_export_defaults_to_the_service_accounts_own_drive_root() -> None:
    """No folder_id given -> gspread's own default (create in Drive root),
    which is the behaviour that fails for a bare service account (this is
    "current behaviour" and is preserved unchanged when nothing is
    configured)."""
    client = _FakeClient()
    export_timetable(client, "IFF timetable", _two_day_tabs(), "Asia/Jakarta")
    assert client.created[0].folder_id is None


def test_export_creates_the_spreadsheet_inside_the_configured_folder() -> None:
    client = _FakeClient()
    export_timetable(
        client,
        "IFF timetable",
        _two_day_tabs(),
        "Asia/Jakarta",
        folder_id="1uB8vmBvYeQIdjhsKY--qfVdSKaDyNKqy",
    )
    assert client.created[0].folder_id == "1uB8vmBvYeQIdjhsKY--qfVdSKaDyNKqy"
