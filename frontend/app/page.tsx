export default function Home() {
  return (
    <div className="mx-auto max-w-2xl px-8 py-16">
      <h1 className="text-lg font-semibold text-neutral-900">Meridian</h1>
      <p className="mt-2 text-sm leading-relaxed text-neutral-600">
        Interview scheduler for IFF recruitment. Pick a workspace in the sidebar
        to import applicants, check capacity, solve the timetable and send
        invites — or create one with <span className="font-medium">+ New</span>.
      </p>

      <ol className="mt-8 space-y-3 text-sm text-neutral-600">
        {[
          ["Import Data", "Read applicants from a CSV export or the linked Google Sheet."],
          ["Check Capacity", "Per-division demand vs supply, before any solve."],
          ["Solve", "Place every applicant's two interviews on the grid."],
          ["Publish", "Room, applicant and panel views, plus the conflict report."],
        ].map(([step, blurb], i) => (
          <li key={step} className="flex gap-3">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-neutral-200 text-xs font-medium text-neutral-700">
              {i + 1}
            </span>
            <span>
              <span className="font-medium text-neutral-800">{step}</span> —{" "}
              {blurb}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
