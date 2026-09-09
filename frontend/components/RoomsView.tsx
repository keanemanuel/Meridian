"use client";

import { roomSummaries } from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { Badge, EmptyState } from "./ui";

/** One card per room: how many distinct interviews it hosts and which
 * divisions are represented in it. Grouping is by room, so a room that runs
 * more than one panel is a single bucket. */
export function RoomsView({ assignments }: { assignments: Assignment[] }) {
  const rooms = roomSummaries(assignments);

  if (rooms.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {rooms.map((r) => (
        <section
          key={r.room}
          className="overflow-hidden rounded-lg border border-neutral-200 bg-white"
        >
          <header className="flex items-baseline justify-between border-b border-neutral-200 px-4 py-2.5">
            <div>
              <h3 className="text-sm font-semibold text-neutral-900">
                Room {r.room}
              </h3>
              <p className="text-xs text-neutral-400">
                {r.panels.length} panel{r.panels.length === 1 ? "" : "s"}:{" "}
                {r.panels.join(", ")}
              </p>
            </div>
            <p className="shrink-0 text-xs text-neutral-500">
              {r.interviewCount} interview{r.interviewCount === 1 ? "" : "s"}
              {r.clashes > 0 && (
                <span className="ml-1 text-red-600">· {r.clashes} clash</span>
              )}
            </p>
          </header>

          <div className="px-4 py-3">
            <p className="mb-2 text-xs uppercase tracking-wide text-neutral-400">
              Divisions represented ({r.divisions.length})
            </p>
            <ul className="flex flex-wrap gap-1.5">
              {r.divisions.map((d) => (
                <li key={d.division}>
                  <Badge>
                    {d.division}
                    <span className="ml-1 tabular-nums text-neutral-400">
                      {d.count}
                    </span>
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        </section>
      ))}
    </div>
  );
}
