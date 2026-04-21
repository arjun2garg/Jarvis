# Hevy Integration — Jarvis

Three things to set up: the Supabase schema, the sync script, and the Claude Project update.

---

## Step 1: Supabase Schema

Run this in the Supabase SQL editor. It follows the same landing/staging pattern as Todoist.

```sql
-- ============================================================
-- LANDING TABLE: raw Hevy API payloads
-- ============================================================
CREATE TABLE raw_hevy (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hevy_id     TEXT UNIQUE NOT NULL,         -- Hevy's workout ID
    payload     JSONB NOT NULL,               -- full API response for this workout
    synced_at   TIMESTAMPTZ DEFAULT now()
);

-- Index for time-range queries on synced_at
CREATE INDEX idx_raw_hevy_synced_at ON raw_hevy (synced_at DESC);

-- RLS (matches your existing pattern)
ALTER TABLE raw_hevy ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow anonymous read" ON raw_hevy FOR SELECT USING (true);


-- ============================================================
-- STAGING VIEW: workouts
-- Exposes one row per workout with clean typed columns.
-- Exercises/sets remain as JSONB — Claude can dig into them.
-- ============================================================
CREATE VIEW workouts AS
SELECT
    r.id,
    r.hevy_id                                               AS source_id,
    r.payload->>'title'                                     AS title,
    r.payload->>'description'                               AS description,
    (r.payload->>'start_time')::timestamptz                 AS started_at,
    (r.payload->>'end_time')::timestamptz                   AS ended_at,
    -- Duration in minutes, rounded
    ROUND(
        EXTRACT(EPOCH FROM (
            (r.payload->>'end_time')::timestamptz -
            (r.payload->>'start_time')::timestamptz
        )) / 60
    )::int                                                  AS duration_minutes,
    -- Number of exercises
    jsonb_array_length(r.payload->'exercises')              AS exercise_count,
    -- Total sets across all exercises
    (
        SELECT COALESCE(SUM(jsonb_array_length(ex->'sets')), 0)
        FROM jsonb_array_elements(r.payload->'exercises') AS ex
    )::int                                                  AS total_sets,
    -- Total volume in kg (weight_kg * reps, summed across all sets)
    (
        SELECT COALESCE(SUM(
            (s->>'weight_kg')::numeric * (s->>'reps')::int
        ), 0)
        FROM jsonb_array_elements(r.payload->'exercises') AS ex,
             jsonb_array_elements(ex->'sets') AS s
        WHERE s->>'weight_kg' IS NOT NULL
          AND s->>'reps' IS NOT NULL
          AND s->>'set_type' = 'normal'     -- exclude warmups from volume
    )                                                       AS volume_kg,
    -- Full exercises array intact for Claude to inspect
    r.payload->'exercises'                                  AS exercises,
    r.synced_at
FROM raw_hevy r;


-- ============================================================
-- HELPER VIEW: exercise_sets
-- Flattened — one row per set. Useful for per-exercise queries
-- e.g. "what's my bench press trend over the last 3 months"
-- ============================================================
CREATE VIEW exercise_sets AS
SELECT
    r.hevy_id                                   AS workout_id,
    (r.payload->>'start_time')::timestamptz     AS workout_date,
    r.payload->>'title'                         AS workout_title,
    ex->>'title'                                AS exercise_name,
    ex->>'exercise_template_id'                 AS exercise_template_id,
    (s->>'index')::int                          AS set_index,
    s->>'set_type'                              AS set_type,   -- normal/warmup/dropset/failure
    (s->>'weight_kg')::numeric                  AS weight_kg,
    (s->>'reps')::int                           AS reps,
    (s->>'rpe')::numeric                        AS rpe,
    (s->>'duration_seconds')::int               AS duration_seconds,
    (s->>'distance_meters')::numeric            AS distance_meters
FROM raw_hevy r,
     jsonb_array_elements(r.payload->'exercises') AS ex,
     jsonb_array_elements(ex->'sets') AS s;
```

---

## Step 2: Sync Script

Save as `sync_hevy.py`. Requires: `pip install requests supabase python-dotenv`

