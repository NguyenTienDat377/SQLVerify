-- Migration #4 — projects: let a user group verification runs into projects.
CREATE TABLE IF NOT EXISTS public.projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (owner_id, name)          -- no two projects with the same name per user
);

CREATE INDEX IF NOT EXISTS projects_owner_id_idx ON public.projects (owner_id);

ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_select_own_projects"
    ON public.projects FOR SELECT TO authenticated
    USING (owner_id = auth.uid());

CREATE POLICY "users_insert_own_projects"
    ON public.projects FOR INSERT TO authenticated
    WITH CHECK (owner_id = auth.uid());

CREATE POLICY "users_update_own_projects"
    ON public.projects FOR UPDATE TO authenticated
    USING (owner_id = auth.uid())
    WITH CHECK (owner_id = auth.uid());

CREATE POLICY "users_delete_own_projects"
    ON public.projects FOR DELETE TO authenticated
    USING (owner_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON public.projects TO authenticated;

-- Link runs to a project; SET NULL keeps the run if its project is deleted.
ALTER TABLE public.verification_runs
    ADD COLUMN IF NOT EXISTS project_id UUID
        REFERENCES public.projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS verification_runs_project_id_idx
    ON public.verification_runs (project_id);
