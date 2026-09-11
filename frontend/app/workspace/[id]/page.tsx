"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useState } from "react";
import { ImportModal } from "@/components/ImportModal";
import { Modal } from "@/components/Modal";
import { RoomView } from "@/components/RoomView";
import { useToast } from "@/components/Toast";
import { Badge, Button, EmptyState, Spinner } from "@/components/ui";
import { ApiError, api, saveBlob } from "@/lib/api";
import { formatRunId } from "@/lib/schedule";
import type {
  Assignment,
  CapacityCheck,
  RejectedRow,
  RunSummary,
  SolveResult,
  WorkspaceMeta,
} from "@/lib/types";

type Action = "import" | "check" | "solve" | "download";
type Tab = "runs" | "rejected";

export default function WorkspacePage({
  params,
}: PageProps<"/workspace/[id]">) {
  const { id: rawId } = use(params);
  const workspaceId = decodeURIComponent(rawId);
  const toast = useToast();
  const router = useRouter();

  const [meta, setMeta] = useState<WorkspaceMeta | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [rejected, setRejected] = useState<RejectedRow[]>([]);
  const [tab, setTab] = useState<Tab>("runs");
  /** row_number currently being recovered, so its button shows a spinner. */
  const [recovering, setRecovering] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** Whether applicants have been ingested for this workspace. `null` = not
   * known yet (still loading, or the status call failed) — only an explicit
   * `false` disables Check Capacity / Schedule!, so a status hiccup never
   * locks the controls. */
  const [ingested, setIngested] = useState<boolean | null>(null);

  const [busy, setBusy] = useState<Action | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [capacity, setCapacity] = useState<CapacityCheck | null>(null);
  /** Set when solve came back 409 INFEASIBLE — the user may override (E-06). */
  const [infeasible, setInfeasible] = useState<string | null>(null);
  /** Latest run's assignments, shown inline below. Publish runs as part of
   * scheduling, so there is no separate Publish click. */
  const [schedule, setSchedule] = useState<{
    runId: string;
    assignments: Assignment[];
  } | null>(null);
  /** The counters from the most recent solve in this session, shown
   * prominently as "X / Y interviews placed". */
  const [lastSolve, setLastSolve] = useState<SolveResult | null>(null);

  const loadRuns = useCallback(async () => {
    const list = await api.listRuns(workspaceId);
    // Run ids are lexically sortable timestamps; newest first.
    const sorted = [...list].sort((a, b) => b.run_id.localeCompare(a.run_id));
    setRuns(sorted);
    return sorted;
  }, [workspaceId]);

  const loadIngestStatus = useCallback(async () => {
    try {
      const status = await api.ingestStatus(workspaceId);
      setIngested(status.ingested);
    } catch {
      // A status hiccup shouldn't lock Check / Schedule — leave it "unknown".
      setIngested(null);
    }
  }, [workspaceId]);

  const loadRejected = useCallback(async () => {
    try {
      setRejected(await api.listRejected(workspaceId));
    } catch {
      // No validation report yet (nothing imported) is not an error here.
      setRejected([]);
    }
  }, [workspaceId]);

  const loadSchedule = useCallback(
    async (runId: string) => {
      try {
        const assignments = await api.getAssignments(workspaceId, runId);
        setSchedule({ runId, assignments });
      } catch {
        // A run row with no assignments yet is not an error worth surfacing here.
        setSchedule(null);
      }
    },
    [workspaceId],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      // A capacity table and schedule belong to the workspace they were run for.
      setCapacity(null);
      setSchedule(null);
      setIngested(null);
      try {
        const [m, sorted] = await Promise.all([
          api.getWorkspace(workspaceId),
          loadRuns(),
          loadRejected(),
          loadIngestStatus(),
        ]);
        if (cancelled) return;
        setMeta(m);
        setLoadError(null);
        const latest = sorted.find((r) => r.has_assignments);
        if (latest) await loadSchedule(latest.run_id);
      } catch (err) {
        if (!cancelled) setLoadError((err as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, loadRuns, loadRejected, loadSchedule, loadIngestStatus]);

  const recover = async (row: RejectedRow) => {
    setRecovering(row.row_number);
    try {
      const res = await api.recover(workspaceId, row.row_number);
      toast.success(res.message);
      await Promise.all([loadRejected(), loadRuns(), loadIngestStatus()]);
    } catch (err) {
      toast.fromError(err, "Recover failed.");
    } finally {
      setRecovering(null);
    }
  };

  const runCheck = async () => {
    setBusy("check");
    try {
      const result = await api.check(workspaceId);
      setCapacity(result);
      if (result.feasible) {
        toast.success("Capacity looks feasible for every division.");
      } else {
        toast.error(
          `INFEASIBLE for ${result.infeasible_divisions.join(", ")}. Add panels before solving.`,
        );
      }
    } catch (err) {
      toast.fromError(err, "Capacity check failed.");
    } finally {
      setBusy(null);
    }
  };

  const runSolve = async (skipCheck = false) => {
    setBusy("solve");
    setInfeasible(null);
    toast.info("Scheduling…");
    try {
      const result = await api.solve(workspaceId, skipCheck);
      // Publish runs automatically as part of Solve — it builds the room /
      // applicant / panel views and the conflict report. Kept best-effort:
      // a failure here doesn't undo a good solve.
      try {
        await api.publish(workspaceId, "latest");
      } catch (err) {
        toast.fromError(err, "Solved, but building the published views failed.");
      }
      setLastSolve(result);
      toast.success(
        `Schedule complete! ${result.interviews_placed}/${result.interviews_required} ` +
          `interviews placed, ${result.clashes} clash(es), ${result.locked} locked, ` +
          `${result.solve_seconds}s.`,
      );
      if (result.warnings && result.warnings.length > 0) {
        toast.info(
          "Extra panels were added automatically to fit demand. Review your " +
            "staffing before sending invites.",
          result.warnings,
        );
      }
      await loadRuns();
      await loadSchedule(result.run_id);
    } catch (err) {
      // 409 is the Capacity Advisor refusing to proceed — offer the override.
      if (err instanceof ApiError && err.status === 409) {
        setInfeasible(err.message);
      } else {
        toast.fromError(err, "Solve failed.");
      }
    } finally {
      setBusy(null);
    }
  };

  const downloadXlsx = async () => {
    if (!schedule) return;
    setBusy("download");
    try {
      const { blob, filename } = await api.downloadXlsx(workspaceId, schedule.runId);
      saveBlob(blob, filename);
    } catch (err) {
      toast.fromError(err, "Download failed.");
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return (
      <p className="flex items-center gap-2 px-8 py-8 text-sm text-neutral-500">
        <Spinner /> Loading workspace…
      </p>
    );
  }

  if (loadError || !meta) {
    return (
      <div className="px-8 py-8">
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {loadError ?? `Workspace "${workspaceId}" not found.`}
        </div>
      </div>
    );
  }

  const runHref = (runId: string) =>
    `/workspace/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}`;

  return (
    <div className="mx-auto max-w-5xl px-8 py-8">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold text-neutral-900">{meta.name}</h1>
        <Badge>{meta.group}</Badge>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        <Button
          onClick={() => setShowImport(true)}
          loading={busy === "import"}
          disabled={busy !== null}
        >
          Import Data
        </Button>
        <Button
          onClick={runCheck}
          loading={busy === "check"}
          disabled={busy !== null || ingested === false}
          title={
            ingested === false
              ? "Import applicant data first"
              : "Run the Capacity Advisor before solving"
          }
        >
          Check Capacity
        </Button>
        <Button
          variant="primary"
          onClick={() => runSolve(false)}
          loading={busy === "solve"}
          disabled={busy !== null || ingested === false}
          title={
            ingested === false ? "Import applicant data first" : undefined
          }
        >
          Schedule!
        </Button>
        <Button
          onClick={downloadXlsx}
          loading={busy === "download"}
          disabled={busy !== null || schedule === null}
          title={
            schedule
              ? "Download the published schedule as an Excel workbook"
              : "Schedule first, then there is something to download"
          }
        >
          Download XLSX
        </Button>
      </div>

      {ingested === false && (
        <p className="mt-2 text-xs text-neutral-500">
          Import applicant data to enable Check Capacity and Schedule.
        </p>
      )}

      {busy === "solve" && (
        <div className="mt-5 flex items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
          <Spinner />
          Solving schedule, this can take up to 2 minutes. Keep this tab open.
        </div>
      )}

      {lastSolve && (
        <div className="mt-5 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-neutral-200 bg-white px-4 py-3">
          <span className="text-2xl font-semibold tabular-nums text-neutral-900">
            {lastSolve.interviews_placed} / {lastSolve.interviews_required}
          </span>
          <span className="text-sm text-neutral-600">interviews placed</span>
          <span className="ml-auto text-xs text-neutral-500">
            {lastSolve.clashes} clash{lastSolve.clashes === 1 ? "" : "es"} ·{" "}
            {lastSolve.locked} locked · {lastSolve.solve_seconds}s ·{" "}
            {lastSolve.status}
          </span>
        </div>
      )}

      {capacity && <CapacityTable check={capacity} />}

      <section className="mt-10">
        <div className="mb-3 flex items-center gap-1 border-b border-neutral-200">
          <TabButton active={tab === "runs"} onClick={() => setTab("runs")}>
            Run history
          </TabButton>
          <TabButton
            active={tab === "rejected"}
            onClick={() => setTab("rejected")}
          >
            Rejected
            {rejected.length > 0 && (
              <span className="ml-1.5 rounded-full bg-red-100 px-1.5 text-xs font-semibold text-red-700 tabular-nums">
                {rejected.length}
              </span>
            )}
          </TabButton>
        </div>

        {tab === "runs" &&
          (runs.length === 0 ? (
            <EmptyState
              title="No runs yet"
              hint="Import applicants, check capacity, then press Schedule! to produce the first timetable."
            />
          ) : (
            <ul className="divide-y divide-neutral-200 overflow-hidden rounded-lg border border-neutral-200 bg-white">
              {runs.map((run, i) => (
                <li key={run.run_id}>
                  <Link
                    href={runHref(run.run_id)}
                    className="flex items-center gap-3 px-4 py-3 text-sm transition-colors hover:bg-neutral-50"
                  >
                    <span className="flex-1 font-medium text-neutral-800">
                      {formatRunId(run.run_id)}
                    </span>
                    {i === 0 && <Badge tone="green">latest</Badge>}
                    {!run.has_assignments && (
                      <Badge tone="amber">no assignments</Badge>
                    )}
                    <span className="font-mono text-xs text-neutral-400">
                      {run.run_id}
                    </span>
                    <span className="text-neutral-300">›</span>
                  </Link>
                </li>
              ))}
            </ul>
          ))}

        {tab === "rejected" && (
          <RejectedTable
            rows={rejected}
            recovering={recovering}
            onRecover={recover}
          />
        )}
      </section>

      {schedule && schedule.assignments.length > 0 && (
        <section className="mt-10">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-semibold text-neutral-900">Schedule</h2>
            <span className="text-xs text-neutral-500">
              {formatRunId(schedule.runId)}
            </span>
            <Link
              href={runHref(schedule.runId)}
              className="ml-auto text-xs font-medium text-neutral-600 hover:text-neutral-900"
            >
              Open full view to edit, re-solve or send ›
            </Link>
          </div>
          <RoomView
            assignments={schedule.assignments}
            onSelect={() => router.push(runHref(schedule.runId))}
          />
        </section>
      )}

      {showImport && (
        <ImportModal
          workspaceId={workspaceId}
          onClose={() => setShowImport(false)}
          onDone={(result) => {
            toast.success(
              `Imported ${result.applicants} applicant(s): ${result.rejected} rejected, ` +
                `${result.collapsed} collapsed, ${result.warnings} warning(s).`,
            );
            void loadRejected();
            void loadIngestStatus();
          }}
        />
      )}

      {infeasible && (
        <Modal title="Capacity Advisor says INFEASIBLE" onClose={() => setInfeasible(null)}>
          <p className="text-sm leading-relaxed text-neutral-700">{infeasible}</p>
          <p className="mt-3 text-xs text-neutral-500">
            Solving anyway will produce a schedule, but it is likely to contain
            forced clashes. Adding panels is the better fix.
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setInfeasible(null)}>Cancel</Button>
            <Button variant="danger" onClick={() => void runSolve(true)}>
              Solve anyway
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`-mb-px flex items-center border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
        active
          ? "border-neutral-900 text-neutral-900"
          : "border-transparent text-neutral-500 hover:text-neutral-800"
      }`}
    >
      {children}
    </button>
  );
}

function RejectedTable({
  rows,
  recovering,
  onRecover,
}: {
  rows: RejectedRow[];
  recovering: number | null;
  onRecover: (row: RejectedRow) => void;
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="Nothing rejected"
        hint="Every imported row passed validation, or no data has been imported yet."
      />
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-neutral-50 text-xs uppercase tracking-wide text-neutral-500">
          <tr>
            {[
              "CSV Row",
              "Name",
              "Email",
              "Sub-div 1",
              "Sub-div 2",
              "Reason",
              "Recover",
            ].map((h) => (
              <th key={h} className="px-4 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-100">
          {rows.map((row) => (
            <tr key={row.row_number}>
              <td className="px-4 py-2 font-mono text-xs tabular-nums text-neutral-500">
                {row.csv_row}
              </td>
              <td className="px-4 py-2 font-medium text-neutral-800">
                {row.full_name || <span className="text-neutral-400">—</span>}
              </td>
              <td className="px-4 py-2 text-neutral-700">
                {row.email || <span className="text-neutral-400">—</span>}
              </td>
              <td className="px-4 py-2 text-neutral-700">
                {row.sub_division_1 || (
                  <span className="text-neutral-400">—</span>
                )}
              </td>
              <td className="px-4 py-2 text-neutral-700">
                {row.sub_division_2 || (
                  <span className="text-neutral-400">—</span>
                )}
              </td>
              <td className="px-4 py-2">
                <span
                  className="font-medium text-red-600"
                  title={row.message}
                >
                  {row.reason_code}
                </span>
              </td>
              <td className="px-4 py-2">
                {row.recoverable ? (
                  <Button
                    onClick={() => onRecover(row)}
                    loading={recovering === row.row_number}
                    disabled={recovering !== null}
                  >
                    Recover
                  </Button>
                ) : (
                  <span
                    className="text-xs text-neutral-400"
                    title={
                      row.reason_code === "MISSING_EMAIL" ||
                      row.reason_code === "INVALID_EMAIL"
                        ? "No way to contact this applicant."
                        : "This row has no division to schedule against."
                    }
                  >
                    Cannot recover
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CapacityTable({ check }: { check: CapacityCheck }) {
  return (
    <section className="mt-6 overflow-hidden rounded-lg border border-neutral-200 bg-white">
      <div className="flex items-center gap-2 border-b border-neutral-200 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-neutral-900">
          Capacity Advisor
        </h2>
        <Badge tone={check.feasible ? "green" : "red"}>
          {check.feasible ? "FEASIBLE" : "INFEASIBLE"}
        </Badge>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              {["Division", "Demand", "Panels", "Raw supply", "Effective", "Recommended", "Verdict"].map(
                (h) => (
                  <th key={h} className="px-4 py-2 text-left font-medium">
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {check.rows.map((row) => {
              const bad = row.verdict === "INFEASIBLE";
              return (
                <tr key={row.division} className={bad ? "bg-red-100" : ""}>
                  <td className="px-4 py-2 font-medium text-neutral-800">
                    {row.division}
                  </td>
                  <td className="px-4 py-2 text-neutral-700">{row.demand}</td>
                  <td className="px-4 py-2 text-neutral-700">
                    {row.panels_configured}
                  </td>
                  <td className="px-4 py-2 text-neutral-700">{row.raw_supply}</td>
                  <td className="px-4 py-2 text-neutral-700">
                    {row.effective_supply}
                  </td>
                  <td className="px-4 py-2 text-neutral-700">
                    {row.recommended_panels}
                  </td>
                  <td
                    className={`px-4 py-2 font-medium ${bad ? "text-red-600" : "text-neutral-700"}`}
                  >
                    {row.verdict}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
