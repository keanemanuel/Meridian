"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, pendingCountFromError } from "@/lib/api";
import type { InvitePreview, ResultPreview } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button, Spinner } from "./ui";

type Kind = "invite" | "result";

type Preview =
  | { kind: "invite"; data: InvitePreview }
  | { kind: "result"; data: ResultPreview };

/** Preview-then-send, mirroring the CLI's `--dry-run` / `--send` pair.
 *
 * The send endpoints require a typed recipient count that must match the
 * ledger-filtered pending list exactly (FR-62, FR-64). The preview cannot
 * know how many the ledger has already excluded, so a mismatch is expected
 * on a re-send: the server reports the real number and this re-confirms
 * against it rather than sending anything unintended.
 */
export function SendModal({
  kind,
  workspaceId,
  runId,
  onClose,
}: {
  kind: Kind;
  workspaceId: string;
  runId: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [verifiedBy, setVerifiedBy] = useState("");
  /** Set when the server told us the real pending count differs. */
  const [correctedCount, setCorrectedCount] = useState<number | null>(null);

  const title = kind === "invite" ? "Send invites" : "Send results";

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data =
        kind === "invite"
          ? await api.invitePreview(workspaceId, runId)
          : await api.resultPreview(workspaceId, runId);
      setPreview(
        kind === "invite"
          ? { kind: "invite", data: data as InvitePreview }
          : { kind: "result", data: data as ResultPreview },
      );
    } catch (err) {
      toast.fromError(err, "Preview failed — nothing was sent.");
      onClose();
    } finally {
      setLoading(false);
    }
    // `toast` and `onClose` are stable enough for a one-shot load on open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, workspaceId, runId]);

  useEffect(() => {
    void (async () => {
      await load();
    })();
  }, [load]);

  const previewCount =
    preview?.kind === "invite"
      ? preview.data.auto_sendable
      : preview
        ? Object.values(preview.data.counts).reduce((a, b) => a + b, 0)
        : 0;

  const count = correctedCount ?? previewCount;

  const send = async () => {
    setSending(true);
    try {
      const res =
        kind === "invite"
          ? await api.inviteSend(workspaceId, runId, count)
          : await api.resultSend(workspaceId, runId, count, verifiedBy.trim());

      if (res.message) {
        toast.info(res.message);
      } else {
        const failed = "failed_total" in res ? (res.failed_total ?? 0) : 0;
        toast.success(
          `Attempted ${res.attempted}; ledger now records ${res.sent_total} sent` +
            (failed ? `, ${failed} failed.` : "."),
        );
      }
      onClose();
    } catch (err) {
      // The ledger had already excluded some recipients — re-confirm on the
      // number the server reported instead of failing the user outright.
      const pending =
        err instanceof ApiError ? pendingCountFromError(err) : null;
      if (pending !== null) {
        setCorrectedCount(pending);
        toast.info(
          `${pending} recipient(s) are actually pending — the rest are already in the ledger. Confirm again to send those ${pending}.`,
        );
      } else {
        toast.fromError(err, "Send failed.");
      }
    } finally {
      setSending(false);
    }
  };

  const blocked = kind === "result" && !verifiedBy.trim();

  return (
    <Modal title={title} onClose={onClose} width="w-[32rem]">
      {loading ? (
        <p className="flex items-center gap-2 py-4 text-sm text-neutral-500">
          <Spinner /> Rendering preview…
        </p>
      ) : !preview ? null : (
        <div className="space-y-4">
          {preview.kind === "invite" ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2.5 text-sm">
              <dt className="text-neutral-500">Recipients</dt>
              <dd className="text-neutral-800">{preview.data.total}</dd>
              <dt className="text-neutral-500">Auto-sendable</dt>
              <dd className="font-medium text-neutral-900">
                {preview.data.auto_sendable}
              </dd>
              <dt className="text-neutral-500">Held for manual review</dt>
              <dd className="text-neutral-800">
                {preview.data.held_for_manual}
              </dd>
            </dl>
          ) : (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2.5 text-sm">
              {Object.entries(preview.data.counts).map(([decision, n]) => (
                <div key={decision} className="contents">
                  <dt className="text-neutral-500">{decision}</dt>
                  <dd className="text-neutral-800">{n}</dd>
                </div>
              ))}
            </dl>
          )}

          {preview.kind === "invite" && preview.data.held_for_manual > 0 && (
            <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {preview.data.held_for_manual} invite(s) involve a clash and are
              held back for manual sending. They were written to{" "}
              <code className="font-mono">
                {preview.data.emails_dir}/manual_review
              </code>
              .
            </p>
          )}

          {preview.data.samples.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-medium text-neutral-600">
                Sample subjects
              </p>
              <ul className="space-y-1 rounded-md border border-neutral-200 px-3 py-2 text-xs text-neutral-600">
                {preview.data.samples.map((s) => (
                  <li key={s.applicant_id} className="truncate">
                    <span className="text-neutral-400">{s.to_email}</span> —{" "}
                    {s.subject}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {kind === "result" && (
            <div>
              <label
                htmlFor="verified-by"
                className="mb-1.5 block text-xs font-medium text-neutral-600"
              >
                Verified by (required)
              </label>
              <input
                id="verified-by"
                value={verifiedBy}
                onChange={(e) => setVerifiedBy(e.target.value)}
                placeholder="Your name"
                className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-500"
              />
              <p className="mt-1 text-xs text-neutral-500">
                Recorded in the verification log alongside the send.
              </p>
            </div>
          )}

          <p className="rounded border border-neutral-200 bg-white px-3 py-2 text-sm text-neutral-700">
            This will send <span className="font-semibold">{count}</span> real
            email{count === 1 ? "" : "s"}
            {correctedCount !== null && " (corrected against the ledger)"}. Every
            send is recorded, so no one is emailed twice.
          </p>

          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={sending}
              disabled={blocked || count === 0}
              onClick={send}
            >
              Send {count}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
