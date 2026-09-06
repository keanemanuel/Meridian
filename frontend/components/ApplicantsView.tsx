"use client";

import { useState } from "react";
import { applicantRows, formatDate, formatTime } from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { Badge, EmptyState } from "./ui";

function TimeCell({ a }: { a: Assignment | null }) {
  if (!a) return <span className="text-neutral-300">—</span>;
  return (
    <span className={a.is_clash ? "text-red-600" : "text-neutral-700"}>
      {formatDate(a.date)} {formatTime(a.start_time)}
    </span>
  );
}

/** One row per applicant, both choices side by side (FR-31). */
export function ApplicantsView({ assignments }: { assignments: Assignment[] }) {
  const [query, setQuery] = useState("");
  const [clashOnly, setClashOnly] = useState(false);

  const all = applicantRows(assignments);
  const q = query.trim().toLowerCase();
  const rows = all.filter(
    (r) =>
      (!clashOnly || r.hasClash) &&
      (!q ||
        r.full_name.toLowerCase().includes(q) ||
        r.applicant_id.toLowerCase().includes(q) ||
        r.email.toLowerCase().includes(q)),
  );

  if (all.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const headers = [
    "Name",
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
          className="w-64 rounded-md border border-neutral-300 px-3 py-1.5 text-sm outline-none focus:border-neutral-500"
        />
        <label className="flex items-center gap-2 text-sm text-neutral-600">
          <input
            type="checkbox"
            checked={clashOnly}
            onChange={(e) => setClashOnly(e.target.checked)}
          />
          Clashes only
        </label>
        <span className="text-xs text-neutral-400">
          {rows.length} of {all.length} applicants
        </span>
      </div>

      <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
        <table className="min-w-full text-sm">
          <thead className="bg-neutral-50 text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              {headers.map((h) => (
                <th key={h} className="px-4 py-2 text-left font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {rows.map((r) => (
              <tr key={r.applicant_id} className={r.hasClash ? "bg-red-50" : ""}>
                <td className="px-4 py-2">
                  <span className="block font-medium text-neutral-800">
                    {r.full_name}
                  </span>
                  <span className="block text-xs text-neutral-400">
                    {r.applicant_id}
                  </span>
                </td>

                <td className="px-4 py-2 text-neutral-700">
                  {r.first?.sub_division ?? <span className="text-neutral-300">—</span>}
                </td>
                <td className="px-4 py-2">
                  <TimeCell a={r.first} />
                </td>
                <td className="px-4 py-2 text-neutral-700">
                  {r.first ? (
                    <>
                      {r.first.room}
                      {r.first.is_locked && <span className="ml-1">🔒</span>}
                    </>
                  ) : (
                    <span className="text-neutral-300">—</span>
                  )}
                </td>

                <td className="px-4 py-2 text-neutral-700">
                  {r.second?.sub_division ?? <span className="text-neutral-300">—</span>}
                </td>
                <td className="px-4 py-2">
                  <TimeCell a={r.second} />
                </td>
                <td className="px-4 py-2 text-neutral-700">
                  {r.second ? (
                    <>
                      {r.second.room}
                      {r.second.is_locked && <span className="ml-1">🔒</span>}
                    </>
                  ) : (
                    <span className="text-neutral-300">—</span>
                  )}
                </td>

                <td className="px-4 py-2">
                  {r.hasClash ? (
                    <Badge tone="red">CLASH</Badge>
                  ) : (
                    <span className="text-neutral-300">—</span>
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
