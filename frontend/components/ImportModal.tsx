"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { IngestResult } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

/** Ingest is a one-shot upload of a Google Form CSV export (FR-01). CSV
 * upload is the only supported input method — nothing is linked to the
 * workspace. */
export function ImportModal({
  workspaceId,
  onClose,
  onDone,
}: {
  workspaceId: string;
  onClose: () => void;
  onDone: (result: IngestResult) => void;
}) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (file === null) return;
    setBusy(true);
    try {
      onDone(await api.ingestCsv(workspaceId, file));
      onClose();
    } catch (err) {
      toast.fromError(err, "Import failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Import data" onClose={onClose} width="w-[32rem]">
      <div className="space-y-3">
        <p className="text-sm text-ink-muted">
          One-shot import of a Google Form CSV export. Download the form
          responses as CSV (<span className="whitespace-nowrap">File → Download → CSV</span>)
          and upload the file here.
        </p>
        <input
          id="import-csv-file"
          type="file"
          accept=".csv,text/csv"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="input w-full"
        />

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={file === null}
            onClick={run}
          >
            Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}
