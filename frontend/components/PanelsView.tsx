"use client";

import { useState } from "react";
import {
  assignmentMatches,
  formatDate,
  formatTime,
  panelCards,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { EmptyState, SearchBar } from "./ui";

/** One card per panel showing the order it runs its interviews in (FR-32). */
export function PanelsView({ assignments }: { assignments: Assignment[] }) {
  const [query, setQuery] = useState("");
  const cards = panelCards(assignments);

  if (cards.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  return (
    <>
      <SearchBar value={query} onChange={setQuery} />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {cards.map(({ panel, runOrder }) => {
          const clashes = runOrder.filter((a) => a.is_clash).length;
          return (
            <section
              key={panel.panel_id}
              className="card overflow-hidden"
            >
              <header className="flex items-baseline justify-between border-b-2 border-purple-deep px-4 py-2.5">
                <div>
                  <h3 className="type-label text-sm text-purple-deep">
                    {panel.panel_id}
                  </h3>
                  <p className="text-xs text-ink-muted">{panel.room}</p>
                </div>
                <p className="text-xs text-ink-muted">
                  {runOrder.length} interview{runOrder.length === 1 ? "" : "s"}
                  {clashes > 0 && (
                    <span className="ml-1 text-danger">· {clashes} clash</span>
                  )}
                </p>
              </header>

              <ol className="divide-y divide-rule">
                {runOrder.map((a, i) => (
                  <li
                    key={a.assignment_id}
                    className={`flex items-baseline gap-3 px-4 py-2 text-sm ${
                      a.is_clash ? "bg-danger-wash text-danger" : ""
                    } ${
                      assignmentMatches(a, query)
                        ? "ring-2 ring-inset ring-accent-pink-ink"
                        : ""
                    }`}
                  >
                    <span className="w-5 shrink-0 text-xs text-ink-muted">
                      {i + 1}
                    </span>
                    <span className="w-24 shrink-0 text-xs text-ink-muted">
                      {formatDate(a.date)} {formatTime(a.start_time)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">
                        {a.full_name}
                        {a.is_locked && <span className="ml-1">🔒</span>}
                      </span>
                      <span
                        className={`block truncate text-xs ${a.is_clash ? "text-danger" : "text-ink-muted"}`}
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
    </>
  );
}
