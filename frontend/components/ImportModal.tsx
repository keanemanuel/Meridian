"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { IngestResult } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** Ingest has two sources (FR-01), but the choice is implied by the
 * workspace, not offered as a toggle: a linked Google Sheet → incremental
 * Sheets read; no Sheet → one-shot CSV upload. `force` re-reads the Sheet
 * from row 1 instead of continuing from the watermark. */
export function ImportModal({
  workspaceId,
  hasSheet,
  onClose,
  onDone,
}: {
  workspaceId: string;
  hasSheet: boolean;
  onClose: () => void;
  onDone: (result: IngestResult) => void;
}) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!hasSheet && !file) return;
    setBusy(true);
    try {
      const result = hasSheet
        ? await api.ingestSheets(workspaceId, force)
        : await api.ingestCsv(workspaceId, file as File);
      onDone(result);
      onClose();
    } catch (err) {
      toast.fromError(err, "Import failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Import data" onClose={onClose}>
      <div className="space-y-4">
        {hasSheet ? (
          <>
            <p className="text-sm text-neutral-700">
              Importing from the workspace&rsquo;s linked Google Sheet.
            </p>
            <label className="flex items-start gap-2 text-sm text-neutral-700">
              <input
                type="checkbox"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                Force full re-read
                <span className="block text-xs text-neutral-500">
                  Ignores the watermark and re-reads every row from the top.
                </span>
              </span>
            </label>
          </>
        ) : (
          <div>
            <label
              htmlFor="csv-file"
              className="mb-1.5 block text-xs font-medium text-neutral-600"
            >
              Google Form CSV export
            </label>
            <input
              id="csv-file"
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-neutral-100 file:px-2 file:py-1 file:text-xs"
            />
            <p className="mt-2 text-xs text-neutral-500">
              Link a Google Sheet to enable live import.
            </p>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!hasSheet && !file}
            onClick={run}
          >
            Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}
