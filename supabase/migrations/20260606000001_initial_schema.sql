-- Migration: initial schema
-- Captures the verification_runs table that was created manually in the dashboard.
-- Run this on a fresh instance; skip on prod (table already exists).

CREATE TABLE IF NOT EXISTS public.verification_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status              TEXT NOT NULL,           -- equivalent | divergent | unknown | error
    divergence_reason   TEXT,
    counterexample_db   JSONB,
    query_v1_output     JSONB,
    query_v2_output     JSONB,
    error_message       TEXT,
    explanation         TEXT,
    ddl_input           TEXT,
    query_a_input       TEXT,
    query_b_input       TEXT,
    result_json         JSONB,
    bound               INT DEFAULT 3,
    duration_ms         INT,
    dialect             TEXT,
    ddl_sql             TEXT,
    sql_v1              TEXT,
    sql_v2              TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.verification_runs ENABLE ROW LEVEL SECURITY;
