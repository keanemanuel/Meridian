export default function Home() {
  return (
    <div className="mx-auto max-w-2xl px-8 py-16">
      <h1 className="text-lg font-semibold text-neutral-900">
        Welcome to IFF Recruitment
      </h1>
      <p className="mt-2 text-sm leading-relaxed text-neutral-600">
        Select a workspace from the sidebar or create a new one to get started.
        Each workspace keeps its own applicants, its own timetable and its own
        send history, so two intake rounds never mix.
      </p>

      <p className="mt-6 text-sm font-medium text-neutral-800">
        How a round runs
      </p>
      <ol className="mt-3 space-y-3 text-sm text-neutral-600">
        {[
          [
            "Import Data",
            "Upload a CSV export of the Google Form responses.",
          ],
          [
            "Schedule!",
            "Place every applicant's two interviews on the grid.",
          ],
          [
            "Publish",
            "Room, applicant and panel views, plus the conflict report.",
          ],
        ].map(([step, blurb], i) => (
          <li key={step} className="flex gap-3">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-neutral-200 text-xs font-medium text-neutral-700">
              {i + 1}
            </span>
            <span>
              <span className="font-medium text-neutral-800">{step}</span>
              {". "}
              {blurb}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
