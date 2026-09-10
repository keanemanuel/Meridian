"""Human-readable summary of an applicant's declared interview availability.

Pure: turns the stored `availability_slots` (grid slot ids) back into the
day-and-time preference the applicant actually ticked on the form (FR-51,
SPEC.md §9.2). The live IFF form collects whole days, so a fully-available day
collapses to just its label; an augmented export that supplies explicit time
blocks shows the merged ranges instead.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date as Date
from datetime import time as Time

from iff_scheduler.domain.models import Slot


def _hhmm(t: Time) -> str:
    return t.strftime("%H:%M")


def _slot_date(slot_id: str) -> Date | None:
    """The calendar date encoded in a grid slot id ("2026-09-17_1800" -> 17 Sep).

    Slot ids are "<date>_<HHMM>" (`domain/grid.build_slot_grid`). Only the HHMM
    suffix moves when the event window is re-timed after ingest; the date prefix
    is stable, so it still identifies the day the applicant ticked even once the
    exact slot id has drifted off the grid.
    """
    prefix = slot_id.split("_", 1)[0]
    try:
        return Date.fromisoformat(prefix)
    except ValueError:
        return None


def _day_label(label: str, d: Date) -> str:
    # Built by hand: strftime's no-pad "%-d" is a platform extension.
    return f"{label} {d.day} {d.strftime('%b')}"


def _merge_ranges(slots: list[Slot]) -> list[tuple[Time, Time]]:
    """Collapse contiguous slots (one's end == the next's start) into windows."""
    windows: list[tuple[Time, Time]] = []
    for slot in sorted(slots, key=lambda s: s.start_time):
        if windows and windows[-1][1] == slot.start_time:
            windows[-1] = (windows[-1][0], slot.end_time)
        else:
            windows.append((slot.start_time, slot.end_time))
    return windows


def summarise_availability(slot_ids: Iterable[str], grid_slots: Sequence[Slot]) -> str:
    """Render declared availability, e.g. "Thu 17 Sep; Fri 18 Sep (17:00–19:00)".

    A day the applicant is fully available for shows as just its label; a
    partial day appends its merged time ranges. Returns "" when nothing was
    declared — the caller decides how to show an empty preference (CLAUDE.md
    invariant 3: no guessing).

    Slot ids whose exact value is not on the current grid but whose date *is* a
    grid day are recovered to a whole-day preference: the live IFF form collects
    whole days, and re-timing the event window after ingest (e.g. moving the
    Thursday start from 18:00 to 18:30) shifts every stored slot id's HHMM
    suffix so none match by value, which would otherwise blank the column for
    every applicant. The date the applicant ticked is not a guess; only the
    intra-day granularity is lost, so such a day renders as its bare label.
    Slot ids for a date that is not on the grid at all are ignored.
    """
    by_id = {s.slot_id: s for s in grid_slots}
    ids = list(slot_ids)
    chosen = [by_id[sid] for sid in ids if sid in by_id]

    all_by_date: dict[Date, list[Slot]] = defaultdict(list)
    for slot in grid_slots:
        all_by_date[slot.date].append(slot)
    chosen_by_date: dict[Date, list[Slot]] = defaultdict(list)
    for slot in chosen:
        chosen_by_date[slot.date].append(slot)

    declared_dates: set[Date] = set(chosen_by_date)
    for sid in ids:
        d = _slot_date(sid)
        if d is not None and d in all_by_date:
            declared_dates.add(d)
    if not declared_dates:
        return ""

    parts: list[str] = []
    for d in sorted(declared_dates):
        picked = chosen_by_date.get(d, [])
        label = _day_label(all_by_date[d][0].day_label, d)
        if not picked or len(picked) == len(all_by_date[d]):
            parts.append(label)
        else:
            ranges = ", ".join(f"{_hhmm(a)}–{_hhmm(b)}" for a, b in _merge_ranges(picked))
            parts.append(f"{label} ({ranges})")
    return "; ".join(parts)
