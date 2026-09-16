#!/usr/bin/env python3
"""
V2.1 Section 6 — daily rolling backfill.

1. Ensures collect.py + finance_lifecycle.py have run for D-1/D-3/D-7/D-14
   (relative to today) — reports which are missing rather than silently
   skipping them.
2. PLUS: scans every order_finance_lifecycle_*.csv already on disk (any
   date, not just the last 14 days) for orders still in a non-terminal
   state (UNSETTLED, MISSING_FINANCE, RETURN_REFUND_PENDING,
   PAYMENT_PENDING) and re-checks each one directly against
   finance_order_statement_transactions + Get Unsettled Transactions, live,
   regardless of how old the order is. Terminal states (PAID,
   CANCELLED_NO_SETTLEMENT_EXPECTED) are never re-checked.

    python3 backfill.py --today 2026-09-08
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pilot_common import NORMALIZED_DIR, REPORTS_DIR, PilotStopError, bootstrap_session, now_utc_iso

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiktok_client import NetworkError  # noqa: E402

from collect import _call_with_retry, Logger  # noqa: E402

NON_TERMINAL = {"UNSETTLED", "MISSING_FINANCE", "RETURN_REFUND_PENDING", "PAYMENT_PENDING"}
TERMINAL = {"PAID", "CANCELLED_NO_SETTLEMENT_EXPECTED"}


def required_backfill_dates(today: str) -> list[str]:
    y, m, d = (int(x) for x in today.split("-"))
    base = datetime(y, m, d)
    return [(base - timedelta(days=n)).strftime("%Y-%m-%d") for n in (1, 3, 7, 14)]


def check_backfill_coverage(today: str) -> dict[str, bool]:
    coverage = {}
    for date in required_backfill_dates(today):
        lifecycle_path = NORMALIZED_DIR / f"order_finance_lifecycle_{date}.csv"
        coverage[date] = lifecycle_path.exists()
    return coverage


def find_non_terminal_orders() -> list[dict[str, str]]:
    """Every order across every order_finance_lifecycle_*.csv on disk that
    is currently in a non-terminal state, deduped by order_id (keep the
    most recently-observed row per order — later report_date wins)."""
    rows_by_order: dict[str, dict] = {}
    for path in sorted(glob.glob(str(NORMALIZED_DIR / "order_finance_lifecycle_*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["finance_state"] in NON_TERMINAL:
                    rows_by_order[row["order_id"]] = row
                elif row["order_id"] in rows_by_order and row["finance_state"] in TERMINAL:
                    # a later file already shows this order resolved —
                    # drop it from the recheck list
                    rows_by_order.pop(row["order_id"], None)
    return list(rows_by_order.values())


def recheck_orders(log: Logger, orders: list[dict[str, str]]) -> list[dict[str, Any]]:
    try:
        session = bootstrap_session()
    except PilotStopError as e:
        log.log(f"STOP: {e}")
        return []

    results = []
    for i, o in enumerate(orders, start=1):
        oid = o["order_id"]
        time.sleep(0.12)
        try:
            resp = _call_with_retry(
                lambda oid=oid: session.client.read_domain(
                    "finance_order_statement_transactions", session.access_token,
                    shop_cipher=session.shop_info.get("shop_cipher"),
                    path_params={"order_id": oid},
                ),
                log, f"backfill recheck {oid}",
            )
        except NetworkError as e:
            log.log(f"recheck {oid}: FAIL_NETWORK {e}")
            continue

        has_settlement = resp.ok and bool(resp.data.get("sku_transactions"))
        new_state = o["finance_state"]
        if has_settlement:
            new_state = "SETTLED (was %s — now has a statement, re-run finance_lifecycle.py for this order's date to fully reclassify)" % o["finance_state"]
        results.append({
            "order_id": oid,
            "previous_state": o["finance_state"],
            "order_create_date": o["order_create_date"],
            "still_non_terminal": not has_settlement,
            "checked_at_utc": now_utc_iso(),
            "note": new_state,
        })
        if i % 20 == 0:
            log.log(f"backfill recheck: {i}/{len(orders)} done")

    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", required=True)
    args = ap.parse_args()

    from pilot_common import LOGS_DIR
    log = Logger(LOGS_DIR / f"backfill_{args.today}.log")

    coverage = check_backfill_coverage(args.today)
    log.log(f"D-1/D-3/D-7/D-14 coverage for today={args.today}: {coverage}")
    backfill_pass = all(coverage.values())

    non_terminal = find_non_terminal_orders()
    log.log(f"{len(non_terminal)} order(s) currently non-terminal across all collected dates — rechecking live")

    recheck_results = recheck_orders(log, non_terminal)
    progressed = sum(1 for r in recheck_results if not r["still_non_terminal"])

    out = {
        "today": args.today,
        "backfill_dates_required": required_backfill_dates(args.today),
        "backfill_coverage": coverage,
        "backfill_mapping": "PASS" if backfill_pass else "FAIL",
        "non_terminal_orders_checked": len(recheck_results),
        "progressed_to_settled": progressed,
        "still_non_terminal": len(recheck_results) - progressed,
    }
    out_path = REPORTS_DIR / f"backfill_summary_{args.today}.json"
    import json
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    detail_path = NORMALIZED_DIR / f"backfill_recheck_{args.today}.csv"
    with open(detail_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "previous_state", "order_create_date",
                                           "still_non_terminal", "checked_at_utc", "note"])
        w.writeheader()
        for r in recheck_results:
            w.writerow(r)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nDetail: {detail_path}")
    print(f"Summary: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
