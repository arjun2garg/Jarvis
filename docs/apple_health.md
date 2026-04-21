# Apple Health Integration — Jarvis

Three pieces: Supabase schema, an Edge Function (the webhook receiver), and Health Auto Export app config.

---

## Architecture

```
Health Auto Export (iPhone)
        │  POST /functions/v1/ingest-health
        ▼
Supabase Edge Function          ← authenticates, parses, normalizes
        │
        ▼
raw_apple_health (landing)      ← one row per data point, full JSONB preserved
        │
        ▼
health_metrics (staging view)   ← clean typed columns, all scalar metrics
sleep_analysis (staging view)   ← sleep-specific: deep/REM/core/awake stages
```

Health Auto Export sends a payload like:
```json
{
  "data": {
    "metrics": [
      { "name": "weight_body_mass", "units": "kg",
        "data": [{ "date": "2025-04-20 08:00:00 -0500", "qty": 70.5 }] },
      { "name": "sleep_analysis", "units": "hr",
        "data": [{ "date": "2025-04-20", "asleep": 7.5,
                   "asleepDeep": 1.1, "asleepREM": 2.2, "asleepCore": 4.2, "awake": 0.7,
                   "inBed": 8.2, "inBedStart": "...", "inBedEnd": "..." }] }
    ]
  }
}
```

---

## Step 1: Supabase Schema

```sql
-- ============================================================
-- LANDING TABLE
-- One row per data point. Unique on (metric_name, recorded_date).
-- ============================================================
CREATE TABLE raw_apple_health (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_name   TEXT NOT NULL,
    recorded_date DATE NOT NULL,
    value         NUMERIC,               -- qty for scalar metrics; NULL for sleep
    unit          TEXT,
    data_point    JSONB NOT NULL,        -- full original object preserved
    synced_at     TIMESTAMPTZ DEFAULT now(),

    UNIQUE (metric_name, recorded_date)  -- idempotent upserts
);

CREATE INDEX idx_raw_apple_health_metric_date
    ON raw_apple_health (metric_name, recorded_date DESC);

-- RLS
ALTER TABLE raw_apple_health ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow anonymous read" ON raw_apple_health FOR SELECT USING (true);


-- ============================================================
-- STAGING VIEW: health_metrics
-- All scalar metrics — one row per metric per day.
-- ============================================================
CREATE VIEW health_metrics AS
SELECT
    metric_name,
    recorded_date,
    value,
    unit,
    synced_at
FROM raw_apple_health
WHERE metric_name != 'sleep_analysis'   -- sleep gets its own view
ORDER BY recorded_date DESC;


-- ============================================================
-- STAGING VIEW: sleep_analysis
-- Sleep-specific fields extracted from JSONB.
-- ============================================================
CREATE VIEW sleep_analysis AS
SELECT
    recorded_date,
    (data_point->>'asleep')::numeric        AS hours_asleep,
    (data_point->>'inBed')::numeric         AS hours_in_bed,
    (data_point->>'asleepDeep')::numeric    AS deep_hours,
    (data_point->>'asleepREM')::numeric     AS rem_hours,
    (data_point->>'asleepCore')::numeric    AS core_hours,
    (data_point->>'awake')::numeric         AS awake_hours,
    -- Sleep efficiency: time asleep / time in bed
    ROUND(
        (data_point->>'asleep')::numeric /
        NULLIF((data_point->>'inBed')::numeric, 0) * 100, 1
    )                                       AS efficiency_pct,
    synced_at
FROM raw_apple_health
WHERE metric_name = 'sleep_analysis'
ORDER BY recorded_date DESC;


-- ============================================================
-- CONVENIENCE: latest values per metric (useful for dashboards)
-- ============================================================
CREATE VIEW health_latest AS
SELECT DISTINCT ON (metric_name)
    metric_name,
    recorded_date,
    value,
    unit
FROM raw_apple_health
WHERE metric_name != 'sleep_analysis'
ORDER BY metric_name, recorded_date DESC;
```

---

## Step 2: Edge Function

This is the webhook receiver. It lives in Supabase and gives you a public HTTPS URL.

### File: `supabase/functions/ingest-health/index.ts`

