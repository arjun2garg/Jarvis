#!/usr/bin/env python3
"""
Jarvis — verify setup.

Checks env vars, Todoist API connectivity, and Supabase table/view presence
before you run the sync for the first time.

Usage: python verify_setup.py
"""

import os
import sys

import requests
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table

load_dotenv()

console = Console()

REQUIRED_VARS = {
    "TODOIST_API_KEY":      "Todoist → Settings → Integrations → Developer",
    "HEVY_API_KEY":         "Hevy → https://hevy.com/settings?developer (requires Hevy Pro)",
    "SUPABASE_URL":         "Supabase → Settings → API → Project URL",
    "SUPABASE_SERVICE_KEY": "Supabase → Settings → API → service_role key",
    "SUPABASE_ANON_KEY":    "Supabase → Settings → API → anon public key",
}

SUPABASE_RESOURCES = [
    ("raw_todoist",    "landing table"),
    ("raw_hevy",       "landing table"),
    ("sync_log",       "utility table"),
    ("tasks",          "staging view"),
    ("workouts",       "staging view"),
    ("exercise_sets",  "staging view"),
]


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon  = "[green]✓[/green]" if ok else "[red]✗[/red]"
    extra = f"  [dim]{detail}[/dim]" if detail else ""
    console.print(f"  {icon}  {label}{extra}")
    return ok


def check_env_vars() -> bool:
    console.print("\n[bold]Environment variables[/bold]")
    all_ok = True
    for key, hint in REQUIRED_VARS.items():
        val = os.environ.get(key, "").strip()
        if val:
            masked = val[:6] + "…" + val[-4:] if len(val) > 12 else "***"
            all_ok &= check(key, True, masked)
        else:
            check(key, False, f"missing — find it at: {hint}")
            all_ok = False
    return all_ok


def check_todoist(api_key: str) -> bool:
    console.print("\n[bold]Todoist API[/bold]")
    try:
        resp = requests.get(
            "https://api.todoist.com/api/v1/tasks",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data  = resp.json()
            tasks = data.get("results", []) if isinstance(data, dict) else data
            return check("GET /api/v1/tasks", True, f"returned {len(tasks)} task(s)")
        elif resp.status_code == 401:
            return check("GET /api/v1/tasks", False, "401 Unauthorized — check your TODOIST_API_KEY")
        else:
            return check("GET /api/v1/tasks", False, f"HTTP {resp.status_code}")
    except requests.exceptions.ConnectionError:
        return check("GET /api/v1/tasks", False, "connection error — check your internet connection")
    except requests.exceptions.Timeout:
        return check("GET /api/v1/tasks", False, "timed out after 10s")


def check_hevy(api_key: str) -> bool:
    console.print("\n[bold]Hevy API[/bold]")
    try:
        resp = requests.get(
            "https://api.hevyapp.com/v1/workouts",
            headers={"api-key": api_key, "accept": "application/json"},
            params={"page": 1, "pageSize": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            page_count = data.get("page_count", 0) if isinstance(data, dict) else 0
            workouts = data.get("workouts", []) if isinstance(data, dict) else []
            detail = f"page_count={page_count}"
            if workouts:
                first = workouts[0]
                detail += f", most recent: {(first.get('title') or '(untitled)')[:30]}"
            return check("GET /v1/workouts", True, detail)
        elif resp.status_code in (401, 403):
            return check("GET /v1/workouts", False, f"{resp.status_code} — check HEVY_API_KEY (needs Hevy Pro)")
        else:
            return check("GET /v1/workouts", False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except requests.exceptions.ConnectionError:
        return check("GET /v1/workouts", False, "connection error — check your internet connection")
    except requests.exceptions.Timeout:
        return check("GET /v1/workouts", False, "timed out after 10s")


def check_supabase(url: str, service_key: str) -> bool:
    console.print("\n[bold]Supabase[/bold]")
    base_url = url.rstrip("/")
    headers = {
        "apikey":        service_key,
        "Authorization": f"Bearer {service_key}",
    }
    all_ok = True

    # Basic connectivity — try to hit the REST root
    try:
        resp = requests.get(f"{base_url}/rest/v1/", headers=headers, timeout=10)
        if resp.status_code in (200, 400):
            check("REST API reachable", True, base_url)
        else:
            all_ok &= check("REST API reachable", False, f"HTTP {resp.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        all_ok &= check("REST API reachable", False, "connection error — check SUPABASE_URL")
        return False

    # Check each required table/view
    for resource, kind in SUPABASE_RESOURCES:
        try:
            resp = requests.get(
                f"{base_url}/rest/v1/{resource}",
                headers=headers,
                params={"limit": "0"},
                timeout=10,
            )
            if resp.status_code == 200:
                all_ok &= check(f"{resource}  ({kind})", True)
            elif resp.status_code == 404:
                all_ok &= check(
                    f"{resource}  ({kind})",
                    False,
                    "not found — run the schema SQL in Supabase SQL Editor",
                )
            else:
                all_ok &= check(f"{resource}  ({kind})", False, f"HTTP {resp.status_code}")
        except requests.exceptions.RequestException as exc:
            all_ok &= check(f"{resource}  ({kind})", False, str(exc))

    return all_ok


def print_result(env_ok: bool, todoist_ok: bool, hevy_ok: bool, supabase_ok: bool) -> None:
    all_ok = env_ok and todoist_ok and hevy_ok and supabase_ok

    tbl = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    tbl.add_column("", min_width=20)
    tbl.add_column("", justify="center")

    rows = [
        ("Env vars",      env_ok),
        ("Todoist API",   todoist_ok),
        ("Hevy API",      hevy_ok),
        ("Supabase",      supabase_ok),
    ]
    for label, ok in rows:
        tbl.add_row(label, "[green]OK[/green]" if ok else "[red]FAIL[/red]")

    from rich.panel import Panel
    style  = "green" if all_ok else "red"
    title  = "[bold green]All checks passed — ready to sync[/bold green]" if all_ok else "[bold red]Some checks failed[/bold red]"
    console.print()
    console.print(Panel(tbl, title=title, border_style=style))

    if all_ok:
        console.print("\n  Run [cyan]make sync[/cyan] (or [cyan]python sync_todoist.py[/cyan]) to populate your database.\n")
    else:
        console.print("\n  Fix the issues above, then re-run [cyan]python verify_setup.py[/cyan]\n")


def main() -> None:
    console.print("[bold blue]Jarvis — setup verification[/bold blue]")

    env_ok      = check_env_vars()
    todoist_ok  = False
    hevy_ok     = False
    supabase_ok = False

    if env_ok:
        todoist_ok  = check_todoist(os.environ["TODOIST_API_KEY"])
        hevy_ok     = check_hevy(os.environ["HEVY_API_KEY"])
        supabase_ok = check_supabase(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    else:
        console.print("\n  [dim]Skipping API checks until env vars are set.[/dim]")

    print_result(env_ok, todoist_ok, hevy_ok, supabase_ok)
    sys.exit(0 if (env_ok and todoist_ok and hevy_ok and supabase_ok) else 1)


if __name__ == "__main__":
    main()
