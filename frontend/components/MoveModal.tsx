"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  cellIndex,
  cellKey,
  checkMove,
  divisionPanels,
  formatDate,
  formatTime,
  moveBlocked,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** Move one interview to a different panel/slot (FR-40, FR-41).
 *
 * Only panels serving the interview's own division are offered — a
 * cross-division move is rejected outright by the edit validator
 * (DIVISION_MISMATCH), so it is never a choice here. Any blank slot on those
 * panels is fair game, whether or not it matches the applicant's stated
 * availability: an off-preference target is flagged as a clash but still
 * allowed (FR-34), the recruiter's call. The two moves the server will always
 * refuse — landing on an occupied panel/slot (C2) or on the slot the
 * applicant's other interview already holds (C3) — are disabled here rather
 * than left to bounce back as an error.
 */
export function MoveModal({
  workspaceId,
  runId,
  assignment,
  assignments,
  onClose,
  onMoved,
}: {
  workspaceId: string;
  runId: string;
  assignment: Assignment;
  assignments: Assignment[];
  onClose: () => void;
  onMoved: () => void | Promise<void>;
}) {
  const toast = useToast();
  const [panelId, setPanelId] = useState(assignment.panel_id);
  const [slotId, setSlotId] = useState(assignment.slot_id);
  const [saving, setSaving] = useState(false);

  /** Panels for this interview's division only (FR-40). The interview's current
   * panel always serves its own division, so it is guaranteed to be in here. */
  const panels = useMemo(
    () => divisionPanels(assignments, assignment.division),
    [assignments, assignment.division],
  );
  const slots = useMemo(() => slotAxis(assignments), [assignments]);
  const cells = useMemo(() => cellIndex(assignments), [assignments]);

  const check = checkMove(assignments, assignment, panelId, slotId);
  const blocked = moveBlocked(check);

  const submit = async () => {
    setSaving(true);
    try {
      const result = await api.patchAssignment(
        workspaceId,
        runId,
        assignment.assignment_id,
        panelId,
        slotId,
      );
      const clashNote = result.assignment.is_clash
        ? " Flagged as a clash — outside their stated availability."
        : "";
      toast.success(
        `${assignment.full_name} moved to ${result.assignment.panel_id} · ` +
          `${formatTime(result.assignment.start_time)} and locked ` +
          `(${result.total_locks} lock${result.total_locks === 1 ? "" : "s"} total).` +
          clashNote,
      );
      await onMoved();
      onClose();
    } catch (err) {
      toast.fromError(err, "The edit was rejected. Nothing was saved.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`Move ${assignment.full_name} to a different slot`}
      onClose={onClose}
      width="w-[32rem]"
    >
      <div className="space-y-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2.5 text-sm">
          <dt className="text-neutral-500">Choice</dt>
          <dd className="text-neutral-800">
            #{assignment.choice_index} · {assignment.sub_division}
          </dd>
          <dt className="text-neutral-500">Division</dt>
          <dd className="text-neutral-800">{assignment.division}</dd>
          <dt className="text-neutral-500">Currently</dt>
          <dd className="text-neutral-800">
            {assignment.panel_id} · {assignment.room} ·{" "}
            {formatDate(assignment.date)} {formatTime(assignment.start_time)}
          </dd>
        </dl>

        <div>
          <label
            htmlFor="move-panel"
            className="mb-1.5 block text-xs font-medium text-neutral-600"
          >
            Panel · {assignment.division} only
          </label>
          <select
            id="move-panel"
            value={panelId}
            onChange={(e) => setPanelId(e.target.value)}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm outline-none focus:border-neutral-500"
          >
            {panels.map((p) => (
              <option key={p.panel_id} value={p.panel_id}>
                {p.panel_id} · Room {p.room}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="move-slot"
            className="mb-1.5 block text-xs font-medium text-neutral-600"
          >
            Slot
          </label>
          <select
            id="move-slot"
            value={slotId}
            onChange={(e) => setSlotId(e.target.value)}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm outline-none focus:border-neutral-500"
          >
            {slots.map((s) => {
              const taken = (cells.get(cellKey(panelId, s.slot_id)) ?? []).filter(
                (a) => a.assignment_id !== assignment.assignment_id,
              );
              const here = checkMove(
                assignments,
                assignment,
                panelId,
                s.slot_id,
              );
              const tag = taken.length
                ? `busy (${taken[0].full_name})`
                : here.doubleBook
                  ? "their other interview"
                  : here.offPreference
                    ? "free · clash"
                    : "free";
              return (
                <option key={s.slot_id} value={s.slot_id}>
                  {formatDate(s.date)} {formatTime(s.start_time)}–
                  {formatTime(s.end_time)}
                  {"  · "}
                  {tag}
                </option>
              );
            })}
          </select>
        </div>

        {check.occupied && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {check.occupied.full_name} already holds that panel and slot — a
            panel can only run one interview at a time (C2). Pick a free slot.
          </p>
        )}
        {check.doubleBook && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            That is the same slot as {assignment.full_name}&apos;s other
            interview, so it would double-book them (C3). Pick another slot.
          </p>
        )}
        {check.offPreference && !blocked && (
          <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            This slot is outside {assignment.full_name}&apos;s stated
            availability. The move is allowed and will be recorded as a clash
            (FR-34) — your call.
          </p>
        )}

        <p className="text-xs text-neutral-500">
          A saved move is locked, so every later solve keeps it in place (C6).
        </p>

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={saving}
            disabled={!check.changed || blocked}
            onClick={submit}
          >
            {check.offPreference && !blocked ? "Move anyway" : "Confirm move"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
