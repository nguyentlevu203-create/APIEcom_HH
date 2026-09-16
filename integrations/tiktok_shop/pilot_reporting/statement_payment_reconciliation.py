#!/usr/bin/env python3
"""
V2.1 Section 3 — statement <-> payment reconciliation.

Groups a date's statements by payment_id (supports many-statements-to-one-
payment, never assumes 1:1) and compares the summed statement
settlement_amount against the matched Payments record's settlement_amount.

    python3 statement_payment_reconciliation.py --date 2026-09-07
"""
from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from pilot_common import NORMALIZED_DIR, REPORTS_DIR


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


def read_csv(path_name: str, dated: bool, date: str = "") -> list[dict]:
    name = f"{path_name}_{date}.csv" if dated else f"{path_name}.csv"
    path = NORMALIZED_DIR / name
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build(report_date: str) -> list[dict]:
    statements = read_csv("finance_statements", dated=True, date=report_date)
    payments = {r["id"]: r for r in read_csv("payments_all", dated=False)}

    by_payment: dict[str, list[dict]] = {}
    no_payment_id: list[dict] = []
    for s in statements:
        pid = s.get("payment_id")
        if not pid:
            no_payment_id.append(s)
            continue
        by_payment.setdefault(pid, []).append(s)

    rows = []
    for pid, stmts in by_payment.items():
        stmt_settlement_sum = sum((D(s.get("settlement_amount")) for s in stmts), Decimal("0"))
        stmt_ids = sorted(s["id"] for s in stmts)
        payment = payments.get(pid)

        if payment is None:
            match_status = "MISSING_PAYMENT"
            payment_status = ""
            payment_settlement_amount = ""
            payment_amount = ""
            paid_time = ""
            difference = ""
        else:
            payment_status = payment.get("status", "")
            payment_settlement_amount = payment.get("settlement_amount_value", "")
            payment_amount = payment.get("amount_value", "")
            paid_time = payment.get("paid_time", "")
            diff = stmt_settlement_sum - D(payment_settlement_amount)
            difference = str(diff)
            if payment_status != "PAID":
                match_status = "PENDING_PAYMENT"
            elif diff == 0:
                match_status = "MULTI_STATEMENT_PAYMENT" if len(stmts) > 1 else "MATCHED"
            else:
                match_status = "PAYMENT_AMOUNT_DIFFERENCE"

        rows.append({
            "statement_ids": ";".join(stmt_ids),
            "statement_count": len(stmts),
            "statement_settlement_amount_sum": str(stmt_settlement_sum),
            "payment_id": pid,
            "payment_status": payment_status,
            "payment_settlement_amount": payment_settlement_amount,
            "payment_amount": payment_amount,
            "paid_time": paid_time,
            "difference": difference,
            "match_status": match_status,
        })

    for s in no_payment_id:
        rows.append({
            "statement_ids": s["id"], "statement_count": 1,
            "statement_settlement_amount_sum": s.get("settlement_amount", ""),
            "payment_id": "", "payment_status": "", "payment_settlement_amount": "",
            "payment_amount": "", "paid_time": "", "difference": "",
            "match_status": "UNKNOWN",
        })

    out_path = NORMALIZED_DIR / f"statement_payment_reconciliation_{report_date}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "statement_ids", "statement_count", "statement_settlement_amount_sum",
            "payment_id", "payment_status", "payment_settlement_amount", "payment_amount",
            "paid_time", "difference", "match_status",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    return rows


def summarize(rows: list[dict]) -> dict[str, Any]:
    from collections import Counter
    counts = Counter(r["match_status"] for r in rows)
    total = len(rows)
    multi_statement = sum(1 for r in rows if r["statement_count"] > 1)

    if total == 0:
        mapping = "AMBER"
    elif counts.get("UNKNOWN", 0) > 0 or counts.get("PAYMENT_AMOUNT_DIFFERENCE", 0) > 0:
        mapping = "FAIL"
    elif counts.get("MISSING_PAYMENT", 0) == total:
        mapping = "FAIL"
    elif counts.get("MISSING_PAYMENT", 0) > 0:
        mapping = "AMBER"
    else:
        mapping = "PASS"

    return {
        "total_payment_groups": total,
        "multi_statement_payment_groups": multi_statement,
        "matched": counts.get("MATCHED", 0),
        "multi_statement_payment": counts.get("MULTI_STATEMENT_PAYMENT", 0),
        "pending_payment": counts.get("PENDING_PAYMENT", 0),
        "payment_amount_difference": counts.get("PAYMENT_AMOUNT_DIFFERENCE", 0),
        "missing_payment": counts.get("MISSING_PAYMENT", 0),
        "unknown": counts.get("UNKNOWN", 0),
        "payment_reconciliation_mapping": mapping,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    rows = build(args.date)
    summary = summarize(rows)
    print(f"Statement/Payment groups for {args.date}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
