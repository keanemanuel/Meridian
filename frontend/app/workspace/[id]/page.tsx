"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { ImportModal } from "@/components/ImportModal";
import { Modal } from "@/components/Modal";
import { useToast } from "@/components/Toast";
import { Badge, Button, EmptyState, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { formatRunId } from "@/lib/schedule";
import type { CapacityCheck, RunSummary, WorkspaceMeta } from "@/lib/types";

type Action = "import" | "check" | "solve" | "publish";

export default function WorkspacePage({
  params,
}: PageProps<"/workspace/[id]">) {
  const { id: rawId } = use(params);
  const workspaceId = decodeURIComponent(rawId);
  const toast = useToast();

  const [meta, setMeta] = useState<WorkspaceMeta | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [busy, setBusy] = useState<Action | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [capacity, setCapacity] = useState<CapacityCheck | null>(null);
  /** Set when solve came back 409 INFEASIBLE — the user may override (E-06). */
  const [infeasible, setInfeasible] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    const list = await api.listRuns(workspaceId);
    // Run ids are lexically sortable timestamps; newest first.
    setRuns([...list].sort((a, b) => b.run_id.localeCompare(a.run_id)));
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      // A capacity table belongs to the workspace it was run for.
      setCapacity(null);
      try {
        const [m] = await Promise.all([api.getWorkspace(workspaceId), loadRuns()]);
        if (!cancelled) {
          setMeta(m);
          setLoadError(null);
        }
      } catch (err) {
        if (!cancelled) setLoadError((err as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, loadRuns]);

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
    try {
      const result = await api.solve(workspaceId, skipCheck);
      toast.success(
        `Solved: ${result.interviews_placed}/${result.interviews_required} interviews placed, ` +
          `${result.clashes} clash(es), ${result.locked} locked — ${result.solve_seconds}s.`,
      );
      await loadRuns();
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

  const runPublish = async () => {
    setBusy("publish");
    try {
      const result = await api.publish(workspaceId, "latest");
      toast.success(
        `Published run ${result.run_id}: ${result.room_views} room view(s), ` +
          `${result.applicants} applicants, ${result.clashes_red} red clash(es) → ${result.output_dir}`,
      );
    } catch (err) {
      toast.fromError(err, "Publish failed.");
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

  return (
    <div className="mx-auto max-w-5xl px-8 py-8">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold text-neutral-900">{meta.name}</h1>
        <Badge>{meta.group}</Badge>
        {meta.sheet_id && <Badge tone="green">Sheet linked</Badge>}
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
          disabled={busy !== null}
        >
          Check Capacity
        </Button>
        <Button
          variant="primary"
          onClick={() => runSolve(false)}
          loading={busy === "solve"}
          disabled={busy !== null}
        >
          Solve
        </Button>
        <Button
          onClick={runPublish}
          loading={busy === "publish"}
          disabled={busy !== null}
        >
          Publish
        </Button>
      </div>

      {capacity && <CapacityTable check={capacity} />}

      <section className="mt-10">
        <h2 className="mb-3 text-sm font-semibold text-neutral-900">
          Run history
        </h2>
        {runs.length === 0 ? (
          <EmptyState
            title="No runs yet"
            hint="Import applicants, check capacity, then Solve to produce the first timetable."
          />
        ) : (
          <ul className="divide-y divide-neutral-200 overflow-hidden rounded-lg border border-neutral-200 bg-white">
            {runs.map((run, i) => (
              <li key={run.run_id}>
                <Link
                  href={`/workspace/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(run.run_id)}`}
                  className="flex items-center gap-3 px-4 py-3 text-sm transition-colors hover:bg-neutral-50"
                >
                  <span className="flex-1 font-medium text-neutral-800">
                    {formatRunId(run.run_id)}
                  </span>
                  {i === 0 && <Badge tone="green">latest</Badge>}
                  {!run.has_assignments && <Badge tone="amber">no assignments</Badge>}
                  <span className="font-mono text-xs text-neutral-400">
                    {run.run_id}
                  </span>
                  <span className="text-neutral-300">›</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      {showImport && (
        <ImportModal
          workspaceId={workspaceId}
          hasSheet={Boolean(meta.sheet_id)}
          onClose={() => setShowImport(false)}
          onDone={(result) =>
            toast.success(
              `Imported ${result.applicants} applicant(s) — ${result.rejected} rejected, ` +
                `${result.collapsed} collapsed, ${result.warnings} warning(s).`,
            )
          }
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
