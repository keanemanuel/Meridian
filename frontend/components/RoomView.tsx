"use client";

import { type RefObject, useCallback, useMemo, useRef, useState } from "react";
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

/** How a blank cell relates to the interview being dragged:
 *  - "ok"    — a clean target the server will accept without complaint.
 *  - "clash" — a legal target, but outside the applicant's stated availability,
 *              so the move lands flagged red (FR-34). Still allowed.
 *  - null    — not a target: occupied, a different division's panel, or the
 *              slot the applicant's other interview already sits in (C3). */
type TargetKind = "ok" | "clash" | null;

/** Timetable grid: rows are slots, columns are panels (FR-30).
 *
 * One day is shown at a time, with the days laid out side by side in a track
 * that slides. Each day only shows the panels that actually run that day,
 * because both axes are derived from the run's own assignments.
 *
 * When `onMove` is supplied an interview can be dragged onto any blank slot on
 * a panel of its own division (FR-40). A clean target is highlighted blue; a
 * target outside the applicant's declared availability is highlighted amber and
 * still accepted, landing as a clash the recruiter chose (FR-34). The one blank
 * cell that is *not* a target is the slot holding the applicant's other
 * interview — moving there would double-book them (C3).
 */
export function RoomView({
  assignments,
  onSelect,
  onMove,
  onToggleLock,
  moving = false,
}: {
  assignments: Assignment[];
  onSelect: (a: Assignment) => void;
  onMove?: (move: MoveRequest) => void | Promise<void>;
  onToggleLock?: (a: Assignment) => void | Promise<void>;
  moving?: boolean;
}) {
  const days = useMemo(() => dayAxis(assignments), [assignments]);
  const [rawDay, setRawDay] = useState(0);
  const [query, setQuery] = useState("");

  // `dragging` drives the highlight re-render; `draggingRef` is what the drop
  // handlers read, so a drop is judged against the live drag even if a render
  // has not yet committed (HTML5 DnD fires faster than React reconciles).
  const [dragging, setDragging] = useState<Assignment | null>(null);
  const draggingRef = useRef<Assignment | null>(null);
  const beginDrag = useCallback((a: Assignment) => {
    draggingRef.current = a;
    setDragging(a);
  }, []);
  const endDrag = useCallback(() => {
    draggingRef.current = null;
    setDragging(null);
  }, []);

  const cells = useMemo(() => cellIndex(assignments), [assignments]);
  /** Which division each panel interviews for, as this run used it. */
  const panelDivision = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of assignments) map.set(a.panel_id, a.division);
    return map;
  }, [assignments]);

  /** Classify a blank cell for a given dragged interview. Pure in its inputs —
   * no dependence on the `dragging` state — so drop handlers can call it with
   * `draggingRef.current` and get the same answer the highlight showed. */
  const classifyTarget = useCallback(
    (drag: Assignment, panelId: string, slotId: string): TargetKind => {
      if (!onMove) return null;
      if ((cells.get(cellKey(panelId, slotId)) ?? []).length > 0) return null;
      if (panelDivision.get(panelId) !== drag.division) return null;
      const other = assignments.find(
        (a) =>
          a.applicant_id === drag.applicant_id &&
          a.assignment_id !== drag.assignment_id,
      );
      if (other?.slot_id === slotId) return null; // would double-book (C3)
      const avail = drag.availability_slots ?? [];
      return avail.length > 0 && !avail.includes(slotId) ? "clash" : "ok";
    },
    [assignments, cells, panelDivision, onMove],
  );

  if (days.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const dayIndex = Math.min(rawDay, days.length - 1);

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
            Drop {dragging.full_name} on a blue slot, or an amber one to move
            them outside their stated availability.
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
                onToggleLock={onToggleLock}
                moving={moving}
                dragging={dragging}
                draggingRef={draggingRef}
                beginDrag={beginDrag}
                endDrag={endDrag}
                classifyTarget={classifyTarget}
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
  onToggleLock,
  moving,
  dragging,
  draggingRef,
  beginDrag,
  endDrag,
  classifyTarget,
}: {
  date: string;
  assignments: Assignment[];
  cells: Map<string, Assignment[]>;
  query: string;
  onSelect: (a: Assignment) => void;
  onMove?: (move: MoveRequest) => void | Promise<void>;
  onToggleLock?: (a: Assignment) => void | Promise<void>;
  moving: boolean;
  dragging: Assignment | null;
  draggingRef: RefObject<Assignment | null>;
  beginDrag: (a: Assignment) => void;
  endDrag: () => void;
  classifyTarget: (drag: Assignment, panelId: string, slotId: string) => TargetKind;
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
                // Highlight follows the committed drag state; the drop handlers
                // below re-check against draggingRef so they never act on a
                // stale classification.
                const kind: TargetKind = dragging
                  ? classifyTarget(dragging, panel.panel_id, slot.slot_id)
                  : null;
                return (
                  <td
                    key={panel.panel_id}
                    onDragOver={(e) => {
                      const drag = draggingRef.current;
                      if (
                        !drag ||
                        moving ||
                        !classifyTarget(drag, panel.panel_id, slot.slot_id)
                      ) {
                        return;
                      }
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "move";
                    }}
                    onDrop={(e) => {
                      const drag = draggingRef.current;
                      if (!drag || moving || !onMove) return;
                      if (!classifyTarget(drag, panel.panel_id, slot.slot_id)) {
                        return;
                      }
                      e.preventDefault();
                      endDrag();
                      void onMove({
                        assignment: drag,
                        panelId: panel.panel_id,
                        slotId: slot.slot_id,
                      });
                    }}
                    className={`border-b border-r border-neutral-100 p-0 align-top transition-colors ${
                      kind === "ok"
                        ? "bg-blue-50 ring-1 ring-inset ring-blue-400"
                        : kind === "clash"
                          ? "bg-amber-50 ring-1 ring-inset ring-amber-400"
                          : ""
                    }`}
                  >
                    {here.length === 0 ? (
                      <div className="h-full min-h-[3rem] px-3 py-2 text-neutral-300">
                        {kind === "ok" ? (
                          <span className="text-blue-500">Drop here</span>
                        ) : kind === "clash" ? (
                          <span className="text-amber-600">
                            Drop here · clash
                          </span>
                        ) : (
                          "·"
                        )}
                      </div>
                    ) : (
                      here.map((a) => (
                        <div key={a.assignment_id} className="relative">
                          <button
                            type="button"
                            draggable={Boolean(onMove) && !moving}
                            onDragStart={(e) => {
                              e.dataTransfer.effectAllowed = "move";
                              e.dataTransfer.setData(
                                "text/plain",
                                a.assignment_id,
                              );
                              beginDrag(a);
                            }}
                            onDragEnd={endDrag}
                            onClick={() => onSelect(a)}
                            title={buildTitle(a)}
                            className={`block h-full min-h-[3rem] w-full px-3 py-2 text-left transition-colors ${
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
                            <span className="block truncate pr-6 font-medium">
                              {a.full_name}
                            </span>
                            <span
                              className={`block truncate ${a.is_clash ? "text-red-500" : "text-neutral-500"}`}
                            >
                              {a.sub_division}
                            </span>
                          </button>
                          {onToggleLock ? (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                void onToggleLock(a);
                              }}
                              disabled={moving}
                              aria-pressed={a.is_locked}
                              aria-label={
                                a.is_locked
                                  ? `Unlock ${a.full_name}'s interview`
                                  : `Lock ${a.full_name}'s interview`
                              }
                              title={
                                a.is_locked
                                  ? "Locked — every re-solve keeps this. Click to unlock."
                                  : "Unlocked — a re-solve may move this. Click to lock."
                              }
                              className={`absolute right-0.5 top-0.5 rounded px-1 py-0.5 text-[11px] leading-none transition-opacity hover:bg-white/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-neutral-400 disabled:cursor-not-allowed disabled:opacity-40 ${
                                a.is_locked ? "opacity-100" : "opacity-30 hover:opacity-80"
                              }`}
                            >
                              {a.is_locked ? "🔒" : "🔓"}
                            </button>
                          ) : (
                            a.is_locked && (
                              <span
                                className="absolute right-1 top-1 text-[10px] leading-none"
                                title="Locked. The solver will not move this."
                              >
                                🔒
                              </span>
                            )
                          )}
                        </div>
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