```python
#!/usr/bin/env python3
"""
Hevy → Supabase sync script.
Fetches all workouts from the Hevy API and upserts into raw_hevy.

Env vars required:
  HEVY_API_KEY        — from https://hevy.com/settings?developer (requires Hevy Pro)
  SUPABASE_URL        — e.g. https://fmbfaipklxalerkncnox.supabase.co
  SUPABASE_SERVICE_KEY — service role key (not anon key)
"""

import os
import sys
import json
from datetime import datetime, timezone
import requests
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

HEVY_API_KEY     = os.environ["HEVY_API_KEY"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]
HEVY_BASE        = "https://api.hevyapp.com/v1"
PAGE_SIZE        = 10  # Hevy's max is 10


def fetch_all_workouts() -> list[dict]:
    """Paginate through all workouts from the Hevy API."""
    headers = {"api-key": HEVY_API_KEY, "accept": "application/json"}
    workouts = []
    page = 1

    while True:
        resp = requests.get(
            f"{HEVY_BASE}/workouts",
            headers=headers,
            params={"page": page, "pageSize": PAGE_SIZE},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("workouts", [])
        workouts.extend(batch)

        # Hevy returns page_count in the response
        page_count = data.get("page_count", 1)
        if page >= page_count:
            break
        page += 1

    return workouts


def sync():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    synced_at = datetime.now(timezone.utc).isoformat()

    print("Fetching workouts from Hevy...")
    try:
        workouts = fetch_all_workouts()
    except requests.HTTPError as e:
        log_error(supabase, str(e))
        sys.exit(1)

    print(f"  Fetched {len(workouts)} workouts")

    if not workouts:
        log_sync(supabase, 0)
        return

    # Build upsert rows
    rows = [
        {
            "hevy_id":   w["id"],
            "payload":   json.dumps(w),   # store full payload as JSONB
            "synced_at": synced_at,
        }
        for w in workouts
    ]

    # Upsert in batches of 100 (well within Supabase limits)
    batch_size = 100
    total_upserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        supabase.table("raw_hevy").upsert(
            batch,
            on_conflict="hevy_id"  # idempotent — safe to re-run anytime
        ).execute()
        total_upserted += len(batch)

    log_sync(supabase, total_upserted)
    print(f"  Upserted {total_upserted} workouts into raw_hevy ✓")


def log_sync(supabase, count: int):
    supabase.table("sync_log").insert({
        "source": "hevy",
        "records_synced": count,
        "status": "success",
    }).execute()


def log_error(supabase, message: str):
    supabase.table("sync_log").insert({
        "source": "hevy",
        "records_synced": 0,
        "status": "error",
        "error_message": message,
    }).execute()
    print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":
    sync()
```

### .env file (add to existing one)
```
HEVY_API_KEY=your_key_here
```

### Cron (same schedule as Todoist)
```bash
# Every 30 minutes — add to crontab -e
*/30 * * * * cd /path/to/jarvis && python sync_hevy.py >> logs/hevy.log 2>&1
```

### GitHub Actions (alternative — add to existing workflow)
```yaml
- name: Sync Hevy
  run: python sync_hevy.py
  env:
    HEVY_API_KEY: ${{ secrets.HEVY_API_KEY }}
    SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
    SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

---

## Step 3: Update Claude Project Instructions

Add this block to your Jarvis Project system instructions:

```
## Hevy Workouts

Schema:
- `workouts` view: one row per workout — title, started_at, ended_at, duration_minutes,
  exercise_count, total_sets, volume_kg, exercises (JSONB array)
- `exercise_sets` view: one row per set — workout_date, exercise_name, set_type,
  weight_kg, reps, rpe, duration_seconds, distance_meters

Set types: normal | warmup | dropset | failure
Volume = weight_kg × reps, normal sets only.

Useful queries:
- Recent workouts: SELECT title, started_at, duration_minutes, volume_kg FROM workouts ORDER BY started_at DESC LIMIT 7
- Exercise trend: SELECT workout_date, weight_kg, reps FROM exercise_sets WHERE exercise_name ILIKE '%bench%' AND set_type = 'normal' ORDER BY workout_date DESC
- Weekly volume: SELECT date_trunc('week', started_at) AS week, SUM(volume_kg) FROM workouts GROUP BY 1 ORDER BY 1 DESC

When giving workout advice:
- Look at the last 2–4 weeks of data before making recommendations
- Note which muscle groups were trained and when (for recovery awareness)
- Flag if the user hasn't trained in more than 5 days
- For progressive overload suggestions, compare the last 3 sessions of the same exercise
```

---

## What Claude Can Now Do

With this integration live, here are things you can ask in Jarvis:

- *"How has my bench press progressed this month?"*
- *"What did I train this week, and what's missing?"*
- *"Am I overtraining any muscle group?"*
- *"Suggest what I should do today based on my recent workouts."*
- *"How does my training volume this month compare to last month?"*
- *"What was my best set on Romanian deadlifts ever?"*

The `exercise_sets` view is the workhorse for per-exercise analysis. The `workouts` view is best for session-level overviews and volume trends.

---

## Prerequisites

- **Hevy Pro** subscription (required for API access)
- API key from: https://hevy.com/settings?developer