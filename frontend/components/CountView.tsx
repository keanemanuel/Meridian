"use client";

import type { Assignment } from "@/lib/types";
import { EmptyState } from "./ui";

/** A simple tally: interviews per sub-division, both choices counted
 * together — so the total is every interview in the run, not every
 * applicant. */
export function CountView({ assignments }: { assignments: Assignment[] }) {
  if (assignments.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const counts = new Map<string, number>();
  for (const a of assignments) {
    counts.set(a.sub_division, (counts.get(a.sub_division) ?? 0) + 1);
  }
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1]);

  return (
    <div className="card max-w-md overflow-hidden">
      <table className="w-full text-sm">
        <tbody className="divide-y divide-rule">
          {rows.map(([subDivision, count]) => (
            <tr key={subDivision}>
              <td className="px-4 py-2 text-ink-soft">{subDivision}</td>
              <td className="px-4 py-2 text-right font-medium tabular-nums text-ink">
                {count}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t-2 border-purple-deep bg-paper-sunk">
            <td className="px-4 py-2 font-semibold text-purple-deep">Total</td>
            <td className="px-4 py-2 text-right font-semibold tabular-nums text-purple-deep">
              {assignments.length}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
