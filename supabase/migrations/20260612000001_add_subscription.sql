-- migrations/create_subscriptions.sql
-- Run this in Supabase SQL editor

CREATE TABLE subscriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id         TEXT NOT NULL,
    customer_email      TEXT NOT NULL,
    subscription_id     TEXT UNIQUE NOT NULL,
    order_id            TEXT,
    product_id          TEXT,
    variant_id          TEXT,
    status              TEXT NOT NULL DEFAULT 'active',  -- active | cancelled | expired | paused
    tier                TEXT NOT NULL DEFAULT 'individual', -- individual | team
    current_period_end  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

-- Fast lookups by email (maps Supabase auth user → subscription)
CREATE INDEX idx_subscriptions_email  ON subscriptions(customer_email);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);

-- RLS
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

-- Server-side writes only — service key bypasses RLS
CREATE POLICY "service_role_all" ON subscriptions
    FOR ALL TO service_role USING (true) WITH CHECK (true);