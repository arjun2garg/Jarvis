# Jarvis MVP — Implementation Guide

## What We're Building

A Supabase database that consolidates your life data, starting with tasks synced from Todoist. Claude reads/writes via the Supabase MCP integration, and a simple web dashboard gives you a visual view. Everything is designed so adding more data sources later (Hevy, Apple Health, Cronometer, Apple Calendar) is just adding a landing table, a staging view, and a sync script.

```
You ↔ Claude (Max Plan) ↔ Supabase MCP ↔ Staging Views (clean, queryable)
                                                  ↑
                                          Landing Tables (raw JSON)
                                                  ↑
                                            Sync Scripts
                                                  ↑
                                            Your Apps
```

### Data Architecture

Every data source follows a two-layer pattern:

**Landing table** — stores the raw API payload as JSONB, exactly as the source returns it. No field mapping, no transformation. This is your immutable record of what the source said at sync time.

**Staging view** — a Postgres view that reads from the landing table and transforms the raw JSON into clean, typed, queryable columns. Claude and the dashboard read from this layer. If you want to extract a new field or change how priorities map, you update the view definition — no re-sync, no data migration.

This separation matters because you never lose source data, sync scripts stay dead simple (just dump JSON), and all transformation logic lives in SQL where it's easy to test and iterate.

---

## Step 1: Supabase Project Setup

