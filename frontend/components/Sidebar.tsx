"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
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
  const { create } = useWorkspaces();
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

function Section({
  group,
  onNew,
}: {
  group: string;
  onNew: (group: string) => void;
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
              <Link
                key={w.name}
                href={`/workspace/${encodeURIComponent(w.name)}`}
                title={w.name}
                className={`block truncate rounded-md px-3 py-1.5 text-sm transition-colors ${
                  active
                    ? "bg-neutral-200 font-medium text-neutral-900"
                    : "text-neutral-700 hover:bg-neutral-100"
                }`}
              >
                {w.name}
              </Link>
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
  const { groups, loading, error } = useWorkspaces();
  const [newIn, setNewIn] = useState<string | null>(null);

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-neutral-200 bg-neutral-50">
      <nav className="flex-1 overflow-y-auto py-3">
        {loading && (
          <p className="flex items-center gap-2 px-3 py-2 text-xs text-neutral-500">
            <Spinner /> Loading workspaces…
          </p>
        )}

        {error && (
          <p className="mx-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </p>
        )}

        {!loading &&
          groups.map((group) => (
            <Section key={group} group={group} onNew={setNewIn} />
          ))}
      </nav>

      {newIn && (
        <NewWorkspaceModal group={newIn} onClose={() => setNewIn(null)} />
      )}
    </aside>
  );
}
