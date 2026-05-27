-- ============================================================
-- Upstox Token Storage
-- Run this in Supabase SQL Editor once.
-- ============================================================

CREATE TABLE IF NOT EXISTS upstox_tokens (
    id           BIGSERIAL PRIMARY KEY,
    access_token TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL
);

-- Keep only last 7 tokens (one per day)
CREATE INDEX IF NOT EXISTS idx_upstox_tokens_created
    ON upstox_tokens (created_at DESC);

-- Auto-delete tokens older than 7 days
-- DELETE FROM upstox_tokens WHERE created_at < NOW() - INTERVAL '7 days';