"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  cellIndex,
  cellKey,
  formatDate,
  formatTime,
  panelAxis,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** Move one interview to a different panel/slot (FR-41).
 *
 * The API takes both a panel and a slot, so both are offered. Whatever is
 * chosen, the server still runs the whole schedule through `validate_edits`
 * and refuses anything illegal (E-12) — the hints here are a courtesy, not
 * the check.
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

  const panels = useMemo(() => panelAxis(assignments), [assignments]);
  const slots = useMemo(() => slotAxis(assignments), [assignments]);
  const cells = useMemo(() => cellIndex(assignments), [assignments]);

  const unchanged =
    panelId === assignment.panel_id && slotId === assignment.slot_id;

  const occupant = (cells.get(cellKey(panelId, slotId)) ?? []).find(
    (a) => a.assignment_id !== assignment.assignment_id,
  );

  /** The applicant's other interview — landing on its slot double-books them (C1). */
  const otherChoice = assignments.find(
    (a) =>
      a.applicant_id === assignment.applicant_id &&
      a.assignment_id !== assignment.assignment_id,
  );
  const selfClash = otherChoice?.slot_id === slotId;

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
      toast.success(
        `${assignment.full_name} moved to ${result.assignment.panel_id} · ` +
          `${formatTime(result.assignment.start_time)} and locked ` +
          `(${result.total_locks} lock${result.total_locks === 1 ? "" : "s"} total).`,
      );
      await onMoved();
      onClose();
    } catch (err) {
      toast.fromError(err, "The edit was rejected — nothing was saved.");
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
            Panel
          </label>
          <select
            id="move-panel"
            value={panelId}
            onChange={(e) => setPanelId(e.target.value)}
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm outline-none focus:border-neutral-500"
          >
            {panels.map((p) => (
              <option key={p.panel_id} value={p.panel_id}>
                {p.panel_id} — {p.room}
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
              return (
                <option key={s.slot_id} value={s.slot_id}>
                  {formatDate(s.date)} {formatTime(s.start_time)}–
                  {formatTime(s.end_time)}
                  {taken.length ? `  · busy (${taken[0].full_name})` : "  · free"}
                </option>
              );
            })}
          </select>
        </div>

        {occupant && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {occupant.full_name} already has that panel and slot. The edit
            validator will reject this (C2).
          </p>
        )}
        {selfClash && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            That is the same slot as this applicant&apos;s other interview — it
            would double-book them (C1).
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
            disabled={unchanged}
            onClick={submit}
          >
            Confirm move
          </Button>
        </div>
      </div>
    </Modal>
  );
}
