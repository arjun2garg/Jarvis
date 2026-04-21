-- ============================================================
-- Jarvis — Hevy schema
-- Run this in the Supabase SQL Editor.
-- Follows the landing/staging pattern used for Todoist.
-- ============================================================


-- ── Landing table ───────────────────────────────────────────
-- One row per workout. Payload is the full Hevy API response,
-- verbatim. All transformation happens in the views below, so
-- we never lose source data and sync stays dead simple.
CREATE TABLE IF NOT EXISTS raw_hevy (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hevy_id    TEXT UNIQUE NOT NULL,
    payload    JSONB NOT NULL,
    synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_hevy_synced_at
    ON raw_hevy (synced_at DESC);

-- Common query: "what workouts happened this week?"
CREATE INDEX IF NOT EXISTS idx_raw_hevy_start_time
    ON raw_hevy ((payload->>'start_time'));

-- RLS — matches raw_todoist pattern (personal single-user DB)
ALTER TABLE raw_hevy ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow anonymous read" ON raw_hevy;
CREATE POLICY "Allow anonymous read" ON raw_hevy FOR SELECT USING (true);


-- ── Staging view: workouts ──────────────────────────────────
-- One row per workout, clean typed columns, exercises array
-- kept as JSONB so Claude can drill in.
CREATE OR REPLACE VIEW workouts AS
SELECT
    r.id,
    r.hevy_id                                               AS source_id,
    'hevy'                                                  AS source,
    r.payload->>'title'                                     AS title,
    r.payload->>'description'                               AS description,
    (r.payload->>'start_time')::timestamptz                 AS started_at,
    (r.payload->>'end_time')::timestamptz                   AS ended_at,
    ROUND(
        EXTRACT(EPOCH FROM (
            (r.payload->>'end_time')::timestamptz -
            (r.payload->>'start_time')::timestamptz
        )) / 60
    )::int                                                  AS duration_minutes,
    jsonb_array_length(COALESCE(r.payload->'exercises', '[]'::jsonb))
                                                            AS exercise_count,
    (
        SELECT COALESCE(SUM(jsonb_array_length(ex->'sets')), 0)::int
        FROM jsonb_array_elements(COALESCE(r.payload->'exercises', '[]'::jsonb)) AS ex
    )                                                       AS total_sets,
    -- Volume in kg: weight × reps, normal sets only (warmups excluded)
    (
        SELECT COALESCE(SUM(
            (s->>'weight_kg')::numeric * (s->>'reps')::int
        ), 0)::numeric
        FROM jsonb_array_elements(COALESCE(r.payload->'exercises', '[]'::jsonb)) AS ex,
             jsonb_array_elements(COALESCE(ex->'sets', '[]'::jsonb)) AS s
        WHERE s->>'weight_kg' IS NOT NULL
          AND s->>'reps'      IS NOT NULL
          AND COALESCE(s->>'set_type', 'normal') = 'normal'
    )                                                       AS volume_kg,
    r.payload->'exercises'                                  AS exercises,
    r.synced_at
FROM raw_hevy r;


-- ── Staging view: exercise_sets ─────────────────────────────
-- Flattened — one row per set. Ideal for per-exercise trend
-- queries ("bench press over the last 3 months").
CREATE OR REPLACE VIEW exercise_sets AS
SELECT
    r.hevy_id                                               AS workout_id,
    (r.payload->>'start_time')::timestamptz                 AS workout_date,
    r.payload->>'title'                                     AS workout_title,
    ex->>'title'                                            AS exercise_name,
    ex->>'exercise_template_id'                             AS exercise_template_id,
    (ex->>'index')::int                                     AS exercise_index,
    (s->>'index')::int                                      AS set_index,
    COALESCE(s->>'set_type', 'normal')                      AS set_type,
    (s->>'weight_kg')::numeric                              AS weight_kg,
    (s->>'reps')::int                                       AS reps,
    (s->>'rpe')::numeric                                    AS rpe,
    (s->>'duration_seconds')::int                           AS duration_seconds,
    (s->>'distance_meters')::numeric                        AS distance_meters,
    -- Volume contribution for this single set (kg × reps)
    CASE
        WHEN s->>'weight_kg' IS NOT NULL AND s->>'reps' IS NOT NULL
        THEN (s->>'weight_kg')::numeric * (s->>'reps')::int
    END                                                     AS volume_kg
FROM raw_hevy r,
     jsonb_array_elements(COALESCE(r.payload->'exercises', '[]'::jsonb)) AS ex,
     jsonb_array_elements(COALESCE(ex->'sets', '[]'::jsonb)) AS s;
