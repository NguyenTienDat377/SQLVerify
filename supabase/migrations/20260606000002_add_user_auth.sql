-- Migration: add user authorization
-- Adds user_id FK to verification_runs and scopes RLS so each authenticated
-- user only sees and modifies their own runs. Existing anonymous rows keep
-- user_id = NULL and are no longer visible through RLS (service role still
-- bypasses RLS for server-side reads).

-- ─── 1. user_id column ───────────────────────────────────────────────────────
ALTER TABLE public.verification_runs
    ADD COLUMN IF NOT EXISTS user_id UUID
        REFERENCES auth.users(id) ON DELETE SET NULL;

-- Index for per-user history queries
CREATE INDEX IF NOT EXISTS verification_runs_user_id_idx
    ON public.verification_runs (user_id);

-- ─── 2. Drop any broad catch-all policies that predate this migration ─────────
DROP POLICY IF EXISTS "Allow all"                       ON public.verification_runs;
DROP POLICY IF EXISTS "Enable read access for all users" ON public.verification_runs;
DROP POLICY IF EXISTS "Enable insert for all users"      ON public.verification_runs;

-- ─── 3. RLS policies — authenticated users only ──────────────────────────────

-- SELECT: users see only their own runs
CREATE POLICY "users_select_own_runs"
    ON public.verification_runs
    FOR SELECT
    TO authenticated
    USING (user_id = auth.uid());

-- INSERT: users can only insert rows tied to themselves
CREATE POLICY "users_insert_own_runs"
    ON public.verification_runs
    FOR INSERT
    TO authenticated
    WITH CHECK (user_id = auth.uid());

-- UPDATE: users can update (e.g. cache explanation) only their own runs
CREATE POLICY "users_update_own_runs"
    ON public.verification_runs
    FOR UPDATE
    TO authenticated
    USING  (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- The service_role key used in db/client.py bypasses RLS automatically —
-- no additional policy is needed for server-side writes.

-- ─── 4. Grant DML to authenticated role ──────────────────────────────────────
GRANT SELECT, INSERT, UPDATE
    ON public.verification_runs
    TO authenticated;