```typescript
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Metrics that have a simple qty field (all except sleep)
const SCALAR_METRICS = new Set([
  "weight_body_mass",
  "body_fat_percentage",
  "lean_body_mass",
  "body_mass_index",
  "resting_heart_rate",
  "heart_rate_variability_sdnn",
  "cardio_recovery",
  "walking_heart_rate_average",
  "respiratory_rate",
  "dietary_energy_consumed",
  "protein",
  "carbohydrates",
  "total_fat",
  "fiber",
  "sodium",
  "active_energy_burned",
  "basal_energy_burned",
  "step_count",
  "apple_exercise_time",
]);

Deno.serve(async (req: Request) => {
  // ── Auth ──────────────────────────────────────────────────
  const secret = req.headers.get("x-webhook-secret");
  if (secret !== Deno.env.get("HEALTH_WEBHOOK_SECRET")) {
    return new Response("Unauthorized", { status: 401 });
  }

  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  // ── Parse payload ─────────────────────────────────────────
  let body: any;
  try {
    body = await req.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const metrics: any[] = body?.data?.metrics ?? [];
  if (metrics.length === 0) {
    return new Response(JSON.stringify({ upserted: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // ── Build rows ────────────────────────────────────────────
  const rows: Record<string, any>[] = [];

  for (const metric of metrics) {
    const metricName: string = metric.name;
    const unit: string = metric.units ?? null;
    const dataPoints: any[] = metric.data ?? [];

    for (const point of dataPoints) {
      // Parse date — Health Auto Export sends "2025-04-20 08:00:00 -0500"
      // We only need the date portion for daily aggregates
      const rawDate: string = point.date ?? "";
      const recordedDate = rawDate.substring(0, 10); // "YYYY-MM-DD"

      if (!recordedDate || recordedDate.length < 10) continue;

      const isScalar = SCALAR_METRICS.has(metricName);
      const value = isScalar ? (point.qty ?? null) : null;

      rows.push({
        metric_name: metricName,
        recorded_date: recordedDate,
        value,
        unit,
        data_point: point,
        synced_at: new Date().toISOString(),
      });
    }
  }

  if (rows.length === 0) {
    return new Response(JSON.stringify({ upserted: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // ── Upsert into Supabase ──────────────────────────────────
  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  // Batch in chunks of 500 to stay well within limits
  const CHUNK = 500;
  let totalUpserted = 0;

  for (let i = 0; i < rows.length; i += CHUNK) {
    const chunk = rows.slice(i, i + CHUNK);
    const { error } = await supabase
      .from("raw_apple_health")
      .upsert(chunk, { onConflict: "metric_name,recorded_date" });

    if (error) {
      console.error("Upsert error:", error.message);
      return new Response(JSON.stringify({ error: error.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    totalUpserted += chunk.length;
  }

  // Log to sync_log
  await supabase.from("sync_log").insert({
    source: "apple_health",
    records_synced: totalUpserted,
    status: "success",
  });

  console.log(`Upserted ${totalUpserted} health data points`);
  return new Response(JSON.stringify({ upserted: totalUpserted }), {
    headers: { "Content-Type": "application/json" },
  });
});
```

### Deploy via Claude Code terminal

```bash
# 1. Create the function directory
mkdir -p supabase/functions/ingest-health

# 2. Save the TypeScript file above as supabase/functions/ingest-health/index.ts

# 3. Link project (if not already done)
supabase link --project-ref fmbfaipklxalerkncnox

# 4. Set the webhook secret (pick any strong random string)
supabase secrets set HEALTH_WEBHOOK_SECRET=your-secret-here

# 5. Deploy
supabase functions deploy ingest-health

# 6. Get your function URL (you'll paste this into Health Auto Export)
# Format: https://fmbfaipklxalerkncnox.supabase.co/functions/v1/ingest-health
```

### Test the function locally first (optional)

```bash
supabase functions serve ingest-health --env-file .env.local

# In another terminal, send a test payload:
curl -X POST http://localhost:54321/functions/v1/ingest-health \
  -H "Content-Type: application/json" \
  -H "x-webhook-secret: your-secret-here" \
  -d '{
    "data": {
      "metrics": [{
        "name": "weight_body_mass",
        "units": "kg",
        "data": [{"date": "2025-04-20 08:00:00 -0500", "qty": 70.5}]
      }]
    }
  }'
# Expected: {"upserted":1}
```

---

## Step 3: Health Auto Export App Config

1. Open **Health Auto Export** → **Automations** → **New Automation**
2. Select **REST API**
3. Configure:

| Setting | Value |
|---------|-------|
| **URL** | `https://fmbfaipklxalerkncnox.supabase.co/functions/v1/ingest-health` |
| **HTTP Header key** | `x-webhook-secret` |
| **HTTP Header value** | *(the secret you set above)* |
| **Export Format** | JSON |
| **Export Version** | Version 2 |
| **Batch Requests** | ON ← important, prevents memory issues |
| **Summarize Data** | ON, Daily |
| **Sync Cadence** | Every 1 hour (or Every 30 min if you want fresher data) |

