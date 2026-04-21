#!/usr/bin/env python3
"""
Jarvis — Todoist → Supabase sync.

Lands raw Todoist payloads (active + completed tasks) into raw_todoist.
The staging view (tasks) handles all transformation — this script just dumps JSON.

Usage:
    python sync_todoist.py                         # Full sync
    python sync_todoist.py --dry-run               # Preview counts, no writes
    python sync_todoist.py --active-only           # Skip completed tasks
    python sync_todoist.py --completed-only        # Skip active tasks
    python sync_todoist.py --completed-limit 500   # Fetch more completed history
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

TODOIST_API_BASE        = "https://api.todoist.com/api/v1"
DEFAULT_COMPLETED_LIMIT = 200
TODOIST_PAGE_SIZE       = 200   # items per page (Todoist v1 max)
BATCH_SIZE              = 100   # chunk size for Supabase upserts
RETRY_ATTEMPTS          = 3
RETRY_BACKOFF           = 1.5   # seconds; doubled on each retry


# ── Env helpers ───────────────────────────────────────────────────────────────

def require_env(key: str) -> str:
    """Return env var or exit with a clear message."""
    val = os.environ.get(key, "").strip()
    if not val:
        console.print(f"[red]✗[/red] Missing required env var: [bold]{key}[/bold]")
        console.print("  Add it to your [cyan].env[/cyan] file and try again.\n")
        sys.exit(1)
    return val


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get_json(url: str, headers: dict, params: dict | None = None) -> any:
    """GET with retry + exponential backoff. Returns parsed JSON."""
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


def post_json(url: str, headers: dict, body: any, params: dict | None = None) -> requests.Response:
    """POST with retry + exponential backoff."""
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

def _fetch_task_pages(headers: dict, params: dict | None = None, max_results: int | None = None) -> list[dict]:
    """
    Paginate through the /tasks endpoint using cursor pagination.
    Todoist v1 returns {"results": [...], "next_cursor": "..."}.
    """
    base_params = {**(params or {}), "limit": TODOIST_PAGE_SIZE}
    results: list[dict] = []
    cursor: str | None = None

    while True:
        if cursor:
            base_params["cursor"] = cursor
        data = get_json(f"{TODOIST_API_BASE}/tasks", headers=headers, params=base_params)
        page = data.get("results", []) if isinstance(data, dict) else []
        results.extend(page)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor or (max_results is not None and len(results) >= max_results):
            break

    return results[:max_results] if max_results is not None else results


def fetch_active_tasks(headers: dict) -> list[dict]:
    return _fetch_task_pages(headers)


def fetch_completed_tasks(headers: dict, limit: int) -> list[dict]:
    return _fetch_task_pages(headers, params={"filter": "completed"}, max_results=limit)


# ── Transform ─────────────────────────────────────────────────────────────────

def to_landing_record(task: dict, *, is_completed: bool = False) -> dict:
    """Wrap a raw Todoist task into a raw_todoist row."""
    task_id = str(task.get("id"))   # v1 API: same field for active and completed
    return {
        "todoist_id": task_id,
        "payload": task,       # dict → Supabase serialises as JSONB
        "is_completed": is_completed,
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
    url = f"{supabase_url}/rest/v1/raw_todoist"
    total = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        resp = post_json(url, upsert_headers, batch, params={"on_conflict": "todoist_id"})
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
            "source": "todoist",
            "records_synced": count,
            "status": status,
            "error_message": error,
        },
    )


# ── Output ────────────────────────────────────────────────────────────────────

def build_summary_table(
    active: list[dict],
    completed: list[dict],
    upserted: int,
    elapsed: float,
    dry_run: bool,
) -> Panel:
    today = datetime.now(timezone.utc).date().isoformat()
    priority_labels = {4: "high", 3: "medium", 2: "low", 1: "low"}
    priority_counts = {"high": 0, "medium": 0, "low": 0}
    due_today = 0
    overdue   = 0

    for task in active:
        label = priority_labels.get(task.get("priority", 1), "low")
        priority_counts[label] += 1
        due = task.get("due")
        if due:
            date_str = (due.get("date") or "")[:10]
            if date_str == today:
                due_today += 1
            elif date_str and date_str < today:
                overdue += 1

    tbl = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    tbl.add_column("", style="dim", min_width=22)
    tbl.add_column("", justify="right", min_width=6)

    tbl.add_row("[bold]Active tasks[/bold]", f"[bold]{len(active)}[/bold]")
    tbl.add_row("  High priority",   f"[red]{priority_counts['high']}[/red]")
    tbl.add_row("  Medium priority", f"[yellow]{priority_counts['medium']}[/yellow]")
    tbl.add_row("  Low priority",    f"[dim]{priority_counts['low']}[/dim]")
    tbl.add_row("  Due today",       f"[cyan]{due_today}[/cyan]")
    tbl.add_row(
        "  Overdue",
        f"[bold red]{overdue}[/bold red]" if overdue else "[dim]0[/dim]",
    )
    tbl.add_row("", "")
    tbl.add_row("[bold]Completed (fetched)[/bold]", f"[bold]{len(completed)}[/bold]")
    tbl.add_row("[bold]Total upserted[/bold]",      f"[bold]{upserted}[/bold]")
    tbl.add_row("Elapsed",                          f"[dim]{elapsed:.1f}s[/dim]")

    mode_label = (
        " [on yellow][black] DRY RUN [/black][/on yellow]"
        if dry_run
        else " [green]✓ written to Supabase[/green]"
    )
    return Panel(tbl, title=f"[bold blue]Jarvis — Todoist sync[/bold blue]{mode_label}", border_style="blue")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Todoist tasks to Jarvis Supabase database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run",         action="store_true", help="Fetch but don't write to Supabase")
    parser.add_argument("--active-only",     action="store_true", help="Skip completed tasks")
    parser.add_argument("--completed-only",  action="store_true", help="Skip active tasks")
    parser.add_argument(
        "--completed-limit",
        type=int,
        default=DEFAULT_COMPLETED_LIMIT,
        metavar="N",
        help=f"Max completed tasks to fetch (default: {DEFAULT_COMPLETED_LIMIT})",
    )
    args = parser.parse_args()

    todoist_key   = require_env("TODOIST_API_KEY")
    supabase_url  = require_env("SUPABASE_URL").rstrip("/")
    supabase_key  = require_env("SUPABASE_SERVICE_KEY")

    todoist_headers  = {"Authorization": f"Bearer {todoist_key}"}
    supabase_headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type":  "application/json",
    }

    if args.dry_run:
        console.print("[on yellow][black] DRY RUN [/black][/on yellow] fetching data — nothing will be written\n")

    active_tasks: list[dict]    = []
    completed_tasks: list[dict] = []
    start = time.monotonic()

    try:
        if not args.completed_only:
            with console.status("[blue]Fetching active tasks…[/blue]"):
                active_tasks = fetch_active_tasks(todoist_headers)
            console.print(f"  [green]✓[/green] Active tasks fetched:    [bold]{len(active_tasks)}[/bold]")

        if not args.active_only:
            with console.status(f"[blue]Fetching completed tasks (limit {args.completed_limit})…[/blue]"):
                completed_tasks = fetch_completed_tasks(todoist_headers, args.completed_limit)
            console.print(f"  [green]✓[/green] Completed tasks fetched:  [bold]{len(completed_tasks)}[/bold]")

        # Build a dict keyed by todoist_id so duplicates across active/completed
        # are collapsed. Completed records overwrite active ones when both exist.
        records_by_id: dict[str, dict] = {}
        for t in active_tasks:
            rec = to_landing_record(t, is_completed=False)
            records_by_id[rec["todoist_id"]] = rec
        for t in completed_tasks:
            rec = to_landing_record(t, is_completed=True)
            records_by_id[rec["todoist_id"]] = rec
        all_records = list(records_by_id.values())

        action = "Previewing" if args.dry_run else "Upserting"
        with console.status(f"[blue]{action} {len(all_records)} records…[/blue]"):
            upserted = upsert_records(all_records, supabase_url, supabase_headers, dry_run=args.dry_run)
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
    console.print(build_summary_table(active_tasks, completed_tasks, upserted, elapsed, args.dry_run))


if __name__ == "__main__":
    main()