1. Go to [supabase.com](https://supabase.com) and create a free account
2. Create a new project (name it `jarvis`, pick a strong database password, choose the region nearest you)
3. Wait ~2 minutes for it to provision
4. Save these values somewhere (you'll need them for scripts and the dashboard):
   - **Project URL**: `Settings > API > Project URL`
   - **Anon public key**: `Settings > API > Project API keys > anon public`
   - **Service role key**: `Settings > API > Project API keys > service_role` (keep this secret — it's for sync scripts only)

## Step 2: Database Schema

Run this in Supabase's SQL Editor (`SQL Editor` in the left sidebar). This is everything you need for the MVP — nothing more.

```sql
-- ============================================================
-- LANDING: Raw Todoist data, stored exactly as the API returns it
-- ============================================================
CREATE TABLE raw_todoist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todoist_id TEXT UNIQUE NOT NULL,
    payload JSONB NOT NULL,
    is_completed BOOLEAN DEFAULT false,
    synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_raw_todoist_id ON raw_todoist(todoist_id);


-- ============================================================
-- STAGING: Clean, typed view that Claude and the dashboard read
-- ============================================================
CREATE VIEW tasks AS
SELECT
    r.id,
    r.payload->>'content'                       AS title,
    r.payload->>'description'                   AS description,
    CASE WHEN r.is_completed THEN 'completed'
         ELSE 'active' END                      AS status,
    CASE (r.payload->>'priority')::int
        WHEN 4 THEN 'high'
        WHEN 3 THEN 'medium'
        ELSE 'low'
    END                                         AS priority,
    (r.payload->'due'->>'date')::date           AS due_date,
    r.payload->>'url'                           AS source_url,
    r.payload->'labels'                         AS labels,
    r.payload->>'project_id'                    AS project_id,
    r.todoist_id                                AS source_id,
    'todoist'                                   AS source,
    r.synced_at
FROM raw_todoist r;


-- ============================================================
-- SYNC LOG: Track when syncs run and whether they succeeded
-- ============================================================
CREATE TABLE sync_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    synced_at TIMESTAMPTZ DEFAULT now(),
    records_synced INT,
    status TEXT DEFAULT 'success',
    error_message TEXT
);
```

That's the entire schema — one landing table, one staging view, one utility table.

### How This Extends Later

When you're ready to add a new data source, the pattern is always the same:

```sql
-- Example: adding Hevy
CREATE TABLE raw_hevy (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hevy_id TEXT UNIQUE NOT NULL,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE VIEW workouts AS
SELECT
    r.id,
    (r.payload->>'start_time')::timestamptz     AS workout_date,
    r.payload->>'name'                          AS workout_name,
    -- ... extract whatever fields you need
    r.payload->'exercises'                      AS exercises,
    r.hevy_id                                   AS source_id,
    'hevy'                                      AS source,
    r.synced_at
FROM raw_hevy r;
```

Each source gets a landing table. Each domain gets a staging view. The sync log tracks them all.


## Step 3: Todoist Sync Script

Get your Todoist API token: `Todoist > Settings > Integrations > Developer > API token`

Create this script in your jarvis project repo:

```python
#!/usr/bin/env python3
"""
Sync Todoist tasks to Jarvis Supabase database.

Lands raw API payloads into raw_todoist. The staging view (tasks)
handles all transformation — this script just dumps JSON.

Usage:
    export TODOIST_API_KEY="your_token"
    export SUPABASE_URL="https://xyz.supabase.co"
    export SUPABASE_SERVICE_KEY="your_service_role_key"
    python sync_todoist.py
"""

import os
import json
import requests

# --- Config ---
TODOIST_API = "https://api.todoist.com/rest/v2"
TODOIST_KEY = os.environ["TODOIST_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

TODOIST_HEADERS = {"Authorization": f"Bearer {TODOIST_KEY}"}
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}


def fetch_active_tasks():
    """Fetch all active tasks from Todoist."""
    resp = requests.get(f"{TODOIST_API}/tasks", headers=TODOIST_HEADERS)
    resp.raise_for_status()
    return resp.json()


def fetch_completed_tasks():
    """Fetch recently completed tasks from Todoist Sync API."""
    resp = requests.get(
        "https://api.todoist.com/sync/v9/completed/get_all",
        headers=TODOIST_HEADERS,
        params={"limit": 50}
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def to_landing_record(task, is_completed=False):
    """Convert to a raw_todoist landing record — just the payload."""
    task_id = str(task["task_id"] if is_completed else task["id"])
    return {
        "todoist_id": task_id,
        "payload": json.dumps(task),
        "is_completed": is_completed,
    }


def upsert_to_supabase(records):
    """Upsert records into raw_todoist."""
    if not records:
        return 0

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/raw_todoist",
        headers=SUPABASE_HEADERS,
        params={"on_conflict": "todoist_id"},
        json=records
    )

    if resp.status_code not in (200, 201):
        print(f"Error upserting: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    return len(records)


def log_sync(count, status="success", error=None):
    """Log the sync result."""
    requests.post(
        f"{SUPABASE_URL}/rest/v1/sync_log",
        headers=SUPABASE_HEADERS,
        json={
            "source": "todoist",
            "records_synced": count,
            "status": status,
            "error_message": error
        }
    )


def main():
    try:
        active = fetch_active_tasks()
        active_records = [to_landing_record(t) for t in active]

        completed = fetch_completed_tasks()
        completed_records = [to_landing_record(t, is_completed=True)
                            for t in completed]

        all_records = active_records + completed_records
        count = upsert_to_supabase(all_records)

        log_sync(count)
        print(f"Synced {count} tasks ({len(active_records)} active, "
              f"{len(completed_records)} completed)")

    except Exception as e:
        log_sync(0, status="error", error=str(e))
        print(f"Sync failed: {e}")
        raise


if __name__ == "__main__":
    main()
```

**Test it:**
```bash
pip install requests
export TODOIST_API_KEY="your_token"
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_SERVICE_KEY="your_service_role_key"
python sync_todoist.py
```

Check the Supabase Table Editor — `raw_todoist` should have your raw payloads. Then query the `tasks` view to see the clean version:

```sql
SELECT title, status, priority, due_date
FROM tasks
WHERE status = 'active'
ORDER BY
    CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
    due_date NULLS LAST;
```


## Step 4: Connect Claude

### 4a. Connect the Supabase MCP Integration

In Claude (claude.ai, Max plan):
1. Go to the integrations menu
2. Find and connect **Supabase**
3. Authenticate with your Supabase account
4. Select your `jarvis` project

Claude can now query your database directly through conversation.

### 4b. Create a Claude Project

Go to `Projects` in Claude and create a project called **Jarvis**. Add these custom instructions:

```
You are Jarvis, my personal AI assistant. You have access to my life
database in Supabase (project: jarvis).

## Database Architecture

Data follows a landing/staging pattern:
- Landing tables (raw_*) store raw API payloads as JSONB
- Staging views provide clean, typed columns for querying

ALWAYS query the staging views, not the landing tables, unless I
specifically ask about raw data or debugging a sync.

Current staging views:
- **tasks**: title, description, status (active/completed), priority
  (high/medium/low), due_date, labels, project_id, source_url

Utility tables:
- **sync_log**: source, synced_at, records_synced, status, error_message

## How to Help Me

I struggle with traditional productivity tools. Keep things LOW FRICTION.

- When I say "morning" or "briefing": query active tasks sorted by
  priority then due date (max 5). Give me a concise rundown.
- When I say "what's overdue" or "what did I miss": query tasks where
  due_date < today and status = 'active'.
- When I ask to add a task: tell me you can't write to the tasks view
  directly, but suggest I add it in Todoist where it'll sync
  automatically. (Or, if I insist, you can INSERT into raw_todoist
  with a manual payload.)
- When I ask about sync status: query sync_log ORDER BY synced_at
  DESC LIMIT 5.
- Be concise. No essays.

## Key Preferences
- Timezone: America/Los_Angeles (adjust this)
- Max 5 tasks shown at a time unless I ask for more
- Encouraging, not guilt-trippy
- When in doubt, just do it — don't ask me 5 clarifying questions
```

Now open that project and try: "What tasks do I have this week?" or "What's overdue?"


## Step 5: Dashboard

Use Claude Code to generate a dashboard. Here's what to ask for:

```
Build me a single-page React dashboard (Vite + Tailwind) that connects
to my Supabase database and shows:

1. Active tasks sorted by priority then due date
2. Completed tasks in the last 7 days
3. Last sync time and status

It should query the "tasks" view for task data and the "sync_log" table
for sync status.

Use these env vars:
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your_anon_key

Keep it clean and minimal. Single page, no routing. Dark mode.
Use @supabase/supabase-js to query the database.
```

Claude Code can build this in one shot. Deploy it for free on Vercel or Netlify, or just run it locally with `npm run dev`.

### Supabase Row Level Security Note

For the dashboard to read data with the anon key, you need to enable RLS and add read policies. Run this in the SQL Editor:

```sql
-- Enable RLS on tables (views inherit from their base tables)
ALTER TABLE raw_todoist ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_log ENABLE ROW LEVEL SECURITY;

-- Since this is a personal dashboard (single user), allow all reads
-- with the anon key. Writes use the service_role key in sync scripts.
CREATE POLICY "Allow anonymous read" ON raw_todoist FOR SELECT USING (true);
CREATE POLICY "Allow anonymous read" ON sync_log FOR SELECT USING (true);
```


## Step 6: Automate the Sync

Set up a cron job or GitHub Action to run the Todoist sync automatically.

**Option A: Local cron (simplest)**
```bash
crontab -e
# Run every 30 minutes
*/30 * * * * cd /path/to/jarvis && /usr/bin/python3 sync_todoist.py >> /tmp/jarvis-sync.log 2>&1
```

**Option B: GitHub Actions (works when your computer is off)**
```yaml
# .github/workflows/sync.yml
name: Sync Todoist
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install requests
      - run: python sync_todoist.py
        env:
          TODOIST_API_KEY: ${{ secrets.TODOIST_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

---

## Adding More Data Sources

Each new source follows the same pattern: landing table → staging view → sync script. Here's a preview of what the next integrations look like.

### Hevy (Fitness)

```sql
CREATE TABLE raw_hevy (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hevy_id TEXT UNIQUE NOT NULL,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE VIEW workouts AS
SELECT
    r.id,
    (r.payload->>'start_time')::timestamptz     AS workout_date,
    r.payload->>'name'                          AS workout_name,
    (r.payload->>'duration_seconds')::int / 60  AS duration_min,
    r.payload->'exercises'                      AS exercises,
    r.hevy_id                                   AS source_id,
    'hevy'                                      AS source,
    r.synced_at
FROM raw_hevy r;
```

Claude can then query JSONB to answer questions like "What's my bench press trend?" by drilling into the exercises array.

### Apple Health

Landing table for the XML export, staging views for specific metrics (weight, sleep, steps, heart rate). Export from iPhone, parse with a Python script, land as JSONB records.

### Cronometer (Nutrition)

CSV export → parse → land into `raw_cronometer` → staging view `nutrition` with macro columns.

### Apple Calendar

Export `.ics` file → parse with Python's `icalendar` library → land into `raw_calendar` → staging view `events`.

---

## Future Enhancements

### Custom MCP Server (Month 2-3)

Once you outgrow the Supabase MCP's built-in capabilities, build a thin MCP server that wraps your database with domain-specific tools like `get_daily_briefing()` or `log_activity(natural_language)`.

### Analytics Views (Month 2)

As more data flows in, create Postgres views for common cross-domain analyses — habit completion rates, workout frequency, health correlations. These let Claude answer complex questions with simple queries.

### ML / Pattern Analysis (Month 3+)

With data in Postgres, you can connect pandas, scikit-learn, Jupyter, or Supabase Edge Functions. The landing/staging pattern means you always have the raw data available for feature extraction, even for fields you didn't think to extract in the original staging view.

---

## Quick-Start Checklist

```
[ ] Create Supabase account + project
[ ] Run the schema SQL (raw_todoist, tasks view, sync_log)
[ ] Get your Todoist API token
[ ] Run the sync script — verify data in raw_todoist and tasks view
[ ] Set up RLS policies for dashboard access
[ ] Connect Supabase MCP in Claude (Max plan)
[ ] Create Claude Project with Jarvis instructions
[ ] Test: ask Claude "what are my active tasks?"
[ ] Use Claude Code to build a dashboard
[ ] Set up automated sync (cron or GitHub Actions)
```

Total time to a working MVP: ~1-2 hours.