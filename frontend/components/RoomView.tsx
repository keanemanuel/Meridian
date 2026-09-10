"use client";

import { useMemo, useState } from "react";
import {
  assignmentMatches,
  cellIndex,
  cellKey,
  dayAxis,
  formatDayLabel,
  formatTime,
  panelAxis,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { EmptyState, SearchBar } from "./ui";

export type MoveRequest = {
  assignment: Assignment;
  panelId: string;
  slotId: string;
};

/** Timetable grid: rows are slots, columns are panels (FR-30).
 *
 * One day is shown at a time, with the days laid out side by side in a track
 * that slides. Each day only shows the panels that actually run that day,
 * because both axes are derived from the run's own assignments.
 *
 * When `onMove` is supplied, cells can be dragged to an empty slot. Valid
 * targets are highlighted: an empty cell, on a panel of the same division,
 * that is not where the applicant's other interview already sits. That is the
 * set the server's edit validator will accept, so a highlighted drop never
 * bounces back as DIVISION_MISMATCH or DOUBLE_BOOKED_APPLICANT (E-12).
 */
export function RoomView({
  assignments,
  onSelect,
  onMove,
  moving = false,
}: {
  assignments: Assignment[];
  onSelect: (a: Assignment) => void;
  onMove?: (move: MoveRequest) => void | Promise<void>;
  moving?: boolean;
}) {
  const days = useMemo(() => dayAxis(assignments), [assignments]);
  const [rawDay, setRawDay] = useState(0);
  const [query, setQuery] = useState("");
  const [dragging, setDragging] = useState<Assignment | null>(null);

  const cells = useMemo(() => cellIndex(assignments), [assignments]);
  /** Which division each panel interviews for, as this run used it. */
  const panelDivision = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of assignments) map.set(a.panel_id, a.division);
    return map;
  }, [assignments]);
  /** The other interview of whoever is being dragged, so its slot is excluded. */
  const partnerSlot = useMemo(() => {
    if (!dragging) return null;
    return (
      assignments.find(
        (a) =>
          a.applicant_id === dragging.applicant_id &&
          a.assignment_id !== dragging.assignment_id,
      )?.slot_id ?? null
    );
  }, [assignments, dragging]);

  if (days.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const dayIndex = Math.min(rawDay, days.length - 1);

  const isValidTarget = (panelId: string, slotId: string) => {
    if (!dragging || !onMove) return false;
    if ((cells.get(cellKey(panelId, slotId)) ?? []).length > 0) return false;
    if (panelDivision.get(panelId) !== dragging.division) return false;
    if (slotId === partnerSlot) return false;
    return true;
  };

  return (
    <div>
      <SearchBar value={query} onChange={setQuery} />
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
          {formatDayLabel(days[dayIndex])}
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
        {dragging && (
          <span className="ml-auto text-xs font-medium text-blue-600">
            Drop {dragging.full_name} on any highlighted slot.
          </span>
        )}
      </div>

      <div className="overflow-hidden rounded-lg border border-neutral-200 bg-white">
        <div
          className="flex transition-transform duration-300 ease-out"
          style={{ transform: `translateX(-${dayIndex * 100}%)` }}
        >
          {days.map((date) => (
            <div key={date} className="w-full shrink-0">
              <DayGrid
                date={date}
                assignments={assignments}
                cells={cells}
                query={query}
                onSelect={onSelect}
                onMove={onMove}
                moving={moving}
                dragging={dragging}
                setDragging={setDragging}
                isValidTarget={isValidTarget}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function DayGrid({
  date,
  assignments,
  cells,
  query,
  onSelect,
  onMove,
  moving,
  dragging,
  setDragging,
  isValidTarget,
}: {
  date: string;
  assignments: Assignment[];
  cells: Map<string, Assignment[]>;
  query: string;
  onSelect: (a: Assignment) => void;
  onMove?: (move: MoveRequest) => void | Promise<void>;
  moving: boolean;
  dragging: Assignment | null;
  setDragging: (a: Assignment | null) => void;
  isValidTarget: (panelId: string, slotId: string) => boolean;
}) {
  const onDay = useMemo(
    () => assignments.filter((a) => a.date === date),
    [assignments, date],
  );
  const slots = useMemo(() => slotAxis(onDay), [onDay]);
  const panels = useMemo(() => panelAxis(onDay), [onDay]);

  if (slots.length === 0) {
    return <EmptyState title="Nothing is scheduled on this day." />;
  }

  return (
    <div className="overflow-auto">
      <table className="min-w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="sticky left-0 top-0 z-20 w-32 border-b border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-500">
              Slot
            </th>
            {panels.map((p) => (
              <th
                key={p.panel_id}
                className="sticky top-0 z-10 min-w-[10rem] border-b border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-700"
              >
                <span className="block">{p.panel_id}</span>
                <span className="block font-normal text-neutral-400">
                  Room {p.room}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {slots.map((slot) => (
            <tr key={slot.slot_id}>
              <th
                scope="row"
                className="sticky left-0 z-10 border-b border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left align-top font-normal"
              >
                <span className="block font-medium text-neutral-700">
                  {formatTime(slot.start_time)}–{formatTime(slot.end_time)}
                </span>
              </th>

              {panels.map((panel) => {
                const here =
                  cells.get(cellKey(panel.panel_id, slot.slot_id)) ?? [];
                const droppable = isValidTarget(panel.panel_id, slot.slot_id);
                return (
                  <td
                    key={panel.panel_id}
                    onDragOver={
                      droppable
                        ? (e) => {
                            e.preventDefault();
                            e.dataTransfer.dropEffect = "move";
                          }
                        : undefined
                    }
                    onDrop={
                      droppable
                        ? (e) => {
                            e.preventDefault();
                            const held = dragging;
                            setDragging(null);
                            if (held && onMove) {
                              void onMove({
                                assignment: held,
                                panelId: panel.panel_id,
                                slotId: slot.slot_id,
                              });
                            }
                          }
                        : undefined
                    }
                    className={`border-b border-r border-neutral-100 p-0 align-top transition-colors ${
                      droppable
                        ? "bg-blue-50 ring-1 ring-inset ring-blue-400"
                        : ""
                    }`}
                  >
                    {here.length === 0 ? (
                      <div className="h-full min-h-[3rem] px-3 py-2 text-neutral-300">
                        {droppable ? (
                          <span className="text-blue-500">Drop here</span>
                        ) : (
                          "·"
                        )}
                      </div>
                    ) : (
                      here.map((a) => (
                        <button
                          key={a.assignment_id}
                          type="button"
                          draggable={Boolean(onMove) && !moving}
                          onDragStart={(e) => {
                            e.dataTransfer.effectAllowed = "move";
                            e.dataTransfer.setData(
                              "text/plain",
                              a.assignment_id,
                            );
                            setDragging(a);
                          }}
                          onDragEnd={() => setDragging(null)}
                          onClick={() => onSelect(a)}
                          title={buildTitle(a)}
                          className={`relative block h-full min-h-[3rem] w-full px-3 py-2 text-left transition-colors ${
                            onMove ? "cursor-grab active:cursor-grabbing" : ""
                          } ${
                            dragging?.assignment_id === a.assignment_id
                              ? "opacity-40"
                              : ""
                          } ${
                            a.is_clash
                              ? "bg-red-100 text-red-600 hover:bg-red-200"
                              : "text-neutral-800 hover:bg-neutral-100"
                          } ${
                            assignmentMatches(a, query)
                              ? "ring-2 ring-inset ring-amber-500"
                              : ""
                          }`}
                        >
                          <span className="block truncate pr-4 font-medium">
                            {a.full_name}
                          </span>
                          <span
                            className={`block truncate ${a.is_clash ? "text-red-500" : "text-neutral-500"}`}
                          >
                            {a.sub_division}
                          </span>
                          {a.is_locked && (
                            <span
                              className="absolute right-1 top-1 text-[10px] leading-none"
                              title="Locked. The solver will not move this."
                            >
                              🔒
                            </span>
                          )}
                        </button>
                      ))
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function buildTitle(a: Assignment): string {
  const notes = [
    a.full_name,
    a.sub_division,
    a.is_clash ? "clash" : null,
    a.is_locked ? "locked" : null,
  ].filter(Boolean);
  return notes.join(" · ");
}
