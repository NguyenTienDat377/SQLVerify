-- Migration #5 — organizations: shared identity for the Team tier.
-- An org has one owner and up to `seat_limit` members (see org_members);
-- verification_runs can be tagged to an org the same way they're tagged to a project.
--
-- Both tables are created before any policy is attached: policies are checked
-- against real relations at CREATE POLICY time (unlike a FOREIGN KEY, which
-- Postgres can defer), and organizations' SELECT policy needs org_members to
-- already exist, so table creation and policy creation are kept in separate
-- passes rather than interleaved table-by-table.
CREATE TABLE IF NOT EXISTS public.organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    seat_limit  INT NOT NULL DEFAULT 5,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS organizations_owner_id_idx ON public.organizations (owner_id);

-- Membership: who belongs to which org. The owner is implicit (organizations.owner_id)
-- and does not need a row here; org_members covers the additional seats.
CREATE TABLE IF NOT EXISTS public.org_members (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS org_members_org_id_idx  ON public.org_members (org_id);
CREATE INDEX IF NOT EXISTS org_members_user_id_idx ON public.org_members (user_id);

-- ── organizations policies ──────────────────────────────────────────────
ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "members_select_own_org"
    ON public.organizations FOR SELECT TO authenticated
    USING (
        owner_id = auth.uid()
        OR id IN (SELECT org_id FROM public.org_members WHERE user_id = auth.uid())
    );

CREATE POLICY "owners_insert_own_org"
    ON public.organizations FOR INSERT TO authenticated
    WITH CHECK (owner_id = auth.uid());

CREATE POLICY "owners_update_own_org"
    ON public.organizations FOR UPDATE TO authenticated
    USING (owner_id = auth.uid())
    WITH CHECK (owner_id = auth.uid());

CREATE POLICY "owners_delete_own_org"
    ON public.organizations FOR DELETE TO authenticated
    USING (owner_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON public.organizations TO authenticated;

-- ── org_members policies ────────────────────────────────────────────────
ALTER TABLE public.org_members ENABLE ROW LEVEL SECURITY;

CREATE POLICY "members_select_own_membership"
    ON public.org_members FOR SELECT TO authenticated
    USING (
        user_id = auth.uid()
        OR org_id IN (SELECT id FROM public.organizations WHERE owner_id = auth.uid())
    );

CREATE POLICY "owners_insert_members"
    ON public.org_members FOR INSERT TO authenticated
    WITH CHECK (org_id IN (SELECT id FROM public.organizations WHERE owner_id = auth.uid()));

CREATE POLICY "owners_delete_members"
    ON public.org_members FOR DELETE TO authenticated
    USING (org_id IN (SELECT id FROM public.organizations WHERE owner_id = auth.uid()));

GRANT SELECT, INSERT, DELETE ON public.org_members TO authenticated;

-- Link runs to an org; SET NULL keeps the run if its org is deleted.
ALTER TABLE public.verification_runs
    ADD COLUMN IF NOT EXISTS organization_id UUID
        REFERENCES public.organizations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS verification_runs_organization_id_idx
    ON public.verification_runs (organization_id);
