-- Migration #6 — org-scoped projects: a project may belong to an org instead
-- of being purely personal, so its runs become visible to every member of
-- that org, not just the creator (roadmap item #3, "Team is real now").
ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS org_id UUID
        REFERENCES public.organizations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS projects_org_id_idx ON public.projects (org_id);

-- Defense-in-depth: let org members read a project scoped to their org, not
-- just the owner. (The service key bypasses RLS for server writes; the real
-- gate is db/repositories/projects.py, which scopes explicitly in code.)
CREATE POLICY "org_members_select_shared_projects"
    ON public.projects FOR SELECT TO authenticated
    USING (
        org_id IS NOT NULL
        AND org_id IN (
            SELECT id FROM public.organizations WHERE owner_id = auth.uid()
            UNION
            SELECT org_id FROM public.org_members WHERE user_id = auth.uid()
        )
    );
