"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { ApplicantsView } from "@/components/ApplicantsView";
import { CountView } from "@/components/CountView";
import { Modal } from "@/components/Modal";
import { MoveModal } from "@/components/MoveModal";
import { PanelsView } from "@/components/PanelsView";
import { RoomView, type MoveRequest } from "@/components/RoomView";
import { RoomsView } from "@/components/RoomsView";
import { useToast } from "@/components/Toast";
import { Badge, Button, Spinner, TabBar, TabButton } from "@/components/ui";
import { ApiError, api, saveBlob } from "@/lib/api";
import { formatRunId, formatTime, interviewBreakdown } from "@/lib/schedule";
import type { Assignment, RoomPanel } from "@/lib/types";

const TABS = [
  { id: "room", label: "View" },
  { id: "applicants", label: "Applicants" },
  { id: "panels", label: "Panels" },
  { id: "rooms", label: "Rooms" },
  { id: "count", label: "Count" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** One entry on the undo stack: where a dragged interview came from. */
type MoveHistoryEntry = {
  assignmentId: string;
  fullName: string;
  panelId: string;
  slotId: string;
};

/** How many manual moves can be stepped back through. Kept in this page's
 * state only: reloading the run reads the saved schedule, which is the real
 * record of what happened. */
const UNDO_LIMIT = 10;

export default function RunPage({
  params,
}: PageProps<"/workspace/[id]/runs/[runId]">) {
  const { id: rawId, runId: rawRunId } = use(params);
  const workspaceId = decodeURIComponent(rawId);
  const runId = decodeURIComponent(rawRunId);

  const toast = useToast();
  const router = useRouter();

  const [tab, setTab] = useState<TabId>("room");
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [panels, setPanels] = useState<RoomPanel[]>([]);
  const [divisions, setDivisions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selected, setSelected] = useState<Assignment | null>(null);
  const [resolving, setResolving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [moving, setMoving] = useState(false);
  const [history, setHistory] = useState<MoveHistoryEntry[]>([]);
  const [infeasible, setInfeasible] = useState<string | null>(null);
  const [showBreakdown, setShowBreakdown] = useState(false);

  const load = useCallback(async () => {
    try {
      setAssignments(await api.getAssignments(workspaceId, runId));
      setLoadError(null);
      try {
        const p = await api.listPanels(workspaceId, runId);
        setPanels(p.panels);
        setDivisions(p.divisions);
      } catch {
        // The panel list is a Rooms-tab nicety — a failure here must not
        // blank the whole run page.
      }
    } catch (err) {
      setLoadError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [workspaceId, runId]);

  const addPanel = useCallback(
    async (division: string, room: string) => {
      try {
        const res = await api.createPanel(workspaceId, runId, division, room);
        setPanels(res.panels);
        toast.success(
          `Added ${res.panel.panel_id} to room ${room}. It is empty — drag interviews in.`,
        );
      } catch (err) {
        toast.fromError(err, "That panel could not be added.");
      }
    },
    [workspaceId, runId, toast],
  );

  const removePanel = useCallback(
    async (panelId: string) => {
      try {
        const res = await api.deletePanel(workspaceId, runId, panelId);
        setPanels(res.panels);
        toast.success(`Removed panel ${panelId}.`);
      } catch (err) {
        toast.fromError(err, "That panel could not be removed.");
      }
    },
    [workspaceId, runId, toast],
  );

  /** Drag a panel's division badge from one room card onto another: the panel
   * (with every interview on it) moves to the new room. Refresh assignments
   * too so Room View / Applicants reflect the new room without a reload. */
  const movePanel = useCallback(
    async (panelId: string, room: string) => {
      try {
        const res = await api.movePanel(workspaceId, runId, panelId, room);
        setPanels(res.panels);
        await load();
        toast.success(
          `Moved ${panelId} to room ${room}` +
            (res.moved_interviews > 0
              ? ` with its ${res.moved_interviews} interview${res.moved_interviews === 1 ? "" : "s"}.`
              : "."),
        );
      } catch (err) {
        toast.fromError(err, "That panel could not be moved.");
      }
    },
    [workspaceId, runId, toast, load],
  );

  useEffect(() => {
    void (async () => {
      setLoading(true);
      await load();
    })();
  }, [load]);

  /** Apply a drag-and-drop move. `remember` is false for an undo, so undoing
   * does not itself become another undoable step. */
  const applyMove = useCallback(
    async ({ assignment, panelId, slotId }: MoveRequest, remember = true) => {
      setMoving(true);
      try {
        const result = await api.patchAssignment(
          workspaceId,
          runId,
          assignment.assignment_id,
          panelId,
          slotId,
        );
        if (remember) {
          setHistory((prev) =>
            [
              ...prev,
              {
                assignmentId: assignment.assignment_id,
                fullName: assignment.full_name,
                panelId: assignment.panel_id,
                slotId: assignment.slot_id,
              },
            ].slice(-UNDO_LIMIT),
          );
        }
        toast.success(
          `${assignment.full_name} moved to ${result.assignment.panel_id} at ` +
            `${formatTime(result.assignment.start_time)} and locked.` +
            (result.assignment.is_clash
              ? " Flagged as a clash — outside their stated availability."
              : ""),
        );
        await load();
      } catch (err) {
        toast.fromError(err, "That move was rejected. Nothing was saved.");
      } finally {
        setMoving(false);
      }
    },
    [workspaceId, runId, toast, load],
  );

  /** Lock or unlock one interview in place. Persists immediately — same as a
   * drag — then reloads so the 🔒 count and the Re-solve summary track it. */
  const toggleLock = useCallback(
    async (assignment: Assignment) => {
      const next = !assignment.is_locked;
      setMoving(true);
      try {
        await api.setAssignmentLock(
          workspaceId,
          runId,
          assignment.assignment_id,
          next,
        );
        toast.success(
          next
            ? `${assignment.full_name}'s interview is locked — every re-solve keeps it.`
            : `${assignment.full_name}'s interview is unlocked — a re-solve may move it.`,
        );
        await load();
      } catch (err) {
        toast.fromError(err, "The lock could not be changed. Nothing was saved.");
      } finally {
        setMoving(false);
      }
    },
    [workspaceId, runId, toast, load],
  );

  const undoLastMove = useCallback(async () => {
    const last = history[history.length - 1];
    if (!last) return;
    const target = assignments.find(
      (a) => a.assignment_id === last.assignmentId,
    );
    if (!target) {
      toast.error("That interview is no longer in this run, so it cannot be moved back.");
      setHistory((prev) => prev.slice(0, -1));
      return;
    }
    setHistory((prev) => prev.slice(0, -1));
    await applyMove(
      { assignment: target, panelId: last.panelId, slotId: last.slotId },
      false,
    );
  }, [history, assignments, applyMove, toast]);

  const downloadXlsx = async () => {
    setDownloading(true);
    try {
      const { blob, filename } = await api.downloadXlsx(workspaceId, runId);
      saveBlob(blob, filename);
    } catch (err) {
      toast.fromError(err, "Download failed.");
    } finally {
      setDownloading(false);
    }
  };

  const reSolve = async (skipCheck = false) => {
    setResolving(true);
    setInfeasible(null);
    try {
      const result = await api.resolve(workspaceId, runId, skipCheck);
      toast.success(
        `Re-solved into run ${result.run_id}: ${result.interviews_placed} placed, ` +
          `${result.clashes} clash(es), ${result.locked} lock(s) honoured, ` +
          `${result.changed_vs_previous} changed.`,
      );
      // The re-solve wrote a new run, so go and look at that one.
      router.push(
        `/workspace/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(result.run_id)}`,
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setInfeasible(err.message);
      } else {
        toast.fromError(err, "Re-solve failed.");
      }
    } finally {
      setResolving(false);
    }
  };

  const clashes = assignments.filter((a) => a.is_clash).length;
  const locks = assignments.filter((a) => a.is_locked).length;
  const breakdown = useMemo(
    () => interviewBreakdown(assignments),
    [assignments],
  );
  const busy = resolving || moving || downloading;

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
        <Link
          href={`/workspace/${encodeURIComponent(workspaceId)}`}
          className="eyebrow underline-offset-2 hover:underline"
        >
          ‹ {workspaceId}
        </Link>

        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="display text-2xl sm:text-3xl">
            {formatRunId(runId)}
          </h1>
          <button
            type="button"
            onClick={() => setShowBreakdown(true)}
            disabled={assignments.length === 0}
            title="Show how these interviews split across applicants"
            className="transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Badge>{assignments.length} interviews ▾</Badge>
          </button>
          {clashes > 0 && <Badge tone="danger">{clashes} clash</Badge>}
          {locks > 0 && <Badge tone="pink">{locks} 🔒 locked</Badge>}

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button
              onClick={undoLastMove}
              disabled={history.length === 0 || busy}
              title={
                history.length === 0
                  ? "No manual moves to undo yet"
                  : `Move ${history[history.length - 1].fullName} back`
              }
            >
              Undo{history.length > 0 ? ` (${history.length})` : ""}
            </Button>
            <Button
              onClick={downloadXlsx}
              loading={downloading}
              disabled={busy || assignments.length === 0}
              title="Download the schedule, rooms and applicants spreadsheets (one ZIP)"
            >
              Download XLSX
            </Button>
          </div>
        </div>

        <div className="mt-6">
          <TabBar>
            {TABS.map((t) => (
              <TabButton
                key={t.id}
                active={tab === t.id}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </TabButton>
            ))}
          </TabBar>
        </div>

        <div className="mt-5">
          {loading ? (
            <p className="flex items-center gap-2 text-sm text-ink-muted">
              <Spinner /> Loading assignments…
            </p>
          ) : loadError ? (
            <div className="border-2 border-danger bg-danger-wash px-4 py-3 text-sm text-danger">
              {loadError}
            </div>
          ) : (
            <>
              {tab === "room" && (
                <>
                  <RoomView
                    assignments={assignments}
                    onSelect={setSelected}
                    onMove={applyMove}
                    onToggleLock={toggleLock}
                    moving={moving}
                  />
                  <p className="mt-2 text-xs text-ink-muted">
                    Drag an interview onto a blue slot to move it, or a pink
                    slot to move it outside the applicant&apos;s stated
                    availability (recorded as a clash). Only blank slots on the
                    applicant&apos;s own division show as targets. Click an
                    interview instead to pick a panel and slot by hand. Either
                    way the move is locked, so every later solve keeps it (C6).
                    Use the 🔒 / 🔓 toggle in an interview&apos;s corner to lock
                    or unlock it by hand without moving it.
                  </p>
                </>
              )}
              {tab === "applicants" && (
                <ApplicantsView assignments={assignments} />
              )}
              {tab === "panels" && <PanelsView assignments={assignments} />}
              {tab === "count" && <CountView assignments={assignments} />}
              {tab === "rooms" && (
                <RoomsView
                  assignments={assignments}
                  panels={panels}
                  divisions={divisions}
                  onSelect={setSelected}
                  onAddPanel={addPanel}
                  onDeletePanel={removePanel}
                  onMovePanel={movePanel}
                />
              )}
            </>
          )}
        </div>
      </div>

      {resolving && (
        <div className="flex shrink-0 items-center gap-2 border-t-2 border-accent-blue bg-accent-blue-wash px-5 py-3 sm:px-8 text-sm text-accent-blue">
          <Spinner />
          Solving schedule, this can take up to 2 minutes. Keep this tab open.
        </div>
      )}

      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-t-2 border-purple-deep bg-paper-raised px-5 py-3 sm:px-8">
        <Button onClick={() => reSolve(false)} loading={resolving} disabled={busy}>
          Re-solve
        </Button>
        <span className="mr-auto text-xs text-ink-muted">
          Keeps all {locks} lock{locks === 1 ? "" : "s"} and re-optimises the rest.
        </span>
      </div>

      {selected && (
        <MoveModal
          workspaceId={workspaceId}
          runId={runId}
          assignment={selected}
          assignments={assignments}
          onClose={() => setSelected(null)}
          onMoved={load}
        />
      )}

      {showBreakdown && (
        <Modal
          title="Interview breakdown"
          onClose={() => setShowBreakdown(false)}
        >
          <p className="text-sm text-ink-soft">
            <span className="font-semibold tabular-nums">{breakdown.total}</span>{" "}
            interviews across{" "}
            <span className="font-semibold tabular-nums">
              {breakdown.scheduled}
            </span>{" "}
            scheduled applicant{breakdown.scheduled === 1 ? "" : "s"}.
          </p>
          <ul className="mt-3 space-y-1.5 text-sm text-ink-soft">
            <li className="flex items-baseline justify-between gap-4">
              <span>Applicants with 2 interviews</span>
              <span className="font-semibold tabular-nums">
                {breakdown.withTwo}
              </span>
            </li>
            <li className="flex items-baseline justify-between gap-4">
              <span>Applicants with 1 interview</span>
              <span
                className={`font-semibold tabular-nums ${
                  breakdown.withOne > 0 ? "text-accent-pink-ink" : ""
                }`}
              >
                {breakdown.withOne}
              </span>
            </li>
            {breakdown.other.map((o) => (
              <li
                key={o.applicantId}
                className="flex items-baseline justify-between gap-4 text-danger"
              >
                <span>{o.fullName} has an unexpected count</span>
                <span className="font-semibold tabular-nums">{o.count}</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 border-t border-rule pt-2 text-xs text-ink-muted tabular-nums">
            {breakdown.withTwo} × 2 + {breakdown.withOne}
            {breakdown.other.length > 0
              ? ` + ${breakdown.other.reduce((n, o) => n + o.count, 0)}`
              : ""}{" "}
            = {breakdown.total}
          </p>
          <div className="mt-4 flex justify-end">
            <Button onClick={() => setShowBreakdown(false)}>Close</Button>
          </div>
        </Modal>
      )}

      {infeasible && (
        <Modal
          title="Capacity Advisor says INFEASIBLE"
          onClose={() => setInfeasible(null)}
        >
          <p className="text-sm leading-relaxed text-ink-soft">{infeasible}</p>
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setInfeasible(null)}>Cancel</Button>
            <Button variant="danger" onClick={() => void reSolve(true)}>
              Re-solve anyway
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
