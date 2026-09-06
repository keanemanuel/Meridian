/** Mirrors of the FastAPI response shapes (src/api/routers/*.py).
 *
 * Kept hand-written rather than generated: the surface is small and this way
 * a backend field rename shows up as a type error here rather than as an
 * undefined at runtime.
 */

export type WorkspaceMeta = {
  name: string;
  group: string;
  sheet_id: string | null;
  created_at: string;
};

/** `GET /runs` — one entry per solve. */
export type RunSummary = {
  run_id: string;
  has_assignments: boolean;
  created_at: string;
};

/** `Assignment` (domain/models.py) plus the synthetic id the API adds. */
export type Assignment = {
  /** `${applicant_id}:${choice_index}` — colon-bearing, so URL-encode it. */
  assignment_id: string;
  applicant_id: string;
  full_name: string;
  email: string;
  choice_index: 1 | 2;
  sub_division: string;
  division: string;
  panel_id: string;
  room: string;
  slot_id: string;
  /** ISO date, e.g. "2026-10-14". */
  date: string;
  /** ISO time, e.g. "09:00:00". */
  start_time: string;
  end_time: string;
  is_clash: boolean;
  is_locked: boolean;
  same_parent_pair: boolean;
  reason: string | null;
};

export type CapacityRow = {
  division: string;
  demand: number;
  panels_configured: number;
  raw_supply: number;
  effective_supply: number;
  recommended_panels: number;
  verdict: string;
};

export type CapacityCheck = {
  feasible: boolean;
  infeasible_divisions: string[];
  rows: CapacityRow[];
};

export type SolveResult = {
  run_id: string;
  status: string;
  phase: string;
  interviews_placed: number;
  interviews_required: number;
  clashes: number;
  locked: number;
  objective_value: number | null;
  solve_seconds: number;
  changed_vs_previous: number;
  conflicts: number;
};

export type IngestResult = {
  applicants: number;
  rejected: number;
  collapsed: number;
  warnings: number;
  report: unknown[];
  new_rows?: number;
};

export type PublishResult = {
  run_id: string;
  output_dir: string;
  room_views: number;
  applicants: number;
  panels: number;
  clashes_red: number;
  warnings_amber: number;
  formats: string[];
};

export type InvitePreview = {
  total: number;
  auto_sendable: number;
  held_for_manual: number;
  emails_dir: string;
  samples: { applicant_id: string; to_email: string; subject: string }[];
};

export type ResultPreview = {
  counts: Record<string, number>;
  emails_dir: string;
  samples: { applicant_id: string; to_email: string; subject: string }[];
};

export type PatchAssignmentResult = {
  assignment: Assignment;
  locked: boolean;
  total_locks: number;
};
