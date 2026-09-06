"use client";

import {
  cellIndex,
  cellKey,
  formatDate,
  formatTime,
  panelAxis,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { EmptyState } from "./ui";

/** Timetable grid: rows are slots, columns are panels (FR-30). */
export function RoomView({
  assignments,
  onSelect,
}: {
  assignments: Assignment[];
  onSelect: (a: Assignment) => void;
}) {
  const slots = slotAxis(assignments);
  const panels = panelAxis(assignments);
  const cells = cellIndex(assignments);

  if (slots.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  return (
    <div className="overflow-auto rounded-lg border border-neutral-200 bg-white">
      <table className="min-w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="sticky left-0 top-0 z-20 w-36 border-b border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-500">
              Slot
            </th>
            {panels.map((p) => (
              <th
                key={p.panel_id}
                className="sticky top-0 z-10 min-w-[10rem] border-b border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-700"
              >
                <span className="block">{p.panel_id}</span>
                <span className="block font-normal text-neutral-400">
                  {p.room}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {slots.map((slot, i) => {
            const newDay = i === 0 || slots[i - 1].date !== slot.date;
            return (
              <tr key={slot.slot_id}>
                <th
                  scope="row"
                  className={`sticky left-0 z-10 border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left align-top font-normal ${
                    newDay ? "border-t-2 border-t-neutral-300" : "border-b border-neutral-100"
                  }`}
                >
                  <span className="block font-medium text-neutral-700">
                    {formatTime(slot.start_time)}–{formatTime(slot.end_time)}
                  </span>
                  <span className="block text-neutral-400">
                    {formatDate(slot.date)}
                  </span>
                </th>

                {panels.map((panel) => {
                  const here = cells.get(cellKey(panel.panel_id, slot.slot_id)) ?? [];
                  return (
                    <td
                      key={panel.panel_id}
                      className={`border-r border-neutral-200 p-0 align-top ${
                        newDay ? "border-t-2 border-t-neutral-300" : "border-b border-neutral-100"
                      }`}
                    >
                      {here.length === 0 ? (
                        <div className="h-full min-h-[3rem] px-3 py-2 text-neutral-300">
                          —
                        </div>
                      ) : (
                        here.map((a) => (
                          <button
                            key={a.assignment_id}
                            type="button"
                            onClick={() => onSelect(a)}
                            title={`${a.full_name} — ${a.sub_division}${a.is_clash ? " (clash)" : ""}${a.is_locked ? " (locked)" : ""}`}
                            className={`relative block h-full w-full min-h-[3rem] px-3 py-2 text-left transition-colors ${
                              a.is_clash
                                ? "bg-red-100 text-red-600 hover:bg-red-200"
                                : "text-neutral-800 hover:bg-neutral-100"
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
                                title="Locked — the solver will not move this"
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
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
