"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { WorkspaceMeta } from "@/lib/types";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { Button, Spinner } from "./ui";
import { isProtectedGroup, useWorkspaces } from "./WorkspacesProvider";

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
            className="mb-1.5 block text-xs font-medium text-neutral-600"
          >
            Name
          </label>
          <input
            id="workspace-name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="IFF 2026 Intake"
            className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
        </div>
        <div>
          <p className="mb-1.5 text-xs font-medium text-neutral-600">Group</p>
          <p className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-700">
            {group}
          </p>
        </div>
        {saving && connecting && (
          <p className="text-xs text-neutral-400">
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

/** Rename dialog. A workspace in a live submissions group gets a stronger
 * confirmation before the field is even editable: its name is the id every
 * run, ledger and data path is keyed on. */
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
  const live = isProtectedGroup(workspace.group);

  const [acknowledged, setAcknowledged] = useState(!live);
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

  if (live && !acknowledged) {
    return (
      <Modal title="Rename a live workspace?" onClose={onClose}>
        <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2.5 text-sm leading-relaxed text-amber-900">
          This is the live IFF recruitment workspace. Are you absolutely sure
          you want to rename it?
        </p>
        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          The name identifies the workspace everywhere: its data directory, its
          run history and its send ledger all move with it. Anyone holding a
          link to the old name will need the new one.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" onClick={() => setAcknowledged(true)}>
            Yes, let me rename it
          </Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Rename "${workspace.name}"`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label
            htmlFor="rename-workspace"
            className="mb-1.5 block text-xs font-medium text-neutral-600"
          >
            New name
          </label>
          <input
            id="rename-workspace"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-500"
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
      <p className="text-sm leading-relaxed text-neutral-700">
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

/** The "…" menu on a workspace row. Delete is absent for a live submissions
 * workspace; picking it anyway is impossible, and the API refuses it too. */
function RowMenu({
  workspace,
  onRename,
  onDelete,
}: {
  workspace: WorkspaceMeta;
  onRename: () => void;
  onDelete: () => void;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const live = isProtectedGroup(workspace.group);

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
        className="rounded px-1.5 py-0.5 text-neutral-400 opacity-0 transition-opacity hover:bg-neutral-200 hover:text-neutral-700 focus:opacity-100 group-hover:opacity-100"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 top-6 z-30 w-44 overflow-hidden rounded-md border border-neutral-200 bg-white py-1 shadow-lg">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onRename();
            }}
            className="block w-full px-3 py-1.5 text-left text-sm text-neutral-700 hover:bg-neutral-100"
          >
            Rename
          </button>
          {live ? (
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                toast.error("Live submission workspaces cannot be deleted.");
              }}
              className="block w-full cursor-not-allowed px-3 py-1.5 text-left text-sm text-neutral-400"
              title="Live submission workspaces cannot be deleted."
            >
              Delete
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onDelete();
              }}
              className="block w-full px-3 py-1.5 text-left text-sm text-red-600 hover:bg-red-50"
            >
              Delete
            </button>
          )}
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

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-neutral-500 hover:text-neutral-800"
      >
        <span
          className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden="true"
        >
          ›
        </span>
        <span className="flex-1">{group}</span>
        <span className="font-normal normal-case text-neutral-400">
          {items.length}
        </span>
      </button>

      {open && (
        <div className="mt-1">
          {items.length === 0 && (
            <p className="px-3 py-1.5 text-xs italic text-neutral-400">
              No workspaces yet
            </p>
          )}
          {items.map((w) => {
            const active = w.name === activeId;
            return (
              <div
                key={w.name}
                className={`group flex items-center gap-1 rounded-md pr-1.5 transition-colors ${
                  active
                    ? "bg-neutral-200 font-medium text-neutral-900"
                    : "text-neutral-700 hover:bg-neutral-100"
                }`}
              >
                <Link
                  href={`/workspace/${encodeURIComponent(w.name)}`}
                  title={w.name}
                  className="min-w-0 flex-1 truncate px-3 py-1.5 text-sm"
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
            className="mt-0.5 w-full rounded-md px-3 py-1.5 text-left text-sm text-neutral-500 hover:bg-neutral-100 hover:text-neutral-800"
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
    <aside className="flex w-60 shrink-0 flex-col border-r border-neutral-200 bg-neutral-50">
      <nav className="flex-1 overflow-y-auto py-3">
        {loading && (
          <p className="flex items-center gap-2 px-3 py-2 text-xs text-neutral-500">
            <Spinner />{" "}
            {connecting ? "Connecting to backend…" : "Loading workspaces…"}
          </p>
        )}

        {connecting && (
          <p className="mx-3 mt-1 text-xs text-neutral-400">
            The scheduler API is waking up. This can take up to half a minute
            on the first request.
          </p>
        )}

        {error && !loading && error !== dismissedError && (
          <div className="mx-3 flex items-start gap-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            <p className="flex-1 leading-snug">{error}</p>
            <button
              type="button"
              onClick={() => setDismissedError(error)}
              aria-label="Dismiss error"
              className="-m-1 shrink-0 rounded p-1 leading-none opacity-60 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-current"
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
