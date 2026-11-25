#!/usr/bin/env python3
"""P15 — TikTok ads cost backfill (TikTok API for Business -> core.fact_ads_daily).

Dry-run by default: pulls from the API and prints per-day totals, writes
NOTHING. Writing to the production DB needs --write AND an approval id:

    python3 scripts/run_tiktok_ads_backfill.py --start 2026-08-01 --end 2026-11-24
    python3 scripts/run_tiktok_ads_backfill.py --start 2026-08-01 --end 2026-11-24 \
        --write --approval APR-...

Requires sql/066_STAGED_tiktok_ads_net_spend.sql applied first (net_spend).
Pulls in 30-day chunks; each chunk is its own transaction + etl_run_log
row, so an interruption keeps every completed chunk. Idempotent upsert —
safe to re-run. Does not touch control.etl_sync_state (the incremental
TIKTOK/ads domain owns that).
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipelines"))
sys.path.insert(0, str(ROOT / "integrations" / "tiktok_business"))

import ads_collector  # noqa: E402
import incr_common as ic  # noqa: E402

CHUNK_DAYS = 30


def chunks(start: date, end: date, size: int = CHUNK_DAYS):
    d = start
    while d <= end:
        e = min(end, d + timedelta(days=size - 1))
        yield d, e
        d = e + timedelta(days=1)


def tiktok_shop_id() -> str:
    sys.path.insert(0, str(ROOT / "integrations" / "tiktok_shop" / "pilot_reporting"))
    import pilot_common  # noqa: E402
    shop_id = (pilot_common.load_shop_info() or {}).get("shop_id")
    if not shop_id:
        raise SystemExit("TikTok shop_id not found (shop_info.json / TIKTOK_SHOP_INFO_JSON)")
    return str(shop_id)


def summarize(rows):
    by_day = defaultdict(lambda: defaultdict(lambda: 0))
    for r in rows:
        if r["attribution_basis"] == "COVERAGE":
            continue
        by_day[r["business_date"]][r["attribution_basis"]] += r["spend"] or 0
    return by_day


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--write", action="store_true", help="write to the production DB (needs --approval)")
    ap.add_argument("--approval", help="approval id naming this backfill (action + range)")
    a = ap.parse_args()
    if a.end < a.start:
        raise SystemExit("--end before --start")
    if a.write and not a.approval:
        raise SystemExit("--write requires --approval <id>")

    log = ic.simple_log("TIKTOK/ads-backfill")
    shop_id = tiktok_shop_id()
    creds = ads_collector.load_credentials()
    get = ads_collector.make_getter(creds, log)

    grand = defaultdict(lambda: 0)
    for s, e in chunks(a.start, a.end):
        rows = ads_collector.collect(get, creds, shop_id, s, e, log)
        for d, kinds in sorted(summarize(rows).items()):
            print(d.isoformat(), {k: int(v) for k, v in kinds.items()})
            for k, v in kinds.items():
                grand[k] += v
        if not a.write:
            continue
        conn = ic.get_db_conn()
        cur = conn.cursor()
        run_id = ic.start_run_log(cur, "TIKTOK", "ads", shop_id, run_type="backfill")
        try:
            result = ads_collector.upsert_rows(cur, run_id, rows, ic.Counters())
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            conn.close()
            ic.finish_run_log("TIKTOK", "ads", run_id, "fail", 0, f"{a.approval}: {exc}")
            raise
        conn.close()
        ic.finish_run_log("TIKTOK", "ads", run_id, "success", len(rows), "")
        log(f"chunk {s}..{e} written: {result} (approval {a.approval})")

    print("TOTAL", {k: int(v) for k, v in grand.items()}, "(dry-run, nothing written)" if not a.write else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
