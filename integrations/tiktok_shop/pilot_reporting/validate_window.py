#!/usr/bin/env python3
"""
Section 25 — SECOND VALIDATION WINDOW.

Lighter, multi-day smoke check (2026-09-01 -> 2026-09-07 inclusive) to
validate pagination, multi-day analytics, order volume scaling, finance
matching proportion, and affiliate/video/live availability across a wider
window. Does NOT replace or alter the single-day 2026-09-07 CEO report —
this writes its own separate summary file only.

    python3 validate_window.py --start 2026-09-01 --end 2026-09-08
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from collect import (
    Logger, day_window_epoch, next_day_str, paginate,
)
from pilot_common import LOGS_DIR, PilotStopError, REPORTS_DIR, bootstrap_session, now_utc_iso


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="shop-local start date, inclusive, YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="shop-local end date, EXCLUSIVE, YYYY-MM-DD")
    args = ap.parse_args()

    log = Logger(LOGS_DIR / f"validate_{args.start}_to_{args.end}.log")
    log.log(f"=== SECOND VALIDATION WINDOW {args.start} -> {args.end} (exclusive) ===")

    try:
        session = bootstrap_session()
    except PilotStopError as e:
        log.log(f"STOP: {e}")
        print(str(e))
        return 1

    start_epoch, _ = day_window_epoch(args.start)
    _, end_epoch = day_window_epoch(
        (datetime.strptime(args.end, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    )

    result: dict = {"window_start": args.start, "window_end_exclusive": args.end, "checked_at_utc": now_utc_iso()}

    # Orders — full pagination across the whole window (report_date arg to
    # paginate()/write_raw() is just a filename prefix here; use the window
    # label so these snapshots don't collide with the single-day pilot's
    # raw/2026-09-07_*.json files).
    window_label = f"validation_{args.start}_{args.end}"
    order_rows, order_status = paginate(
        log, session, "orders", window_label, "orders",
        query_base={"page_size": "50"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
        max_pages=100,
    )
    result["orders"] = {"status": order_status, "count": len(order_rows)}

    returns_rows, returns_status = paginate(
        log, session, "return_refund", window_label, "returns",
        query_base={"page_size": "50", "sort_field": "create_time", "sort_order": "DESC"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
        max_pages=50,
    )
    result["returns"] = {"status": returns_status, "count": len(returns_rows)}

    affiliate_rows, affiliate_status = paginate(
        log, session, "affiliate", window_label, "affiliate_orders",
        query_base={"page_size": "50"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
        max_pages=50,
    )
    result["affiliate"] = {"status": affiliate_status, "count": len(affiliate_rows)}

    product_rows, product_status = paginate(
        log, session, "product_analytics", window_label, "product_analytics",
        query_base={"start_date_ge": args.start, "end_date_lt": args.end,
                    "currency": "LOCAL", "page_size": "100"},
        max_pages=50,
    )
    result["product_analytics"] = {"status": product_status, "count": len(product_rows)}

    video_rows, video_status = paginate(
        log, session, "video_analytics", window_label, "video_analytics",
        query_base={"start_date_ge": args.start, "end_date_lt": args.end,
                    "currency": "LOCAL", "page_size": "100"},
        max_pages=100,
    )
    result["video_analytics"] = {"status": video_status, "count": len(video_rows)}

    live_rows, live_status = paginate(
        log, session, "live_analytics", window_label, "live_analytics",
        query_base={"start_date_ge": args.start, "end_date_lt": args.end,
                    "currency": "LOCAL", "page_size": "100"},
        max_pages=50,
    )
    result["live_analytics"] = {"status": live_status, "count": len(live_rows)}

    finance_unsettled_rows, unsettled_status = paginate(
        log, session, "finance_unsettled", window_label, "finance_unsettled",
        query_base={"page_size": "50", "sort_field": "order_create_time", "sort_order": "DESC"},
        max_pages=20,
    )
    result["finance_unsettled_recent"] = {"status": unsettled_status, "count": len(finance_unsettled_rows)}

    out_path = REPORTS_DIR / f"validation_window_{args.start}_to_{args.end}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.log(f"=== VALIDATION WINDOW done: {json.dumps(result)} ===")
    print(json.dumps(result, indent=2))
    print(f"\nWritten: {out_path}")
    print("\nNote: this is a scale/pagination check only — the single-day CEO "
          "report for 2026-09-07 is unaffected and unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
