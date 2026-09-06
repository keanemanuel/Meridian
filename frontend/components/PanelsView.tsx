"use client";

import { formatDate, formatTime, panelCards } from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { EmptyState } from "./ui";

/** One card per panel showing the order it runs its interviews in (FR-32). */
export function PanelsView({ assignments }: { assignments: Assignment[] }) {
  const cards = panelCards(assignments);

  if (cards.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {cards.map(({ panel, runOrder }) => {
        const clashes = runOrder.filter((a) => a.is_clash).length;
        return (
          <section
            key={panel.panel_id}
            className="overflow-hidden rounded-lg border border-neutral-200 bg-white"
          >
            <header className="flex items-baseline justify-between border-b border-neutral-200 px-4 py-2.5">
              <div>
                <h3 className="text-sm font-semibold text-neutral-900">
                  {panel.panel_id}
                </h3>
                <p className="text-xs text-neutral-400">{panel.room}</p>
              </div>
              <p className="text-xs text-neutral-500">
                {runOrder.length} interview{runOrder.length === 1 ? "" : "s"}
                {clashes > 0 && (
                  <span className="ml-1 text-red-600">· {clashes} clash</span>
                )}
              </p>
            </header>

            <ol className="divide-y divide-neutral-100">
              {runOrder.map((a, i) => (
                <li
                  key={a.assignment_id}
                  className={`flex items-baseline gap-3 px-4 py-2 text-sm ${
                    a.is_clash ? "bg-red-100 text-red-600" : ""
                  }`}
                >
                  <span className="w-5 shrink-0 text-xs text-neutral-400">
                    {i + 1}
                  </span>
                  <span className="w-24 shrink-0 text-xs text-neutral-500">
                    {formatDate(a.date)} {formatTime(a.start_time)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">
                      {a.full_name}
                      {a.is_locked && <span className="ml-1">🔒</span>}
                    </span>
                    <span
                      className={`block truncate text-xs ${a.is_clash ? "text-red-500" : "text-neutral-500"}`}
                    >
                      {a.sub_division}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}
