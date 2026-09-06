-- 001_initial.sql — beta Postgres schema (SPEC.md §14 "Beta").
--
-- Replaces the alpha CSV/YAML store (data/workspaces/<name>/...) with four
-- tables on Supabase Postgres. The core solver is untouched: it still returns
-- plain domain objects and the db/ repos convert those to the rows below.
--
-- Mapping to the alpha files:
--   workspaces   <- data/workspaces/workspaces.json
--   runs         <- data/workspaces/<name>/runs/<timestamp>/metrics.json
--   assignments  <- data/workspaces/<name>/runs/<timestamp>/assignments.csv
--   send_ledger  <- data/workspaces/<name>/ledger/send_ledger.csv

CREATE TABLE workspaces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  group_name TEXT NOT NULL,
  sheet_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
  run_label TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  metrics JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE assignments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
  applicant_id TEXT NOT NULL,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL,
  choice_index INTEGER NOT NULL,
  sub_division TEXT NOT NULL,
  division TEXT NOT NULL,
  panel_id TEXT NOT NULL,
  room TEXT NOT NULL,
  slot_id TEXT NOT NULL,
  date DATE NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  is_clash BOOLEAN DEFAULT false,
  is_locked BOOLEAN DEFAULT false,
  same_parent_pair BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE send_ledger (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
  applicant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  template TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  provider_message_id TEXT,
  attempt_count INTEGER DEFAULT 0,
  sent_at TIMESTAMPTZ,
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Lookup indexes for the repo access patterns (list by parent, then filter).
CREATE INDEX idx_runs_workspace_id ON runs (workspace_id, created_at DESC);
CREATE INDEX idx_assignments_run_id ON assignments (run_id);
CREATE INDEX idx_send_ledger_run_id ON send_ledger (run_id);
-- send_ledger is keyed by (applicant_id, template) per FR-62 ("a re-run never
-- double-sends the same applicant the same email"); the repo upserts on this.
CREATE UNIQUE INDEX idx_send_ledger_key ON send_ledger (run_id, applicant_id, template);
