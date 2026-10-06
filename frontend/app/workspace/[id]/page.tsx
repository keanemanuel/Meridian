"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useState } from "react";
import { ImportModal } from "@/components/ImportModal";
import { Modal } from "@/components/Modal";
import { RoomView } from "@/components/RoomView";
import { useToast } from "@/components/Toast";
import {
  Badge,
  Button,
  EmptyState,
  Spinner,
  TabBar,
  TabButton,
} from "@/components/ui";
import { ApiError, api, saveBlob } from "@/lib/api";
import { formatRunId } from "@/lib/schedule";
import type {
  Assignment,
  RejectedRow,
  RunSummary,
  SolveResult,
  WorkspaceMeta,
} from "@/lib/types";

type Action = "import" | "solve" | "download";
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
   * `false` disables Schedule!, so a status hiccup never locks the control. */
  const [ingested, setIngested] = useState<boolean | null>(null);

  const [busy, setBusy] = useState<Action | null>(null);
  const [showImport, setShowImport] = useState(false);
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
      // A status hiccup shouldn't lock Schedule — leave it "unknown".
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
      // The schedule belongs to the workspace it was run for.
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
      <p className="flex items-center gap-2 px-5 py-8 text-sm text-ink-muted sm:px-8">
        <Spinner /> Loading workspace…
      </p>
    );
  }

  if (loadError || !meta) {
    return (
      <div className="px-5 py-8 sm:px-8">
        <div className="border-2 border-danger bg-danger-wash px-4 py-3 text-sm text-danger">
          {loadError ?? `Workspace "${workspaceId}" not found.`}
        </div>
      </div>
    );
  }

  const runHref = (runId: string) =>
    `/workspace/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}`;

  return (
    <div className="mx-auto max-w-5xl px-5 py-8 sm:px-8">
      <p className="eyebrow" aria-hidden="true">
        Workspace
      </p>
      <div className="mt-2 flex flex-wrap items-end gap-x-4 gap-y-2">
        <h1 className="display">{meta.name}</h1>
        <Badge>{meta.group}</Badge>
      </div>

      <div className="mt-7 flex flex-wrap gap-3">
        <Button
          onClick={() => setShowImport(true)}
          loading={busy === "import"}
          disabled={busy !== null}
        >
          Import Data
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
              ? "Download the schedule, rooms and applicants spreadsheets (one ZIP)"
              : "Schedule first, then there is something to download"
          }
        >
          Download XLSX
        </Button>
      </div>

      {ingested === false && (
        <p className="mt-2 text-xs text-ink-muted">
          Import applicant data to enable Schedule.
        </p>
      )}

      {busy === "solve" && (
        <div className="mt-6 flex items-center gap-2 border-2 border-accent-blue bg-accent-blue-wash px-4 py-3 text-sm text-accent-blue">
          <Spinner />
          Solving schedule, this can take up to 2 minutes. Keep this tab open.
        </div>
      )}

      {lastSolve && (
        <div className="card mt-6 flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-3 shadow-hard-sm">
          <span className="text-2xl font-semibold tabular-nums text-purple-deep">
            {lastSolve.interviews_placed} / {lastSolve.interviews_required}
          </span>
          <span className="text-sm text-ink-soft">interviews placed</span>
          <span className="ml-auto text-xs text-ink-muted">
            {lastSolve.clashes} clash{lastSolve.clashes === 1 ? "" : "es"} ·{" "}
            {lastSolve.locked} locked · {lastSolve.solve_seconds}s ·{" "}
            {lastSolve.status}
          </span>
        </div>
      )}

      <section className="mt-10">
        <div className="mb-4">
          <TabBar>
            <TabButton active={tab === "runs"} onClick={() => setTab("runs")}>
              Run history
            </TabButton>
            <TabButton
              active={tab === "rejected"}
              onClick={() => setTab("rejected")}
            >
              Rejected
              {rejected.length > 0 && (
                <span className="ml-1.5 bg-danger px-1.5 pt-0.5 text-[11px] leading-tight tabular-nums text-white">
                  {rejected.length}
                </span>
              )}
            </TabButton>
          </TabBar>
        </div>

        {tab === "runs" &&
          (runs.length === 0 ? (
            <EmptyState
              title="No runs yet"
              hint="Import applicants, check capacity, then press Schedule! to produce the first timetable."
            />
          ) : (
            <ul className="card divide-y divide-rule overflow-hidden">
              {runs.map((run, i) => (
                <li key={run.run_id}>
                  <Link
                    href={runHref(run.run_id)}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-sm transition-colors hover:bg-purple-wash"
                  >
                    <span className="flex-1 font-medium text-ink">
                      {formatRunId(run.run_id)}
                    </span>
                    {i === 0 && <Badge tone="purple">latest</Badge>}
                    {!run.has_assignments && (
                      <Badge tone="pink">no assignments</Badge>
                    )}
                    <span className="font-mono text-xs text-ink-muted">
                      {run.run_id}
                    </span>
                    <span className="text-purple-vivid" aria-hidden="true">›</span>
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
            <h2 className="section-title">Schedule</h2>
            <span className="text-xs text-ink-muted">
              {formatRunId(schedule.runId)}
            </span>
            <Link
              href={runHref(schedule.runId)}
              className="ml-auto text-xs font-medium text-purple-vivid underline-offset-2 hover:underline"
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
          <p className="text-sm leading-relaxed text-ink-soft">{infeasible}</p>
          <p className="mt-3 text-xs text-ink-muted">
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
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="type-label border-b-2 border-purple-deep bg-paper-sunk text-[11px] text-purple-deep">
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
        <tbody className="divide-y divide-rule">
          {rows.map((row) => (
            <tr key={row.row_number}>
              <td className="px-4 py-2 font-mono text-xs tabular-nums text-ink-muted">
                {row.csv_row}
              </td>
              <td className="px-4 py-2 font-medium text-ink">
                {row.full_name || <span className="text-ink-muted">—</span>}
              </td>
              <td className="px-4 py-2 text-ink-soft">
                {row.email || <span className="text-ink-muted">—</span>}
              </td>
              <td className="px-4 py-2 text-ink-soft">
                {row.sub_division_1 || (
                  <span className="text-ink-muted">—</span>
                )}
              </td>
              <td className="px-4 py-2 text-ink-soft">
                {row.sub_division_2 || (
                  <span className="text-ink-muted">—</span>
                )}
              </td>
              <td className="px-4 py-2">
                <span
                  className="font-medium text-danger"
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
                    className="text-xs text-ink-muted"
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