4. Under **Health Metrics**, select exactly these metrics:

**Body:**
- Weight & Body Mass
- Body Fat Percentage
- Lean Body Mass
- Body Mass Index

**Heart:**
- Resting Heart Rate
- Heart Rate Variability (SDNN)
- Cardio Recovery
- Walking Heart Rate Average

**Sleep:**
- Sleep Analysis

**Respiratory:**
- Respiratory Rate

**Nutrition** *(these come from Cronometer via Apple Health):*
- Dietary Energy Consumed
- Protein
- Carbohydrates
- Total Fat
- Fiber
- Sodium

**Activity:**
- Active Energy Burned
- Basal Energy Burned
- Step Count
- Apple Exercise Time

5. Tap **Manual Export** once to backfill all historical data. Set the date range to your earliest data — this might send many batches.

---

## Step 4: Update Claude Project Instructions

Add this block to the Jarvis Project:

```
## Apple Health Metrics

Schema:
- `health_metrics` view: metric_name, recorded_date, value, unit — all scalar metrics, one row per day
- `sleep_analysis` view: recorded_date, hours_asleep, hours_in_bed, deep_hours, rem_hours, core_hours, awake_hours, efficiency_pct
- `health_latest` view: most recent value per metric — use this for "what is my current X"

Key metric names (snake_case, use with ILIKE or =):
  Body:           weight_body_mass, body_fat_percentage, lean_body_mass, body_mass_index
  Cardiovascular: resting_heart_rate, heart_rate_variability_sdnn, cardio_recovery, walking_heart_rate_average
  Respiratory:    respiratory_rate
  Nutrition:      dietary_energy_consumed, protein, carbohydrates, total_fat, fiber, sodium
  Activity:       active_energy_burned, basal_energy_burned, step_count, apple_exercise_time

Units: weight=kg, HRV=ms, heart rates=bpm, energy=kcal, protein/carbs/fat/fiber/sodium=g, steps=count, time=min

Useful queries:
  -- Weight trend (last 30 days)
  SELECT recorded_date, value FROM health_metrics
  WHERE metric_name = 'weight_body_mass'
  ORDER BY recorded_date DESC LIMIT 30;

  -- Today's nutrition summary
  SELECT metric_name, value, unit FROM health_metrics
  WHERE metric_name IN ('dietary_energy_consumed','protein','carbohydrates','total_fat')
  AND recorded_date = CURRENT_DATE;

  -- HRV trend (recovery signal)
  SELECT recorded_date, value FROM health_metrics
  WHERE metric_name = 'heart_rate_variability_sdnn'
  ORDER BY recorded_date DESC LIMIT 14;

  -- Sleep this week
  SELECT recorded_date, hours_asleep, deep_hours, rem_hours, efficiency_pct
  FROM sleep_analysis ORDER BY recorded_date DESC LIMIT 7;

  -- Caloric balance (eaten vs burned)
  SELECT
    n.recorded_date,
    n.value AS calories_eaten,
    a.value AS active_burned,
    b.value AS basal_burned,
    n.value - a.value - b.value AS net_surplus
  FROM health_metrics n
  JOIN health_metrics a ON a.recorded_date = n.recorded_date AND a.metric_name = 'active_energy_burned'
  JOIN health_metrics b ON b.recorded_date = n.recorded_date AND b.metric_name = 'basal_energy_burned'
  WHERE n.metric_name = 'dietary_energy_consumed'
  ORDER BY n.recorded_date DESC LIMIT 14;

When giving health advice:
- For weight goal context: user is working toward a healthy weight gain (underweight goal)
- Caloric surplus = dietary_energy_consumed - (active_energy_burned + basal_energy_burned)
- HRV below personal baseline = flag recovery concern before recommending hard training
- Sleep < 7h or efficiency < 85% = note impact on recovery and hormone levels
- Cross-reference nutrition + workouts + weight for weekly check-ins
```

---

## What Claude Can Now Do

- *"Am I in a caloric surplus this week?"* — joins nutrition + activity data
- *"How's my sleep been since I started training harder?"* — correlates sleep_analysis with workouts
- *"What's my HRV trend — am I recovered enough to train today?"*
- *"Show my weight trend for the last 6 weeks"*
- *"My sodium has been high this week — could that be affecting my weight readings?"*
- *"How much protein am I averaging vs my target?"*