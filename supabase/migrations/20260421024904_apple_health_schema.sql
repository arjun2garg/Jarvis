-- ============================================================
-- Jarvis — Apple Health schema
-- Run this in the Supabase SQL Editor (or via supabase db query --linked).
-- Follows the landing/staging pattern used for Todoist + Hevy.
-- ============================================================


-- ── Landing table ───────────────────────────────────────────
-- One row per (metric, day). Full original data point preserved
-- in data_point JSONB so sleep fields and any future additions
-- remain queryable without a re-sync.
CREATE TABLE IF NOT EXISTS raw_apple_health (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_name   TEXT NOT NULL,
    recorded_date DATE NOT NULL,
    value         NUMERIC,              -- qty for scalar metrics; NULL for sleep
    unit          TEXT,
    data_point    JSONB NOT NULL,       -- full original object preserved
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (metric_name, recorded_date)  -- idempotent upserts
);

CREATE INDEX IF NOT EXISTS idx_raw_apple_health_metric_date
    ON raw_apple_health (metric_name, recorded_date DESC);

-- RLS — matches raw_todoist / raw_hevy (personal single-user DB)
ALTER TABLE raw_apple_health ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow anonymous read" ON raw_apple_health;
CREATE POLICY "Allow anonymous read" ON raw_apple_health FOR SELECT USING (true);


-- ── Staging view: health_metrics ────────────────────────────
-- All scalar metrics — one row per metric per day.
CREATE OR REPLACE VIEW health_metrics AS
SELECT
    metric_name,
    recorded_date,
    value,
    unit,
    synced_at
FROM raw_apple_health
WHERE metric_name != 'sleep_analysis';


-- ── Staging view: sleep_analysis ────────────────────────────
-- Sleep-specific fields extracted from JSONB.
CREATE OR REPLACE VIEW sleep_analysis AS
SELECT
    recorded_date,
    (data_point->>'asleep')::numeric        AS hours_asleep,
    (data_point->>'inBed')::numeric         AS hours_in_bed,
    (data_point->>'asleepDeep')::numeric    AS deep_hours,
    (data_point->>'asleepREM')::numeric     AS rem_hours,
    (data_point->>'asleepCore')::numeric    AS core_hours,
    (data_point->>'awake')::numeric         AS awake_hours,
    ROUND(
        (data_point->>'asleep')::numeric /
        NULLIF((data_point->>'inBed')::numeric, 0) * 100, 1
    )                                       AS efficiency_pct,
    synced_at
FROM raw_apple_health
WHERE metric_name = 'sleep_analysis';


-- ── Staging view: health_latest ─────────────────────────────
-- Most recent value per scalar metric — handy for dashboards.
CREATE OR REPLACE VIEW health_latest AS
SELECT DISTINCT ON (metric_name)
    metric_name,
    recorded_date,
    value,
    unit
FROM raw_apple_health
WHERE metric_name != 'sleep_analysis'
ORDER BY metric_name, recorded_date DESC;
