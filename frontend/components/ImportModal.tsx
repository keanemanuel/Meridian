"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { IngestResult, WorkspaceMeta } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button } from "./ui";

type Source = "sheets" | "csv";

/** Ingest has two sources (FR-01) and both are offered here.
 *
 * Google Sheet reads the workspace's linked Sheet incrementally, continuing
 * from the watermark unless "force" is ticked. The URL field is pre-filled
 * with whatever is already linked and stays editable, so linking a Sheet and
 * importing from it are one step rather than two.
 */
export function ImportModal({
  workspaceId,
  sheetId,
  onClose,
  onDone,
  onSheetLinked,
}: {
  workspaceId: string;
  sheetId: string | null;
  onClose: () => void;
  onDone: (result: IngestResult) => void;
  onSheetLinked: (updated: WorkspaceMeta) => void;
}) {
  const toast = useToast();
  const [source, setSource] = useState<Source>(sheetId ? "sheets" : "csv");
  const [sheetUrl, setSheetUrl] = useState(
    sheetId ? `https://docs.google.com/spreadsheets/d/${sheetId}/edit` : "",
  );
  const [file, setFile] = useState<File | null>(null);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);

  const linkedUrl = sheetId
    ? `https://docs.google.com/spreadsheets/d/${sheetId}/edit`
    : null;
  const urlChanged = sheetUrl.trim() !== (linkedUrl ?? "");
  const canImport =
    source === "sheets" ? sheetUrl.trim().length > 0 : file !== null;

  const run = async () => {
    if (!canImport) return;
    setBusy(true);
    try {
      if (source === "sheets") {
        // Save an edited URL first, so importing from a Sheet that was never
        // linked (or was just changed) works without a separate save step.
        if (urlChanged) {
          onSheetLinked(
            await api.setWorkspaceSheet(workspaceId, sheetUrl.trim()),
          );
        }
        onDone(await api.ingestSheets(workspaceId, force));
      } else {
        onDone(await api.ingestCsv(workspaceId, file as File));
      }
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
        <SourceCard
          selected={source === "sheets"}
          onSelect={() => setSource("sheets")}
          title="Google Sheet"
          blurb={
            linkedUrl
              ? "Reads the Sheet linked to this workspace, picking up where the last import stopped."
              : "Paste the Sheet the form writes to. It gets linked to this workspace so later imports are one click."
          }
        >
          <label
            htmlFor="import-sheet-url"
            className="mb-1.5 block text-xs font-medium text-neutral-600"
          >
            Sheet URL
          </label>
          <input
            id="import-sheet-url"
            value={sheetUrl}
            onChange={(e) => setSheetUrl(e.target.value)}
            onFocus={() => setSource("sheets")}
            placeholder="https://docs.google.com/spreadsheets/d/…"
            className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
          {urlChanged && sheetUrl.trim() && (
            <p className="mt-1.5 text-xs text-neutral-500">
              This Sheet will be linked to the workspace when you import.
            </p>
          )}
          <label className="mt-3 flex items-start gap-2 text-sm text-neutral-700">
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
        </SourceCard>

        <SourceCard
          selected={source === "csv"}
          onSelect={() => setSource("csv")}
          title="CSV Upload"
          blurb="One-shot import of a Google Form CSV export. Nothing is linked to the workspace."
        >
          <input
            id="import-csv-file"
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setSource("csv");
            }}
            className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-neutral-100 file:px-2 file:py-1 file:text-xs"
          />
        </SourceCard>

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!canImport}
            onClick={run}
          >
            Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function SourceCard({
  selected,
  onSelect,
  title,
  blurb,
  children,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  blurb: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-lg border p-3 transition-colors ${
        selected
          ? "border-neutral-900 bg-white"
          : "border-neutral-200 bg-neutral-50"
      }`}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex w-full items-start gap-2.5 text-left"
      >
        <span
          className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
            selected ? "border-neutral-900" : "border-neutral-400"
          }`}
          aria-hidden="true"
        >
          {selected && (
            <span className="h-2 w-2 rounded-full bg-neutral-900" />
          )}
        </span>
        <span className="min-w-0">
          <span className="block text-sm font-medium text-neutral-900">
            {title}
          </span>
          <span className="block text-xs leading-relaxed text-neutral-500">
            {blurb}
          </span>
        </span>
      </button>
      <div className={`mt-3 ${selected ? "" : "opacity-50"}`}>{children}</div>
    </div>
  );
}
