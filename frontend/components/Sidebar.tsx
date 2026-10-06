"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { WorkspaceMeta } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button, Spinner } from "./ui";
import { useWorkspaces } from "./WorkspacesProvider";

function NewWorkspaceModal({
  group,
  onClose,
}: {
  group: string;
  onClose: () => void;
}) {
  const { create, connecting } = useWorkspaces();
  const toast = useToast();
  const router = useRouter();
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setSaving(true);
    try {
      const created = await create(trimmed, group);
      toast.success(`Workspace "${created.name}" created in ${group}.`);
      onClose();
      router.push(`/workspace/${encodeURIComponent(created.name)}`);
    } catch (err) {
      toast.fromError(err, "Could not create the workspace.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="New workspace" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label
            htmlFor="workspace-name"
            className="field-label"
          >
            Name
          </label>
          <input
            id="workspace-name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="IFF 2026 Intake"
            className="input w-full"
          />
        </div>
        <div>
          <p className="field-label">Group</p>
          <p className="border-2 border-rule bg-paper-sunk px-3 py-1.5 text-sm text-ink-soft">
            {group}
          </p>
        </div>
        {saving && connecting && (
          <p className="text-xs text-ink-muted">
            Connecting to backend. The API may be waking up, which can take up
            to 30 seconds.
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={saving}
            disabled={!name.trim()}
          >
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Rename dialog. The workspace name is the id every run, ledger and data
 * path is keyed on, so a rename moves the data directory with it (the API
 * handles that); a page still pointed at the old name is redirected below. */
function RenameWorkspaceModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceMeta;
  onClose: () => void;
}) {
  const { rename } = useWorkspaces();
  const toast = useToast();
  const router = useRouter();
  const params = useParams<{ id?: string }>();

  const [name, setName] = useState(workspace.name);
  const [saving, setSaving] = useState(false);

  const trimmed = name.trim();
  const viewingThis =
    params?.id !== undefined && decodeURIComponent(params.id) === workspace.name;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!trimmed || trimmed === workspace.name) return;
    setSaving(true);
    try {
      const updated = await rename(workspace.name, trimmed);
      toast.success(`Renamed to "${updated.name}".`);
      onClose();
      // The name is the id, so a page still pointed at the old one would 404.
      if (viewingThis) {
        router.replace(`/workspace/${encodeURIComponent(updated.name)}`);
      }
    } catch (err) {
      toast.fromError(err, "Could not rename the workspace.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title={`Rename "${workspace.name}"`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label
            htmlFor="rename-workspace"
            className="field-label"
          >
            New name
          </label>
          <input
            id="rename-workspace"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="input w-full"
          />
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={saving}
            disabled={!trimmed || trimmed === workspace.name}
          >
            Rename
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteWorkspaceModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceMeta;
  onClose: () => void;
}) {
  const { remove } = useWorkspaces();
  const toast = useToast();
  const router = useRouter();
  const params = useParams<{ id?: string }>();
  const [deleting, setDeleting] = useState(false);

  const viewingThis =
    params?.id !== undefined && decodeURIComponent(params.id) === workspace.name;

  const confirm = async () => {
    setDeleting(true);
    try {
      await remove(workspace.name);
      toast.success(`Deleted "${workspace.name}".`);
      onClose();
      if (viewingThis) router.replace("/");
    } catch (err) {
      toast.fromError(err, "Could not delete the workspace.");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Modal title={`Delete "${workspace.name}"?`} onClose={onClose}>
      <p className="text-sm leading-relaxed text-ink-soft">
        Are you sure? This removes the workspace and everything under it:
        imported applicants, every solve and the send ledger. It cannot be
        undone.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="danger" loading={deleting} onClick={confirm}>
          Delete workspace
        </Button>
      </div>
    </Modal>
  );
}

/** The "…" menu on a workspace row: rename or delete, the same for every
 * workspace. */
function RowMenu({
  workspace,
  onRename,
  onDelete,
}: {
  workspace: WorkspaceMeta;
  onRename: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-label={`Actions for ${workspace.name}`}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="px-1.5 py-0.5 text-ink-muted opacity-0 transition-opacity hover:bg-purple-tint hover:text-ink focus:opacity-100 group-hover:opacity-100 max-md:opacity-100"
      >
        ⋯
      </button>
      {open && (
        <div className="card absolute right-0 top-6 z-30 w-44 overflow-hidden py-1 shadow-hard-sm">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onRename();
            }}
            className="block w-full px-3 py-1.5 text-left text-sm text-ink-soft hover:bg-purple-wash"
          >
            Rename
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
            className="block w-full px-3 py-1.5 text-left text-sm text-danger hover:bg-danger-wash"
          >
            Delete
          </button>
        </div>
      )}
    </div>
  );
}

function Section({
  group,
  onNew,
  onRename,
  onDelete,
}: {
  group: string;
  onNew: (group: string) => void;
  onRename: (w: WorkspaceMeta) => void;
  onDelete: (w: WorkspaceMeta) => void;
}) {
  const { workspaces } = useWorkspaces();
  const params = useParams<{ id?: string }>();
  const [open, setOpen] = useState(true);

  const activeId = params?.id ? decodeURIComponent(params.id) : null;
  const items = workspaces
    .filter((w) => w.group === group)
    .sort((a, b) => a.name.localeCompare(b.name));
  // Styling only: the group's bar fills when it holds the open workspace.
  const holdsActive = items.some((w) => w.name === activeId);

  return (
    <div className="mb-5 px-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`type-label flex w-full items-center gap-1.5 border-b-4 pb-1.5 pt-1 text-left text-xs transition-colors hover:text-ink ${
          holdsActive
            ? "border-purple-vivid text-ink"
            : "border-accent-grey text-ink-muted"
        }`}
      >
        <span
          className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden="true"
        >
          ›
        </span>
        <span className="flex-1">{group}</span>
        <span className="text-ink-muted">
          {items.length}
        </span>
      </button>

      {open && (
        <div className="mt-2">
          {items.length === 0 && (
            <p className="px-3 py-1.5 text-xs italic text-ink-muted">
              No workspaces yet
            </p>
          )}
          {items.map((w) => {
            const active = w.name === activeId;
            return (
              <div
                key={w.name}
                className={`group flex items-center gap-1 border-l-4 pr-1.5 transition-colors ${
                  active
                    ? "border-purple-vivid bg-purple-wash font-medium text-ink"
                    : "border-transparent text-ink-soft hover:bg-purple-wash"
                }`}
              >
                <Link
                  href={`/workspace/${encodeURIComponent(w.name)}`}
                  title={w.name}
                  className="min-w-0 flex-1 truncate px-2 py-1.5 text-sm"
                >
                  {w.name}
                </Link>
                <RowMenu
                  workspace={w}
                  onRename={() => onRename(w)}
                  onDelete={() => onDelete(w)}
                />
              </div>
            );
          })}
          <button
            type="button"
            onClick={() => onNew(group)}
            className="type-label mt-1 w-full px-3 pb-1 pt-1.5 text-left text-[11px] text-purple-vivid hover:bg-purple-wash"
          >
            + New
          </button>
        </div>
      )}
    </div>
  );
}

export function Sidebar() {
  const { groups, loading, connecting, error } = useWorkspaces();
  const [newIn, setNewIn] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<WorkspaceMeta | null>(null);
  const [deleting, setDeleting] = useState<WorkspaceMeta | null>(null);
  // Let the user dismiss a stale connection error; a *new* error re-shows
  // because the dismissed text no longer matches.
  const [dismissedError, setDismissedError] = useState<string | null>(null);

  return (
    <aside className="flex max-h-48 w-full shrink-0 flex-col border-b-2 border-purple-deep bg-paper-sunk/40 md:max-h-none md:w-60 md:border-b-0 md:border-r-2">
      <nav className="flex-1 overflow-y-auto py-4">
        {loading && (
          <p className="flex items-center gap-2 px-3 py-2 text-xs text-ink-muted">
            <Spinner />{" "}
            {connecting ? "Connecting to backend…" : "Loading workspaces…"}
          </p>
        )}

        {connecting && (
          <p className="mx-3 mt-1 text-xs text-ink-muted">
            The scheduler API is waking up. This can take up to half a minute
            on the first request.
          </p>
        )}

        {error && !loading && error !== dismissedError && (
          <div className="mx-3 mb-3 flex items-start gap-2 border-2 border-danger bg-danger-wash px-3 py-2 text-xs text-danger">
            <p className="flex-1 leading-snug">{error}</p>
            <button
              type="button"
              onClick={() => setDismissedError(error)}
              aria-label="Dismiss error"
              className="-m-1 shrink-0 p-1 leading-none opacity-70 transition-opacity hover:opacity-100"
            >
              &times;
            </button>
          </div>
        )}

        {!loading &&
          groups.map((group) => (
            <Section
              key={group}
              group={group}
              onNew={setNewIn}
              onRename={setRenaming}
              onDelete={setDeleting}
            />
          ))}
      </nav>

      {newIn && (
        <NewWorkspaceModal group={newIn} onClose={() => setNewIn(null)} />
      )}
      {renaming && (
        <RenameWorkspaceModal
          workspace={renaming}
          onClose={() => setRenaming(null)}
        />
      )}
      {deleting && (
        <DeleteWorkspaceModal
          workspace={deleting}
          onClose={() => setDeleting(null)}
        />
      )}
    </aside>
  );
}
