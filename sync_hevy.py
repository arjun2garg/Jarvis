#!/usr/bin/env python3
"""
Jarvis — Hevy → Supabase sync.

Lands raw Hevy workout payloads into raw_hevy. The staging views
(workouts, exercise_sets) handle all transformation — this script
just dumps JSON.

Usage:
    python sync_hevy.py                     # Full sync — all pages
    python sync_hevy.py --dry-run           # Preview counts, no writes
    python sync_hevy.py --max-pages 1       # Stop after N pages (debug)
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()

console = Console()

# ── Constants ─────────────────────────────────────────────────────────────────

HEVY_API_BASE  = "https://api.hevyapp.com/v1"
HEVY_PAGE_SIZE = 10          # Hevy's documented max
BATCH_SIZE     = 100         # chunk size for Supabase upserts
RETRY_ATTEMPTS = 3
RETRY_BACKOFF  = 1.5         # seconds; doubled on each retry


# ── Env helpers ───────────────────────────────────────────────────────────────

def require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        console.print(f"[red]✗[/red] Missing required env var: [bold]{key}[/bold]")
        console.print("  Add it to your [cyan].env[/cyan] file and try again.\n")
        sys.exit(1)
    return val


# ── HTTP helpers (retry + backoff) ────────────────────────────────────────────

def get_json(url: str, headers: dict, params: dict | None = None) -> dict:
    params = params or {}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                console.print(f"  [yellow]Rate limited — waiting {wait}s[/yellow]")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            wait = RETRY_BACKOFF * (2 ** attempt)
            console.print(f"  [yellow]Attempt {attempt + 1} failed ({exc}). Retrying in {wait:.0f}s…[/yellow]")
            time.sleep(wait)


def post_json(url: str, headers: dict, body, params: dict | None = None) -> requests.Response:
    params = params or {}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.post(url, headers=headers, params=params, json=body, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF * (2 ** attempt))


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all_workouts(headers: dict, *, max_pages: int | None = None) -> list[dict]:
    """
    Paginate through /v1/workouts. Hevy returns:
        { "page": int, "page_count": int, "workouts": [...] }
    """
    workouts: list[dict] = []
    page = 1

    while True:
        data = get_json(
            f"{HEVY_API_BASE}/workouts",
            headers=headers,
            params={"page": page, "pageSize": HEVY_PAGE_SIZE},
        )
        batch = data.get("workouts", []) if isinstance(data, dict) else []
        workouts.extend(batch)

        page_count = data.get("page_count", 1) if isinstance(data, dict) else 1
        if page >= page_count:
            break
        if max_pages is not None and page >= max_pages:
            break
        page += 1

    return workouts


# ── Transform ─────────────────────────────────────────────────────────────────

def to_landing_record(workout: dict) -> dict:
    """Wrap a raw Hevy workout into a raw_hevy row."""
    return {
        "hevy_id": str(workout["id"]),
        "payload": workout,   # dict → Supabase serialises as JSONB
    }


# ── Write ─────────────────────────────────────────────────────────────────────

def upsert_records(
    records: list[dict],
    supabase_url: str,
    supabase_headers: dict,
    *,
    dry_run: bool,
) -> int:
    if not records:
        return 0
    if dry_run:
        return len(records)

    upsert_headers = {
        **supabase_headers,
        "Prefer": "resolution=merge-duplicates",
    }
    url = f"{supabase_url}/rest/v1/raw_hevy"
    total = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        resp = post_json(url, upsert_headers, batch, params={"on_conflict": "hevy_id"})
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Supabase upsert failed [{resp.status_code}]: {resp.text[:400]}"
            )
        total += len(batch)

    return total


def log_sync(
    supabase_url: str,
    supabase_headers: dict,
    *,
    count: int,
    status: str,
    error: str | None = None,
) -> None:
    post_json(
        f"{supabase_url}/rest/v1/sync_log",
        supabase_headers,
        {
            "source": "hevy",
            "records_synced": count,
            "status": status,
            "error_message": error,
        },
    )


# ── Output ────────────────────────────────────────────────────────────────────

def build_summary_table(
    workouts: list[dict],
    upserted: int,
    elapsed: float,
    dry_run: bool,
) -> Panel:
    today = datetime.now(timezone.utc).date().isoformat()
    this_week = 0
    most_recent_title: str | None = None
    most_recent_started_at: str | None = None
    total_sets = 0

    # Hevy returns workouts newest-first by default; the first element
    # is the most recent one.
    if workouts:
        first = workouts[0]
        most_recent_title = first.get("title") or "(untitled)"
        most_recent_started_at = (first.get("start_time") or "")[:10]

    now = datetime.now(timezone.utc)
    for w in workouts:
        start = w.get("start_time")
        if start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if (now - dt).days < 7:
                    this_week += 1
            except ValueError:
                pass
        for ex in w.get("exercises") or []:
            total_sets += len(ex.get("sets") or [])

    tbl = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    tbl.add_column("", style="dim", min_width=24)
    tbl.add_column("", justify="right", min_width=6)

    tbl.add_row("[bold]Workouts fetched[/bold]",  f"[bold]{len(workouts)}[/bold]")
    tbl.add_row("  Last 7 days",                  f"[cyan]{this_week}[/cyan]")
    tbl.add_row("  Total sets",                   f"{total_sets}")
    if most_recent_title:
        tbl.add_row(
            "  Most recent",
            f"[dim]{most_recent_started_at}[/dim] {most_recent_title[:32]}",
        )
    tbl.add_row("", "")
    tbl.add_row("[bold]Total upserted[/bold]",    f"[bold]{upserted}[/bold]")
    tbl.add_row("Elapsed",                        f"[dim]{elapsed:.1f}s[/dim]")

    mode_label = (
        " [on yellow][black] DRY RUN [/black][/on yellow]"
        if dry_run
        else " [green]✓ written to Supabase[/green]"
    )
    return Panel(tbl, title=f"[bold blue]Jarvis — Hevy sync[/bold blue]{mode_label}", border_style="blue")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Hevy workouts to Jarvis Supabase database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run",   action="store_true", help="Fetch but don't write to Supabase")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Stop after fetching N pages (10 workouts/page). Default: all pages.",
    )
    args = parser.parse_args()

    hevy_key     = require_env("HEVY_API_KEY")
    supabase_url = require_env("SUPABASE_URL").rstrip("/")
    supabase_key = require_env("SUPABASE_SERVICE_KEY")

    hevy_headers = {
        "api-key": hevy_key,
        "accept":  "application/json",
    }
    supabase_headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type":  "application/json",
    }

    if args.dry_run:
        console.print("[on yellow][black] DRY RUN [/black][/on yellow] fetching data — nothing will be written\n")

    workouts: list[dict] = []
    upserted = 0
    start = time.monotonic()

    try:
        with console.status("[blue]Fetching workouts from Hevy…[/blue]"):
            workouts = fetch_all_workouts(hevy_headers, max_pages=args.max_pages)
        console.print(f"  [green]✓[/green] Workouts fetched:  [bold]{len(workouts)}[/bold]")

        records = [to_landing_record(w) for w in workouts]

        action = "Previewing" if args.dry_run else "Upserting"
        with console.status(f"[blue]{action} {len(records)} records…[/blue]"):
            upserted = upsert_records(records, supabase_url, supabase_headers, dry_run=args.dry_run)
        verb = "Would upsert" if args.dry_run else "Upserted"
        console.print(f"  [green]✓[/green] {verb}: [bold]{upserted}[/bold] records\n")

        if not args.dry_run:
            log_sync(supabase_url, supabase_headers, count=upserted, status="success")

    except Exception as exc:
        elapsed = time.monotonic() - start
        console.print(f"\n[bold red]✗ Sync failed[/bold red]: {exc}")
        if not args.dry_run:
            try:
                log_sync(supabase_url, supabase_headers, count=0, status="error", error=str(exc))
            except Exception:
                pass
        sys.exit(1)

    elapsed = time.monotonic() - start
    console.print(build_summary_table(workouts, upserted, elapsed, args.dry_run))


if __name__ == "__main__":
    main()
