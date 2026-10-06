"use client";

import { useState } from "react";
import { applicantRows, formatDate, formatTime } from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { Badge, EmptyState } from "./ui";

function TimeCell({ a }: { a: Assignment | null }) {
  if (!a) return <span className="text-ink-faint">·</span>;
  return (
    <span
      className={`whitespace-nowrap ${a.is_clash ? "text-danger" : "text-ink-soft"}`}
    >
      {formatDate(a.date)} {formatTime(a.start_time)}
    </span>
  );
}

type InterviewCountFilter = "all" | "2" | "1";

/** Scheduled interviews visible for this applicant row (0, 1 or 2). */
const scheduledCount = (r: { first: Assignment | null; second: Assignment | null }) =>
  (r.first ? 1 : 0) + (r.second ? 1 : 0);

/** One row per applicant, both choices side by side (FR-31). */
export function ApplicantsView({ assignments }: { assignments: Assignment[] }) {
  const [query, setQuery] = useState("");
  const [clashOnly, setClashOnly] = useState(false);
  const [countFilter, setCountFilter] = useState<InterviewCountFilter>("all");

  const all = applicantRows(assignments);
  const q = query.trim().toLowerCase();
  const rows = all.filter(
    (r) =>
      (!clashOnly || r.hasClash) &&
      (countFilter === "all" || scheduledCount(r) === Number(countFilter)) &&
      (!q ||
        r.full_name.toLowerCase().includes(q) ||
        r.applicant_id.toLowerCase().includes(q) ||
        r.email.toLowerCase().includes(q)),
  );

  if (all.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const headers = [
    "#",
    "Name",
    "Preference",
    "Div 1",
    "Time 1",
    "Room 1",
    "Div 2",
    "Time 2",
    "Room 2",
    "Clash",
  ];

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by name, id or email…"
          className="input w-64 max-w-full"
        />
        <label className="flex items-center gap-2 text-sm text-ink-soft">
          <input
            type="checkbox"
            checked={clashOnly}
            onChange={(e) => setClashOnly(e.target.checked)}
          />
          Clashes only
        </label>
        <label className="flex items-center gap-2 text-sm text-ink-soft">
          Interview count
          <select
            value={countFilter}
            onChange={(e) =>
              setCountFilter(e.target.value as InterviewCountFilter)
            }
            className="input"
          >
            <option value="all">Any</option>
            <option value="2">2 interviews</option>
            <option value="1">1 interview</option>
          </select>
        </label>
        <span className="text-xs text-ink-muted">
          {rows.length} of {all.length} applicants
        </span>
      </div>

      <div className="card overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="type-label whitespace-nowrap border-b-2 border-purple-deep bg-paper-sunk text-[11px] text-purple-deep">
            <tr>
              {headers.map((h) => (
                <th key={h} className="px-4 py-2 text-left font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-rule">
            {rows.map((r, i) => (
              <tr key={r.applicant_id} className={r.hasClash ? "bg-danger-wash" : ""}>
                <td className="px-4 py-2 text-right tabular-nums text-ink-muted">
                  {i + 1}
                </td>
                <td className="px-4 py-2">
                  <span className="block font-medium text-ink">
                    {r.full_name}
                  </span>
                  <span className="block text-xs text-ink-muted">
                    {r.applicant_id}
                  </span>
                </td>

                <td
                  className="px-4 py-2 text-ink-soft"
                  title="Day / time preference declared on the form"
                >
                  {r.availability || <span className="text-ink-faint">·</span>}
                </td>

                <td className="px-4 py-2 text-ink-soft">
                  {r.first?.sub_division ?? <span className="text-ink-faint">·</span>}
                </td>
                <td className="px-4 py-2">
                  <TimeCell a={r.first} />
                </td>
                <td className="px-4 py-2 text-ink-soft">
                  {r.first ? (
                    <>
                      {r.first.room}
                      {r.first.is_locked && <span className="ml-1">🔒</span>}
                    </>
                  ) : (
                    <span className="text-ink-faint">·</span>
                  )}
                </td>

                <td className="px-4 py-2 text-ink-soft">
                  {r.second?.sub_division ?? <span className="text-ink-faint">·</span>}
                </td>
                <td className="px-4 py-2">
                  <TimeCell a={r.second} />
                </td>
                <td className="px-4 py-2 text-ink-soft">
                  {r.second ? (
                    <>
                      {r.second.room}
                      {r.second.is_locked && <span className="ml-1">🔒</span>}
                    </>
                  ) : (
                    <span className="text-ink-faint">·</span>
                  )}
                </td>

                <td className="px-4 py-2">
                  {r.hasClash ? (
                    <Badge tone="danger">CLASH</Badge>
                  ) : (
                    <span className="text-ink-faint">·</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
