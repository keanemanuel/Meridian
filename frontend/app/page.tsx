const STEPS = [
  ["Import Data", "Upload a CSV export of the Google Form responses."],
  ["Schedule!", "Place every applicant's two interviews on the grid."],
  ["Publish", "Room, applicant and panel views, plus the conflict report."],
];

export default function Home() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10 sm:px-8 sm:py-14">
      <p className="eyebrow">IFF Recruitment · Interview scheduler</p>
      <h1 className="display mt-3">Welcome to IFF Recruitment</h1>
      <p className="mt-5 max-w-xl text-base text-ink">
        Select a workspace from the sidebar or create a new one to get started.
        Each workspace keeps its own applicants, its own timetable and its own
        send history, so two intake rounds never mix.
      </p>

      <p className="eyebrow mt-12">How a round runs</p>
      {/* The three steps as frames on a strip of film. */}
      <ol className="filmstrip mt-3 grid gap-3 px-3 py-6 sm:grid-cols-3">
        {STEPS.map(([step, blurb], i) => (
          <li key={step} className="flex flex-col bg-paper-raised text-ink">
            <div className="flex-1 px-4 pt-4">
              <p className="eyebrow" aria-hidden="true">
                Step {String(i + 1).padStart(2, "0")} /{" "}
                {String(STEPS.length).padStart(2, "0")}
              </p>
              <p className="section-title mt-2 text-base">{step}</p>
              <p className="mt-2 text-sm text-ink-soft">{blurb}</p>
            </div>
            <div className="halftone h-10" aria-hidden="true" />
          </li>
        ))}
      </ol>
    </div>
  );
}
