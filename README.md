# Jarvis

Personal AI assistant backed by a Supabase database that consolidates life data.
Claude (Max plan) reads and writes via the Supabase MCP integration, turning
"what did I train this week?" or "what's overdue?" into a single question.


## Architecture

```
You ↔ Claude (Max plan) ↔ Supabase MCP ↔ Staging views  (clean, typed)
                                              ↑
                                         Landing tables  (raw JSONB)
                                              ↑
                                         Sync scripts
                                              ↑
                                         Source APIs
```

Every source follows the same two-layer pattern:

- **Landing table** (`raw_*`) — stores the raw API payload as JSONB, verbatim.
  Never lossy, never transformed. Sync scripts just dump JSON.
- **Staging view** — a Postgres view that reads from the landing table and
  exposes clean, typed, queryable columns. All transformation logic lives in
  SQL. Changing a view doesn't require a re-sync.

Why it matters: source data is immutable, sync scripts stay trivial, and
schema changes ship as view updates — no data migration.


## Quickstart

```bash
make setup                          # creates .venv, installs deps, scaffolds .env
cp .env.example .env && $EDITOR .env   # fill in API keys
make verify                         # checks env vars + API reachability + schema
make sync                           # runs every source
```

To apply the schema on a fresh Supabase project, see
[Schema management](#schema-management) below.


## Data sources

| Source  | Script            | Landing      | Staging views                 |
|---------|-------------------|--------------|-------------------------------|
| Todoist | `sync_todoist.py` | `raw_todoist`| `tasks`                       |
| Hevy    | `sync_hevy.py`    | `raw_hevy`   | `workouts`, `exercise_sets`   |

Planned: Apple Health, Cronometer (nutrition), Apple Calendar.


## Commands

```
make setup              Create .venv, install deps, scaffold .env
make verify             Check env vars + API connectivity

make sync               Run every source (Todoist + Hevy)
make sync-todoist       Full Todoist sync (active + completed)
make sync-hevy          Full Hevy workouts sync

make sync-dry-run       Preview Todoist sync — no writes
make sync-hevy-dry-run  Preview Hevy sync — no writes
make sync-active        Sync active Todoist tasks only
make sync-completed     Sync completed Todoist tasks only

make clean              Remove .venv and caches
```


## Schema management

Schemas live in `supabase/migrations/` and are applied via the Supabase CLI.

Prerequisites:

```bash
brew install supabase/tap/supabase   # or: npm i -g supabase
```

One-time link (requires a Personal Access Token from
[supabase.com/dashboard/account/tokens](https://supabase.com/dashboard/account/tokens),
exported as `SUPABASE_ACCESS_TOKEN`):

```bash
supabase link --project-ref <your-project-ref>
```

Create and apply a new migration:

```bash
supabase migration new add_whatever
$EDITOR supabase/migrations/<timestamp>_add_whatever.sql
supabase db query --linked -f supabase/migrations/<timestamp>_add_whatever.sql
supabase migration repair --status applied <timestamp>
```

(`db query --linked` uses the Management API so only the access token is
needed; the DB password is only required for `db pull` / `db push`.)


## Automation

Two GitHub Actions workflows, each running twice daily at 9am / 9pm
America/Chicago (`0 2,14 * * *` UTC during CDT — see the comment in each
workflow for the CST hour shift):

- `.github/workflows/todoist-sync.yml` — runs `sync_todoist.py`
- `.github/workflows/hevy-sync.yml` — runs `sync_hevy.py`

Required repository secrets:

- `TODOIST_API_KEY`
- `HEVY_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`


## Adding a new source

1. **Schema**: `supabase migration new <source>_schema` → write the landing
   table and staging view(s) → apply it (see [Schema management](#schema-management)).
2. **Sync script**: copy `sync_hevy.py` as a template. Keep it dumb — fetch,
   wrap each record as `{source_id, payload}`, batch-upsert, write to
   `sync_log`.
3. **Verify**: add the new env var, API probe, and resource checks to
   `verify_setup.py`.
4. **Make targets**: add `sync-<source>` and `sync-<source>-dry-run` to the
   `Makefile`; include the script in the top-level `sync` target.
5. **Cron**: add `.github/workflows/<source>-sync.yml` modeled on the
   existing ones.


## Repo layout

```
.
├── README.md
├── Makefile                         one-command entry points
├── requirements.txt
├── setup.sh                         bootstraps .venv
├── .env.example                     template for required API keys
├── verify_setup.py                  env + API + schema preflight
├── sync_todoist.py                  Todoist → raw_todoist
├── sync_hevy.py                     Hevy → raw_hevy
├── supabase/
│   ├── config.toml                  Supabase CLI project config
│   └── migrations/                  versioned DDL (source of truth)
├── .github/workflows/
│   ├── todoist-sync.yml             cron: */30 min
│   └── hevy-sync.yml                cron: */30 min
└── docs/
    ├── mvp.md                       original Todoist build guide
    └── hevy.md                      Hevy integration guide
```


## Docs

- [docs/mvp.md](docs/mvp.md) — original MVP implementation guide
  (architecture, Todoist setup, Claude Project wiring)
- [docs/hevy.md](docs/hevy.md) — Hevy schema + sync guide
