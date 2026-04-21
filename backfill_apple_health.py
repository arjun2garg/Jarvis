#!/usr/bin/env python3
"""
Backfill historical Apple Health data into raw_apple_health.

Health Auto Export's "Manual Export" dumps raw interval data (e.g. hundreds
of step_count points per day) regardless of the Daily-Summary automation
setting. Our schema is daily-grain (UNIQUE on metric_name + recorded_date),
so this script aggregates points per day, normalizes metric names + units
to the canonical scheme the edge function expects, then POSTs chunked
payloads to ingest-health. Re-runs are safe — the edge function upserts.

Usage:
    HEALTH_WEBHOOK_SECRET=... python backfill_apple_health.py <export.json>

The script logs only counts, never values — safe to run with a transcript.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# Map export-side metric name → canonical (matches edge function's SCALAR_METRICS)
NAME_MAP = {
    "active_energy":          "active_energy_burned",
    "heart_rate_variability": "heart_rate_variability_sdnn",
}

SUM_METRICS = {"step_count", "active_energy_burned", "apple_exercise_time"}
AVG_METRICS = {
    "respiratory_rate",
    "heart_rate_variability_sdnn",
    "resting_heart_rate",
    "walking_heart_rate_average",
}
LAST_METRICS = {
    "body_mass_index",
    "weight_body_mass",
    "body_fat_percentage",
    "lean_body_mass",
}

LB_TO_KG = 1 / 2.2046226218

CHUNK_POINTS = 500  # daily points per POST
REQUEST_TIMEOUT = 120


def parse_day(s: str | None) -> str | None:
    return s[:10] if s and len(s) >= 10 else None


def aggregate_scalar(points: list[dict], strategy: str) -> dict[str, float]:
    by_day: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for p in points:
        day = parse_day(p.get("date"))
        qty = p.get("qty")
        if day is None or qty is None:
            continue
        by_day[day].append((p.get("date", ""), float(qty)))

    out: dict[str, float] = {}
    for day, items in by_day.items():
        vals = [q for _, q in items]
        if strategy == "sum":
            out[day] = sum(vals)
        elif strategy == "avg":
            out[day] = sum(vals) / len(vals)
        elif strategy == "last":
            items.sort(key=lambda x: x[0])
            out[day] = items[-1][1]
    return out


def aggregate_sleep(points: list[dict]) -> dict[str, dict[str, float]]:
    # Attribute to the wake-up day (end/endDate). Fall back to date.
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for p in points:
        day = parse_day(p.get("end") or p.get("endDate") or p.get("date"))
        if not day:
            continue
        stage = (p.get("value") or "").strip()
        hrs = p.get("qty") or 0
        if not stage:
            continue
        by_day[day][stage] += float(hrs)

    out: dict[str, dict[str, float]] = {}
    for day, stages in by_day.items():
        deep  = stages.get("Deep", 0.0)
        rem   = stages.get("REM", 0.0)
        core  = stages.get("Core", 0.0)
        awake = stages.get("Awake", 0.0)
        in_bed = stages.get("In Bed", deep + rem + core + awake)
        asleep = deep + rem + core
        out[day] = {
            "asleep":     round(asleep, 3),
            "inBed":      round(in_bed, 3),
            "asleepDeep": round(deep, 3),
            "asleepREM":  round(rem, 3),
            "asleepCore": round(core, 3),
            "awake":      round(awake, 3),
        }
    return out


def build_payload_metrics(input_path: Path) -> list[dict]:
    with input_path.open() as f:
        raw = json.load(f)

    metrics = raw["data"]["metrics"]
    payload: list[dict] = []

    for m in metrics:
        raw_name = m["name"]
        unit = (m.get("units") or "").strip()
        pts = m.get("data") or []

        if raw_name == "sleep_analysis":
            sleep = aggregate_sleep(pts)
            if sleep:
                data = [{"date": f"{d} 00:00:00 +0000", **fields}
                        for d, fields in sorted(sleep.items())]
                payload.append({"name": "sleep_analysis", "units": "hr", "data": data})
            continue

        canonical = NAME_MAP.get(raw_name, raw_name)

        if canonical in SUM_METRICS:
            agg = aggregate_scalar(pts, "sum")
        elif canonical in AVG_METRICS:
            agg = aggregate_scalar(pts, "avg")
        elif canonical in LAST_METRICS:
            agg = aggregate_scalar(pts, "last")
        else:
            print(f"  skip unknown metric: {raw_name}", file=sys.stderr)
            continue

        # Normalize body-mass units lb → kg
        if canonical in ("weight_body_mass", "lean_body_mass") and unit.lower() == "lb":
            agg = {d: v * LB_TO_KG for d, v in agg.items()}
            unit = "kg"

        data = [{"date": f"{d} 00:00:00 +0000", "qty": round(v, 3)}
                for d, v in sorted(agg.items())]
        payload.append({"name": canonical, "units": unit, "data": data})

    return payload


def post_chunks(payload_metrics: list[dict], url: str, secret: str) -> None:
    headers = {"Content-Type": "application/json", "x-webhook-secret": secret}
    total = 0

    for m in payload_metrics:
        name = m["name"]
        data = m["data"]
        if not data:
            continue
        chunks_sent = 0
        for i in range(0, len(data), CHUNK_POINTS):
            chunk = {"data": {"metrics": [{**m, "data": data[i:i + CHUNK_POINTS]}]}}
            resp = requests.post(url, headers=headers, json=chunk, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                print(f"  {name}: HTTP {resp.status_code} — aborting", file=sys.stderr)
                sys.exit(1)
            result = resp.json()
            total += int(result.get("upserted", 0))
            chunks_sent += 1
        print(f"  {name:<32}  {len(data):>4} days in {chunks_sent} chunk(s)")

    print(f"\nTotal upserted rows: {total}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python backfill_apple_health.py <export.json>")

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        sys.exit(f"File not found: {input_path}")

    supabase_url = os.environ.get("SUPABASE_URL")
    secret = os.environ.get("HEALTH_WEBHOOK_SECRET")
    if not supabase_url or not secret:
        sys.exit("SUPABASE_URL and HEALTH_WEBHOOK_SECRET env vars required")

    url = supabase_url.rstrip("/") + "/functions/v1/ingest-health"

    print(f"Reading {input_path.name} ({input_path.stat().st_size / 1e6:.0f} MB)…")
    payload = build_payload_metrics(input_path)
    total_days = sum(len(m["data"]) for m in payload)
    print(f"Aggregated into {total_days} daily rows across {len(payload)} metrics.\n")
    print("Posting to ingest-health:")
    post_chunks(payload, url, secret)


if __name__ == "__main__":
    main()
