"use client";

import { useMemo, useState } from "react";
import {
  dayAxis,
  formatDayLabel,
  formatTime,
  roomSummaries,
  type RoomSummary,
  type SlotAxis,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { EmptyState } from "./ui";

/** One card per room, for a single day at a time (FR-32).
 *
 * The day selector mirrors RoomView's: prev/next arrows around a date label,
 * and only the days this run actually uses appear. For the chosen day every
 * room active that day gets a card carrying its run-wide summary (interview
 * count, panels, divisions represented) and, below it, that room's own
 * slot-by-slot schedule — the same time -> applicant -> division rows
 * RoomView shows, but scoped to the one room instead of spread across every
 * panel side by side.
 *
 * The "divisions represented" chips double as a filter: exactly one is active
 * at a time and the schedule below shows only that division's interviews, so a
 * room card reads as one division's running order rather than every division
 * interleaved. The selection is per-card — each room keeps its own. When the
 * active division runs on more than one panel in the same room, the schedule
 * gains a column per panel so the two running orders never collide in a cell.
 *
 * Clicking an interview calls `onSelect`, the same hook RoomView uses to open
 * the move / detail dialog.
 */
export function RoomsView({
  assignments,
  onSelect,
}: {
  assignments: Assignment[];
  onSelect: (a: Assignment) => void;
}) {
  const days = useMemo(() => dayAxis(assignments), [assignments]);
  const [rawDay, setRawDay] = useState(0);

  /** Run-wide summary per room, looked up by the selected day's cards. */
  const summaryByRoom = useMemo(() => {
    const map = new Map<string, RoomSummary>();
    for (const s of roomSummaries(assignments)) map.set(s.room, s);
    return map;
  }, [assignments]);

  if (days.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const dayIndex = Math.min(rawDay, days.length - 1);
  const date = days[dayIndex];
  const onDay = assignments.filter((a) => a.date === date);
  const rooms = [...new Set(onDay.map((a) => a.room))].sort((x, y) =>
    x.localeCompare(y, undefined, { numeric: true }),
  );

  /** The full slot axis for the chosen day, spanning every room. Each room
   * card renders one row per entry here regardless of which division is
   * selected, so rows stay aligned across cards and never "squish" when a
   * division fills only part of the day. */
  const daySlots = slotAxis(onDay);

  return (
    <div>
      <div className="mb-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => setRawDay(dayIndex - 1)}
          disabled={dayIndex === 0}
          aria-label="Previous day"
          className="rounded-md border border-neutral-300 bg-white px-2.5 py-1 text-sm text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:opacity-35"
        >
          ‹
        </button>
        <span className="min-w-[10rem] text-center text-sm font-semibold text-neutral-900">
          {formatDayLabel(date)}
        </span>
        <button
          type="button"
          onClick={() => setRawDay(dayIndex + 1)}
          disabled={dayIndex >= days.length - 1}
          aria-label="Next day"
          className="rounded-md border border-neutral-300 bg-white px-2.5 py-1 text-sm text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:opacity-35"
        >
          ›
        </button>
        <span className="text-xs text-neutral-500">
          Day {dayIndex + 1} of {days.length}
        </span>
      </div>

      {rooms.length === 0 ? (
        <EmptyState title="Nothing is scheduled on this day." />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {rooms.map((room) => (
            <RoomCard
              key={room}
              room={room}
              summary={summaryByRoom.get(room)}
              assignments={onDay.filter((a) => a.room === room)}
              slots={daySlots}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** A single room's card: the run-wide summary header, a one-of division
 * selector, then this day's schedule for the selected division as a vertical
 * time -> applicant list. If that division runs on two panels in this one
 * room, the schedule splits into a column per panel (labelled with the panel
 * id) so their running orders sit side by side instead of piling into one
 * cell. */
function RoomCard({
  room,
  summary,
  assignments,
  slots,
  onSelect,
}: {
  room: string;
  summary: RoomSummary | undefined;
  assignments: Assignment[];
  /** The whole day's slot axis, shared by every card. Rows are drawn for all
   * of these, not just the slots the active division happens to fill. */
  slots: SlotAxis[];
  onSelect: (a: Assignment) => void;
}) {
  const panels = summary?.panels ?? [
    ...new Set(assignments.map((a) => a.panel_id)),
  ];
  const interviewCount = summary?.interviewCount ?? assignments.length;
  const clashes = summary?.clashes ?? 0;

  /** Chips come from the run-wide summary (most interviews first) so they stay
   * stable as the day changes; fall back to this day's own divisions if there
   * is no summary. */
  const divisions = useMemo(() => {
    if (summary?.divisions) return summary.divisions;
    const counts = new Map<string, number>();
    for (const a of assignments) {
      counts.set(a.division, (counts.get(a.division) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([division, count]) => ({ division, count }))
      .sort((x, y) => y.count - x.count || x.division.localeCompare(y.division));
  }, [summary, assignments]);

  // Default to the first (largest) division; `?? divisions[0]` keeps a sensible
  // active chip even before the user has clicked or if data arrives late.
  const [picked, setPicked] = useState<string | null>(null);
  const activeDivision = picked ?? divisions[0]?.division ?? null;

  const shown = useMemo(
    () =>
      activeDivision
        ? assignments.filter((a) => a.division === activeDivision)
        : assignments,
    [assignments, activeDivision],
  );

  const bySlot = useMemo(() => {
    const map = new Map<string, Assignment[]>();
    for (const a of shown) {
      const bucket = map.get(a.slot_id);
      if (bucket) bucket.push(a);
      else map.set(a.slot_id, [a]);
    }
    return map;
  }, [shown]);

  /** The panels the active division runs in this room. A division can be split
   * across two panels sharing one room (e.g. MEDMARDOC-BAL-1 and
   * MEDMARDOC-BAL-2 both in Room 2018); when that happens each gets its own
   * column so their running orders never overlap in a single cell. */
  const divisionPanels = useMemo(
    () =>
      [...new Set(shown.map((a) => a.panel_id))].sort((x, y) =>
        x.localeCompare(y, undefined, { numeric: true }),
      ),
    [shown],
  );
  const multiPanel = divisionPanels.length > 1;

  return (
    <section className="overflow-hidden rounded-lg border border-neutral-200 bg-white">
      <header className="flex items-baseline justify-between border-b border-neutral-200 px-4 py-2.5">
        <div>
          <h3 className="text-sm font-semibold text-neutral-900">Room {room}</h3>
          <p className="text-xs text-neutral-400">
            {panels.length} panel{panels.length === 1 ? "" : "s"}:{" "}
            {panels.join(", ")}
          </p>
        </div>
        <p className="shrink-0 text-xs text-neutral-500">
          {interviewCount} interview{interviewCount === 1 ? "" : "s"}
          {clashes > 0 && (
            <span className="ml-1 text-red-600">· {clashes} clash</span>
          )}
        </p>
      </header>

      {divisions.length > 0 && (
        <div className="border-b border-neutral-100 px-4 py-3">
          <p className="mb-2 text-xs uppercase tracking-wide text-neutral-400">
            Divisions represented ({divisions.length}) — pick one
          </p>
          <ul className="flex flex-wrap gap-1.5" role="tablist">
            {divisions.map((d) => {
              const active = d.division === activeDivision;
              return (
                <li key={d.division}>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setPicked(d.division)}
                    className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium transition-colors ${
                      active
                        ? "border-neutral-800 bg-neutral-800 text-white"
                        : "border-neutral-200 bg-neutral-100 text-neutral-600 hover:bg-neutral-200"
                    }`}
                  >
                    {d.division}
                    <span
                      className={`ml-1 tabular-nums ${
                        active ? "text-neutral-300" : "text-neutral-400"
                      }`}
                    >
                      {d.count}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {slots.length === 0 ? (
        <p className="px-4 py-3 text-xs text-neutral-400">
          Nothing scheduled in this room today.
        </p>
      ) : multiPanel ? (
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-neutral-200">
                <th className="w-24 border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left font-medium uppercase tracking-wide text-neutral-400">
                  Slot
                </th>
                {divisionPanels.map((panelId) => (
                  <th
                    key={panelId}
                    className="min-w-[9rem] border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-700 last:border-r-0"
                  >
                    {panelId}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {slots.map((slot) => {
                const here = bySlot.get(slot.slot_id) ?? [];
                return (
                  <tr
                    key={slot.slot_id}
                    className="border-b border-neutral-100 last:border-0"
                  >
                    <SlotTimeHeader slot={slot} />
                    {divisionPanels.map((panelId) => (
                      <td
                        key={panelId}
                        className="border-r border-neutral-100 p-0 align-top last:border-r-0"
                      >
                        <ScheduleCell
                          items={here.filter((a) => a.panel_id === panelId)}
                          onSelect={onSelect}
                        />
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <table className="min-w-full border-collapse text-xs">
          <tbody>
            {slots.map((slot) => {
              const here = bySlot.get(slot.slot_id) ?? [];
              return (
                <tr
                  key={slot.slot_id}
                  className="border-b border-neutral-100 last:border-0"
                >
                  <SlotTimeHeader slot={slot} />
                  <td className="p-0 align-top">
                    <ScheduleCell items={here} onSelect={onSelect} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** The two-line "start over end" row header, identical in the single- and
 * multi-panel layouts so every time cell is the same height. */
function SlotTimeHeader({ slot }: { slot: SlotAxis }) {
  return (
    <th
      scope="row"
      className="w-24 border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left align-top font-normal"
    >
      <span className="block font-medium tabular-nums text-neutral-700">
        {formatTime(slot.start_time)}
      </span>
      <span className="block tabular-nums text-neutral-400">
        {formatTime(slot.end_time)}
      </span>
    </th>
  );
}

/** One slot's worth of interviews for a single column: the blank marker when
 * empty, otherwise one clickable row per applicant. Shared by both layouts. */
function ScheduleCell({
  items,
  onSelect,
}: {
  items: Assignment[];
  onSelect: (a: Assignment) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="min-h-[2.75rem] px-3 py-2 text-neutral-300">·</div>
    );
  }
  return (
    <>
      {items.map((a) => (
        <button
          key={a.assignment_id}
          type="button"
          onClick={() => onSelect(a)}
          title={buildTitle(a)}
          className={`block min-h-[2.75rem] w-full px-3 py-2 text-left transition-colors ${
            a.is_clash
              ? "bg-red-100 text-red-600 hover:bg-red-200"
              : "text-neutral-800 hover:bg-neutral-100"
          }`}
        >
          <span className="block truncate pr-4 font-medium">{a.full_name}</span>
          <span
            className={`block truncate ${a.is_clash ? "text-red-500" : "text-neutral-500"}`}
          >
            {a.sub_division}
          </span>
        </button>
      ))}
    </>
  );
}

function buildTitle(a: Assignment): string {
  return [
    a.full_name,
    a.sub_division,
    a.is_clash ? "clash" : null,
    a.is_locked ? "locked" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
