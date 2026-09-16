#!/usr/bin/env python3
"""
Section 13 — parallel run vs. the existing manual report.

This environment has no connected feed for HH's current manual TikTok
report (no spreadsheet/database/email connector configured — checked at
session start, same finding as cogs_mapping.py). This script does NOT
fabricate manual_value numbers. It:

  1. Always computes and records the API-derived value for each tracked
     metric (this part is real, from already-collected data).
  2. Looks for a manually-supplied comparison file at a fixed path:
         pilot_reporting/manual_reports/manual_<date>.csv
     with columns: metric, manual_value
  3. If that file exists, joins it in and computes difference/status.
     If not, manual_value/difference/status are explicitly
     "AWAITING_MANUAL_INPUT" — never guessed.

Appends (never overwrites) to reports/daily_parallel_validation.csv so a
7-day run accumulates one growing file, per Section 13/14.

    python3 daily_parallel_validation.py --date 2026-09-07
"""
from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from pilot_common import NORMALIZED_DIR, PILOT_DIR, REPORTS_DIR

MANUAL_DIR = PILOT_DIR / "manual_reports"
OUT_PATH = REPORTS_DIR / "daily_parallel_validation.csv"
OUT_FIELDS = ["date", "metric", "api_value", "manual_value", "difference",
              "difference_pct", "basis_match", "status", "explanation"]

TRACKED_METRICS = [
    ("orders", "orders", "COUNT(orders.csv)"),
    ("units", "order_lines", "COUNT(order_lines.csv)"),
    ("gmv", "orders", "SUM(payment.total_amount), eligible orders"),
    ("cancel_count", "orders", "COUNT(status=CANCELLED)"),
    ("return_count", "returns", "COUNT(returns.csv)"),
    ("net_sales_candidate", "order_finance_lifecycle", "SUM(revenue_amount), finalized orders"),
]


def D(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    s = str(x).strip()
    if s == "":
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def read_csv(report_date: str, name: str) -> list[dict]:
    path = NORMALIZED_DIR / f"{name}_{report_date}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_api_values(report_date: str) -> dict[str, str]:
    orders = read_csv(report_date, "orders")
    lines = read_csv(report_date, "order_lines")
    returns = read_csv(report_date, "returns")
    lifecycle = read_csv(report_date, "order_finance_lifecycle")

    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    finalized = [r for r in lifecycle if r["finance_state"] in
                 ("SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID")]

    return {
        "orders": str(len(orders)),
        "units": str(len(lines)),
        "gmv": str(sum((D(o["total_amount"]) for o in eligible), Decimal("0"))),
        "cancel_count": str(sum(1 for o in orders if o["status"] == "CANCELLED")),
        "return_count": str(len(returns)),
        "net_sales_candidate": str(sum((D(r["final_revenue"]) for r in finalized), Decimal("0"))),
    }


def load_manual(report_date: str) -> Optional[dict[str, str]]:
    path = MANUAL_DIR / f"manual_{report_date}.csv"
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return {row["metric"]: row["manual_value"] for row in csv.DictReader(f)}


def build(report_date: str) -> list[dict[str, Any]]:
    api_values = compute_api_values(report_date)
    manual_values = load_manual(report_date)

    rows = []
    for metric, source_table, basis in TRACKED_METRICS:
        api_val = api_values[metric]
        if manual_values is None:
            manual_val = ""
            diff = ""
            diff_pct = ""
            basis_match = ""
            status = "AWAITING_MANUAL_INPUT"
            explanation = (f"No manual report on file at "
                           f"{MANUAL_DIR / f'manual_{report_date}.csv'} — place HH's existing "
                           "manual TikTok report there (columns: metric, manual_value) to enable "
                           "this comparison.")
        elif metric not in manual_values:
            manual_val = ""
            diff = ""
            diff_pct = ""
            basis_match = ""
            status = "METRIC_NOT_IN_MANUAL_REPORT"
            explanation = f"manual_{report_date}.csv has no row for metric={metric!r}"
        else:
            manual_val = manual_values[metric]
            a, m = D(api_val), D(manual_val)
            diff = str(a - m)
            diff_pct = str(round(float((a - m) / m * 100), 2)) if m != 0 else "N/A"
            basis_match = "TBD — requires manual confirmation of the manual report's own basis"
            status = "MATCH" if a == m else "DIFFERENCE"
            explanation = f"basis: {basis}" if a == m else \
                f"basis: {basis} — REQUIRES_RECONCILIATION unless a specific cause is confirmed"

        rows.append({
            "date": report_date, "metric": metric, "api_value": api_val,
            "manual_value": manual_val, "difference": diff, "difference_pct": diff_pct,
            "basis_match": basis_match, "status": status, "explanation": explanation,
        })

    is_new = not OUT_PATH.exists()
    with open(OUT_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if is_new:
            w.writeheader()
        for r in rows:
            w.writerow(r)

    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    rows = build(args.date)
    for r in rows:
        print(f"{r['metric']}: api={r['api_value']} manual={r['manual_value'] or '(none)'} "
              f"status={r['status']}")
    print(f"\nAppended to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
