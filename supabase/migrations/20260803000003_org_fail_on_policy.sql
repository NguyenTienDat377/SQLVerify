-- Migration #7 — server-side fail-on policy: an org owner sets which
-- statuses must fail a check for every project scoped to their org, so a
-- developer's own --fail-on flag can only add strictness, never remove it
-- (CLAUDE.md Team-tier decision table, item #5 — "a gate a member could
-- weaken on their own project isn't a gate").
ALTER TABLE public.organizations
    ADD COLUMN IF NOT EXISTS fail_on_policy TEXT NOT NULL DEFAULT 'divergent';
