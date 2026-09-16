#!/usr/bin/env python3
"""
COGS / SKU master join (Section 5-6 of the follow-up spec).

Looks for an authoritative HH COGS/SKU master at a fixed, documented
location this project can read:

    pilot_reporting/cogs_master/hh_sku_cogs_master.csv

Expected columns (minimum):
    tiktok_sku_id, tiktok_seller_sku, tiktok_product_id, ean,
    hh_sku_code, cogs_amount, currency, effective_from, effective_to, source

No enterprise data connector (database, spreadsheet API, ERP) is
configured in this environment — checked at session start, none found.
This module does NOT invent a COGS source; if the file above doesn't
exist, coverage is honestly 0% and every line is MISSING_MAPPING.

Matching is EXACT only — never fuzzy. Join key precedence:
    1. tiktok_sku_id  (most specific)
    2. tiktok_seller_sku
Only one of the two needs to match for a candidate; if a seller_sku or
sku_id maps to >1 active (effective_from <= order_date <= effective_to)
COGS row, that line is MULTIPLE_MATCH, not silently resolved.

    python3 cogs_mapping.py --date 2026-09-01
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from pilot_common import NORMALIZED_DIR, PILOT_DIR, REPORTS_DIR

COGS_MASTER_PATH = PILOT_DIR / "cogs_master" / "hh_sku_cogs_master.csv"
REQUIRED_COLUMNS = {
    "tiktok_sku_id", "tiktok_seller_sku", "tiktok_product_id", "ean",
    "hh_sku_code", "cogs_amount", "currency", "effective_from", "effective_to", "source",
}


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


def load_cogs_master() -> tuple[list[dict], Optional[str]]:
    """Returns (rows, error). error is None if the file loaded cleanly and
    has the required columns; otherwise rows is [] and error explains why."""
    if not COGS_MASTER_PATH.exists():
        return [], (f"SOURCE NOT CONNECTED — no file at {COGS_MASTER_PATH}. "
                     "No database/spreadsheet/ERP connector is configured in this "
                     "environment (checked at session start). Place an authoritative "
                     "HH SKU/COGS export at this path to connect it.")
    with open(COGS_MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        missing_cols = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing_cols:
            return [], f"INVALID — {COGS_MASTER_PATH} is missing required columns: {sorted(missing_cols)}"
    return rows, None


def _active(row: dict, order_date: str) -> bool:
    eff_from = row.get("effective_from") or "0001-01-01"
    eff_to = row.get("effective_to") or "9999-12-31"
    try:
        return eff_from <= order_date <= eff_to
    except TypeError:
        return False


def join_lines(report_date: str) -> dict[str, Any]:
    lines = read_csv(report_date, "order_lines")
    orders = {o["order_id"]: o for o in read_csv(report_date, "orders")}
    cogs_rows, load_error = load_cogs_master()

    by_sku_id: dict[str, list[dict]] = {}
    by_seller_sku: dict[str, list[dict]] = {}
    for r in cogs_rows:
        if r.get("tiktok_sku_id"):
            by_sku_id.setdefault(r["tiktok_sku_id"], []).append(r)
        if r.get("tiktok_seller_sku"):
            by_seller_sku.setdefault(r["tiktok_seller_sku"], []).append(r)

    out_rows = []
    eligible_value = Decimal("0")
    matched_value = Decimal("0")
    eligible_units = 0
    matched_units = 0
    missing_skus: set[str] = set()

    for li in lines:
        order = orders.get(li["order_id"])
        if not order or order.get("status") == "CANCELLED":
            continue  # COGS coverage is defined over eligible (non-cancelled) lines only
        order_date = report_date
        line_value = D(li.get("sale_price"))
        qty = 1
        eligible_value += line_value
        eligible_units += qty

        if load_error:
            status = "MISSING_MAPPING"
            cogs_amount = None
            hh_sku_code = None
        else:
            candidates = by_sku_id.get(li.get("sku_id"), [])
            if not candidates:
                candidates = by_seller_sku.get(li.get("seller_sku"), [])
            active_candidates = [c for c in candidates if _active(c, order_date)]

            if not active_candidates:
                status = "MISSING_MAPPING"
                cogs_amount = None
                hh_sku_code = None
                missing_skus.add(li.get("seller_sku") or li.get("sku_id") or "UNKNOWN")
            elif len(active_candidates) > 1:
                status = "MULTIPLE_MATCH"
                cogs_amount = None
                hh_sku_code = None
            else:
                status = "EXACT"
                cogs_amount = D(active_candidates[0].get("cogs_amount"))
                hh_sku_code = active_candidates[0].get("hh_sku_code")
                matched_value += line_value
                matched_units += qty

        out_rows.append({
            "order_id": li["order_id"],
            "line_item_id": li.get("line_item_id"),
            "sku_id": li.get("sku_id"),
            "seller_sku": li.get("seller_sku"),
            "sale_price": li.get("sale_price"),
            "match_status": status,
            "hh_sku_code": hh_sku_code,
            "cogs_amount": str(cogs_amount) if cogs_amount is not None else "",
        })

    out_path = NORMALIZED_DIR / f"cogs_join_{report_date}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "order_id", "line_item_id", "sku_id", "seller_sku", "sale_price",
            "match_status", "hh_sku_code", "cogs_amount",
        ])
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    value_coverage_pct = float(matched_value / eligible_value * 100) if eligible_value else 0.0
    unit_coverage_pct = (matched_units / eligible_units * 100) if eligible_units else 0.0

    return {
        "report_date": report_date,
        "cogs_source_connected": load_error is None,
        "load_error": load_error,
        "eligible_line_count": len(out_rows),
        "eligible_value": str(eligible_value),
        "eligible_units": eligible_units,
        "matched_value": str(matched_value),
        "matched_units": matched_units,
        "value_coverage_pct": round(value_coverage_pct, 1),
        "unit_coverage_pct": round(unit_coverage_pct, 1),
        "missing_skus": sorted(missing_skus)[:20],
        "missing_sku_count": len(missing_skus),
        "join_file": str(out_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    result = join_lines(args.date)
    out_path = REPORTS_DIR / f"cogs_coverage_{args.date}.json"
    import json
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
