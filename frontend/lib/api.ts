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

// "/api" everywhere: on Vercel, vercel.json routes /api/* straight to the
// Python function (same origin); locally, next.config.ts rewrites the same
// path to the FastAPI dev server. Every path below already omits the /api
// prefix — it lives here instead — so the two never double up.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api";

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

/** Per-attempt hard ceiling. The API runs on Railway's free tier, which
 * cold-starts in 10–30s after an idle period; a shorter timeout would abort
 * a request the backend is still legitimately booting to answer. */
const REQUEST_TIMEOUT_MS = 30_000;

/** Retries *after* the first try, for calls that opt in (`retry: true`).
 * 3 retries × 5s gaps rides out a cold start without hammering the box. */
const RETRY_ATTEMPTS = 3;
const RETRY_GAP_MS = 5_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Options accepted alongside `fetch`'s own — `retry` turns on the
 * cold-start retry loop, `onRetry` fires before each wait so the UI can show
 * a "connecting" state instead of an error. */
type RequestInitEx = RequestInit & {
  retry?: boolean;
  onRetry?: (attempt: number, maxAttempts: number) => void;
};

/** Retry only failures that a cold/booting backend produces: the fetch
 * itself threw (DNS/connection refused/timeout → status 0) or an edge proxy
 * returned a gateway error while the app was still starting. A 4xx is a real
 * answer — never retry it. */
function isTransient(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    (err.status === 0 ||
      err.status === 502 ||
      err.status === 503 ||
      err.status === 504)
  );
}

async function attemptOnce<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      cache: "no-store",
      signal: controller.signal,
      headers: {
        ...(init?.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
        ...init?.headers,
      },
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "AbortError";
    throw new ApiError(
      0,
      timedOut
        ? `The API at ${API_URL} did not respond within ${REQUEST_TIMEOUT_MS / 1000}s.`
        : `Cannot reach the API at ${API_URL}. Is the backend running?`,
    );
  } finally {
    clearTimeout(timer);
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

async function request<T>(path: string, init?: RequestInitEx): Promise<T> {
  const { retry = false, onRetry, ...fetchInit } = init ?? {};
  const maxAttempts = retry ? RETRY_ATTEMPTS + 1 : 1;

  for (let attempt = 1; ; attempt++) {
    try {
      return await attemptOnce<T>(path, fetchInit);
    } catch (err) {
      if (attempt >= maxAttempts || !isTransient(err)) throw err;
      onRetry?.(attempt, maxAttempts);
      await sleep(RETRY_GAP_MS);
    }
  }
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

/** Assignment ids are `applicant:choice`, so they must be encoded into the path. */
const seg = (s: string) => encodeURIComponent(s);

/** Callback the first-contact calls take so a caller (the workspaces
 * provider, the New Workspace form) can swap in a "connecting…" state while
 * a cold Railway backend boots. */
export type RetryHooks = {
  onRetry?: (attempt: number, maxAttempts: number) => void;
};

export const api = {
  health: () => request<{ status: string }>("/health"),

  // First contact after an idle period pays the Railway cold start, so this
  // and createWorkspace ride the retry loop; every later call assumes a warm
  // backend and fails fast.
  listWorkspaces: (hooks?: RetryHooks) =>
    request<WorkspaceMeta[]>("/workspaces", { retry: true, ...hooks }),

  createWorkspace: (name: string, group: string, hooks?: RetryHooks) =>
    request<WorkspaceMeta>("/workspaces", {
      ...json({ name, group }),
      retry: true,
      ...hooks,
    }),

  getWorkspace: (id: string) =>
    request<WorkspaceMeta>(`/workspaces/${seg(id)}`),

  deleteWorkspace: (id: string) =>
    request<{ deleted: string }>(`/workspaces/${seg(id)}`, {
      method: "DELETE",
    }),

  /** `source=csv` needs a file upload; `source=sheets` reads the linked Sheet. */
  ingestCsv: (id: string, file: File) => {
    const form = new FormData();
    form.append("source", "csv");
    form.append("file", file);
    return request<IngestResult>(`/workspaces/${seg(id)}/ingest`, {
      method: "POST",
      body: form,
    });
  },

  ingestSheets: (id: string, force = false) => {
    const form = new FormData();
    form.append("source", "sheets");
    form.append("force", String(force));
    return request<IngestResult>(`/workspaces/${seg(id)}/ingest`, {
      method: "POST",
      body: form,
    });
  },

  check: (id: string) =>
    request<CapacityCheck>(`/workspaces/${seg(id)}/check`, {
      method: "POST",
    }),

  solve: (id: string, skipCheck = false) =>
    request<SolveResult>(
      `/workspaces/${seg(id)}/solve`,
      json({ skip_check: skipCheck }),
    ),

  publish: (id: string, run = "latest") =>
    request<PublishResult>(
      `/workspaces/${seg(id)}/publish`,
      json({ run, formats: ["xlsx", "html"] }),
    ),

  listRuns: (id: string) =>
    request<RunSummary[]>(`/workspaces/${seg(id)}/runs`),

  getAssignments: (id: string, runId: string) =>
    request<Assignment[]>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/assignments`,
    ),

  patchAssignment: (
    id: string,
    runId: string,
    assignmentId: string,
    panelId: string,
    slotId: string,
  ) =>
    request<PatchAssignmentResult>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/assignments/${seg(assignmentId)}`,
      { method: "PATCH", body: JSON.stringify({ panel_id: panelId, slot_id: slotId }) },
    ),

  /** Re-solve honouring every lock (C6). Writes a fresh run. */
  resolve: (id: string, runId: string, skipCheck = false) =>
    request<SolveResult>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/resolve`,
      json({ skip_check: skipCheck }),
    ),

  invitePreview: (id: string, runId: string) =>
    request<InvitePreview>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/notify/invite/preview`,
      { method: "POST" },
    ),

  inviteSend: (id: string, runId: string, confirmCount: number) =>
    request<{ sent_total?: number; failed_total?: number; attempted?: number; message?: string }>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/notify/invite/send`,
      json({ confirm_count: confirmCount }),
    ),

  resultPreview: (id: string, runId: string) =>
    request<ResultPreview>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/notify/result/preview`,
      { method: "POST" },
    ),

  resultSend: (id: string, runId: string, confirmCount: number, verifiedBy: string) =>
    request<{ sent_total?: number; attempted?: number; message?: string }>(
      `/workspaces/${seg(id)}/runs/${seg(runId)}/notify/result/send`,
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
