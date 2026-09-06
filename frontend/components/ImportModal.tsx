"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { IngestResult } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** Ingest has two sources (FR-01): a one-shot CSV upload, or an incremental
 * read of the workspace's linked Google Sheet. `force` re-reads the Sheet
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
  const [source, setSource] = useState<"csv" | "sheets">(
    hasSheet ? "sheets" : "csv",
  );
  const [file, setFile] = useState<File | null>(null);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (source === "csv" && !file) return;
    setBusy(true);
    try {
      const result =
        source === "csv"
          ? await api.ingestCsv(workspaceId, file as File)
          : await api.ingestSheets(workspaceId, force);
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
        <div className="flex gap-2">
          {(["csv", "sheets"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSource(s)}
              className={`flex-1 rounded-md border px-3 py-2 text-sm transition-colors ${
                source === s
                  ? "border-neutral-900 bg-neutral-900 text-white"
                  : "border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50"
              }`}
            >
              {s === "csv" ? "CSV upload" : "Google Sheet"}
            </button>
          ))}
        </div>

        {source === "csv" ? (
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
          </div>
        ) : (
          <div className="space-y-3">
            {!hasSheet && (
              <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                No Sheet is attached to this workspace yet — attach one with{" "}
                <code className="font-mono">iffsched workspace set-sheet</code>{" "}
                before importing from Sheets.
              </p>
            )}
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
          </div>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={source === "csv" && !file}
            onClick={run}
          >
            Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}
