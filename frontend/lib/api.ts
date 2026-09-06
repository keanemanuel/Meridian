/** Thin typed client for the Meridian FastAPI backend.
 *
 * Every call goes straight to NEXT_PUBLIC_API_URL from the browser — this is
 * an internal committee tool with no auth layer to proxy through.
 */

import type {
  Assignment,
  CapacityCheck,
  IngestResult,
  InvitePreview,
  PatchAssignmentResult,
  PublishResult,
  ResultPreview,
  RunSummary,
  SolveResult,
  WorkspaceMeta,
} from "./types";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** One violation/issue row from a 400 whose `detail` is an object. */
export type ApiIssue = { applicant_id?: string; code?: string; message: string };

export class ApiError extends Error {
  status: number;
  issues: ApiIssue[];

  constructor(status: number, message: string, issues: ApiIssue[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.issues = issues;
  }
}

/** FastAPI's `detail` is a string for simple errors and an object carrying
 * `message` + `violations`/`issues` for the audited ones (E-12, FR-64).
 * Flatten both into one shape the UI can always render. */
function parseDetail(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;

  if (typeof detail === "string") return new ApiError(status, detail);

  if (detail && typeof detail === "object") {
    const d = detail as {
      message?: string;
      violations?: ApiIssue[];
      issues?: ApiIssue[];
    };
    return new ApiError(
      status,
      d.message ?? `Request failed (${status})`,
      d.violations ?? d.issues ?? [],
    );
  }

  // Pydantic 422 bodies are an array of loc/msg objects.
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((e: { msg?: string }) => e?.msg)
      .filter(Boolean) as string[];
    return new ApiError(status, msgs.join("; ") || `Request failed (${status})`);
  }

  return new ApiError(status, `Request failed (${status})`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        ...(init?.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(
      0,
      `Cannot reach the API at ${API_URL}. Is the backend running?`,
    );
  }

  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* non-JSON error body — fall through to the generic message */
    }
    throw parseDetail(res.status, body);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

/** Assignment ids are `applicant:choice`, so they must be encoded into the path. */
const seg = (s: string) => encodeURIComponent(s);

export const api = {
  health: () => request<{ status: string }>("/api/health"),

  listWorkspaces: () => request<WorkspaceMeta[]>("/api/workspaces"),

  createWorkspace: (name: string, group: string) =>
    request<WorkspaceMeta>("/api/workspaces", json({ name, group })),

  getWorkspace: (id: string) =>
    request<WorkspaceMeta>(`/api/workspaces/${seg(id)}`),

  deleteWorkspace: (id: string) =>
    request<{ deleted: string }>(`/api/workspaces/${seg(id)}`, {
      method: "DELETE",
    }),

  /** `source=csv` needs a file upload; `source=sheets` reads the linked Sheet. */
  ingestCsv: (id: string, file: File) => {
    const form = new FormData();
    form.append("source", "csv");
    form.append("file", file);
    return request<IngestResult>(`/api/workspaces/${seg(id)}/ingest`, {
      method: "POST",
      body: form,
    });
  },

  ingestSheets: (id: string, force = false) => {
    const form = new FormData();
    form.append("source", "sheets");
    form.append("force", String(force));
    return request<IngestResult>(`/api/workspaces/${seg(id)}/ingest`, {
      method: "POST",
      body: form,
    });
  },

  check: (id: string) =>
    request<CapacityCheck>(`/api/workspaces/${seg(id)}/check`, {
      method: "POST",
    }),

  solve: (id: string, skipCheck = false) =>
    request<SolveResult>(
      `/api/workspaces/${seg(id)}/solve`,
      json({ skip_check: skipCheck }),
    ),

  publish: (id: string, run = "latest") =>
    request<PublishResult>(
      `/api/workspaces/${seg(id)}/publish`,
      json({ run, formats: ["xlsx", "html"] }),
    ),

  listRuns: (id: string) =>
    request<RunSummary[]>(`/api/workspaces/${seg(id)}/runs`),

  getAssignments: (id: string, runId: string) =>
    request<Assignment[]>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/assignments`,
    ),

  patchAssignment: (
    id: string,
    runId: string,
    assignmentId: string,
    panelId: string,
    slotId: string,
  ) =>
    request<PatchAssignmentResult>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/assignments/${seg(assignmentId)}`,
      { method: "PATCH", body: JSON.stringify({ panel_id: panelId, slot_id: slotId }) },
    ),

  /** Re-solve honouring every lock (C6). Writes a fresh run. */
  resolve: (id: string, runId: string, skipCheck = false) =>
    request<SolveResult>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/resolve`,
      json({ skip_check: skipCheck }),
    ),

  invitePreview: (id: string, runId: string) =>
    request<InvitePreview>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/notify/invite/preview`,
      { method: "POST" },
    ),

  inviteSend: (id: string, runId: string, confirmCount: number) =>
    request<{ sent_total?: number; failed_total?: number; attempted?: number; message?: string }>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/notify/invite/send`,
      json({ confirm_count: confirmCount }),
    ),

  resultPreview: (id: string, runId: string) =>
    request<ResultPreview>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/notify/result/preview`,
      { method: "POST" },
    ),

  resultSend: (id: string, runId: string, confirmCount: number, verifiedBy: string) =>
    request<{ sent_total?: number; attempted?: number; message?: string }>(
      `/api/workspaces/${seg(id)}/runs/${seg(runId)}/notify/result/send`,
      json({ confirm_count: confirmCount, verified_by: verifiedBy }),
    ),
};

/** The send endpoints reject a `confirm_count` that no longer matches the
 * ledger-filtered pending count, and put the real number in the message
 * (FR-62/FR-64). Pull it out so the UI can re-confirm against it instead of
 * making the user guess. */
export function pendingCountFromError(err: ApiError): number | null {
  const m = /!=\s*(\d+)\s*pending/.exec(err.message);
  return m ? Number(m[1]) : null;
}
