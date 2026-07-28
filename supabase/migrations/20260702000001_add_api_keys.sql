-- Migration #2: per-user API keys for the CI/CD auth path.
-- Only the sha256 hash is stored; the raw `skm_…` key is shown once at creation.
-- Server lookups use the service key (RLS-bypassing) so resolve_api_key() can
-- match a hash across all users.

CREATE TABLE IF NOT EXISTS public.api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_prefix   TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_user_id_idx  ON public.api_keys (user_id);
CREATE INDEX IF NOT EXISTS api_keys_key_hash_idx ON public.api_keys (key_hash);

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_select_own_keys"
    ON public.api_keys FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "users_insert_own_keys"
    ON public.api_keys FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_update_own_keys"
    ON public.api_keys FOR UPDATE TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE ON public.api_keys TO authenticated;
