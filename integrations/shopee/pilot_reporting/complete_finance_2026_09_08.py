#!/usr/bin/env python3
"""
Phase 18 step 2+3: complete Shopee escrow coverage to the full 98-order
population for 2026-09-08, and retry Returns with exponential backoff.
GET-only via ShopeeClient. All output stays under pilot_reporting/.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shopee_client import ShopeeClient  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw" / "2026-09-08"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

client = ShopeeClient()

order_sns = json.load(open(RAW_DIR / "order_sns.json"))
order_detail = json.load(open(RAW_DIR / "order_detail.json"))
status_by_sn = {o["order_sn"]: o.get("order_status") for o in order_detail}
existing_escrow = json.load(open(RAW_DIR / "escrow_detail.json"))  # first 60, in order

assert len(order_sns) == 98, f"expected 98 order_sns, got {len(order_sns)}"

remaining = order_sns[len(existing_escrow):]
print(f"Already have escrow for {len(existing_escrow)}/98. Fetching remaining {len(remaining)}.")

new_escrow = []
for idx, sn in enumerate(remaining):
    attempt = 0
    while True:
        attempt += 1
        resp = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": sn})
        err = resp.get("error", "")
        http = resp.get("_http_status")
        if not err and http == 200:
            break
        low = str(err).lower()
        is_rate_limit = "rate" in low or "too many" in low or "limit" in low or http == 429
        is_transient = "inner error" in str(resp.get("message", "")).lower() or "try later" in str(resp.get("message", "")).lower()
        if (is_rate_limit or is_transient) and attempt <= 5:
            backoff = min(2 ** attempt, 30)
            print(f"  [{sn}] attempt {attempt} error={err!r} msg={resp.get('message')!r} -> backoff {backoff}s")
            time.sleep(backoff)
            continue
        break
    new_escrow.append({"order_sn": sn, "resp": resp})
    time.sleep(0.3)

all_escrow_by_sn = {}
for sn, e in zip(order_sns[: len(existing_escrow)], existing_escrow):
    all_escrow_by_sn[sn] = e
for item in new_escrow:
    all_escrow_by_sn[item["order_sn"]] = item["resp"]

assert len(all_escrow_by_sn) == 98, f"escrow coverage incomplete: {len(all_escrow_by_sn)}/98"

with open(RAW_DIR / "escrow_detail_full_98.json", "w", encoding="utf-8") as f:
    json.dump({sn: all_escrow_by_sn[sn] for sn in order_sns}, f, ensure_ascii=False, indent=2)

# Classify each order's finance status
rows = []
for sn in order_sns:
    order_status = status_by_sn.get(sn, "UNKNOWN")
    e = all_escrow_by_sn.get(sn)
    if e is None:
        rows.append({"order_sn": sn, "order_status": order_status, "finance_status": "MISSING",
                     "escrow_amount": None, "merchant_subtotal": None, "error_code": None, "error_message": None})
        continue
    err = e.get("error", "")
    http = e.get("_http_status")
    if err or http != 200:
        rows.append({"order_sn": sn, "order_status": order_status, "finance_status": "API_ERROR",
                     "escrow_amount": None, "merchant_subtotal": None,
                     "error_code": err, "error_message": e.get("message")})
        continue
    order_income = e.get("response", {}).get("order_income")
    if not order_income:
        rows.append({"order_sn": sn, "order_status": order_status, "finance_status": "API_ERROR",
                     "escrow_amount": None, "merchant_subtotal": None,
                     "error_code": "empty_order_income", "error_message": None})
        continue
    escrow_amount = order_income.get("escrow_amount")
    merchant_subtotal = e.get("response", {}).get("buyer_payment_info", {}).get("merchant_subtotal")
    if order_status == "CANCELLED":
        finance_status = "NOT_FINANCE_ELIGIBLE"
    else:
        # Escrow API returns a provisional/estimated figure pre-payout; Shopee VN
        # actual payout/settlement is confirmed via wallet transaction / income
        # report, which this session did not exhaustively pull for every order.
        # Absent a per-order payout confirmation, every non-cancelled order is
        # honestly NOT_SETTLED regardless of order_status (including COMPLETED,
        # since "buyer confirmed receipt" != "payout executed").
        finance_status = "NOT_SETTLED"
    rows.append({"order_sn": sn, "order_status": order_status, "finance_status": finance_status,
                 "escrow_amount": escrow_amount, "merchant_subtotal": merchant_subtotal,
                 "error_code": None, "error_message": None})

import csv
csv_path = REPORTS_DIR / "shopee_finance_reconciliation_2026-09-08.csv"
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["order_sn", "order_status", "finance_status",
                                       "escrow_amount", "merchant_subtotal", "error_code", "error_message"])
    w.writeheader()
    for r in rows:
        w.writerow(r)

from collections import Counter
status_counts = Counter(r["finance_status"] for r in rows)
print("\nFinance status breakdown (98 orders):", dict(status_counts))
print(f"CSV written: {csv_path}")

# ---- Returns retry with exponential backoff ----
from datetime import timedelta
VN_TZ = timezone(timedelta(hours=7))
DAY_START = datetime(2026, 9, 8, 0, 0, 0, tzinfo=VN_TZ)
DAY_END = datetime(2026, 9, 9, 0, 0, 0, tzinfo=VN_TZ)
TS_START = int(DAY_START.timestamp())
TS_END = int(DAY_END.timestamp())

returns_result = None
backoffs = [5, 15, 40, 90, 180]
for attempt, backoff in enumerate([0] + backoffs, start=1):
    if backoff:
        print(f"\nReturns retry attempt {attempt}: waiting {backoff}s before call...")
        time.sleep(backoff)
    resp = client.get("/api/v2/returns/get_return_list", {
        "create_time_from": TS_START, "create_time_to": TS_END, "page_size": 100,
    })
    err = resp.get("error", "")
    http = resp.get("_http_status")
    print(f"  attempt {attempt}: http={http} error={err!r} message={resp.get('message')!r}")
    if not err and http == 200:
        returns_result = resp
        break
    returns_result = resp  # keep last attempt's response regardless

with open(RAW_DIR / "return_list_final_2026-09-08.json", "w", encoding="utf-8") as f:
    json.dump(returns_result, f, ensure_ascii=False, indent=2)

if not returns_result.get("error") and returns_result.get("_http_status") == 200:
    return_count = len(returns_result.get("response", {}).get("return", []))
    if return_count == 0:
        returns_status = "API_EMPTY_VERIFIED"
    else:
        returns_status = f"PASS_NONZERO ({return_count} returns)"
else:
    returns_status = f"API_ERROR_TRANSIENT (http={returns_result.get('_http_status')}, error={returns_result.get('error')!r}, message={returns_result.get('message')!r})"

print(f"\nReturns final status: {returns_status}")

summary = {
    "extracted_at": datetime.now(timezone.utc).isoformat(),
    "report_date": "2026-09-08",
    "finance_status_breakdown": dict(status_counts),
    "finance_checked_orders": len(order_sns),
    "eligible_orders_total": len(order_sns),
    "returns_status": returns_status,
    "returns_raw_file": str(RAW_DIR / "return_list_final_2026-09-08.json"),
    "finance_csv": str(csv_path),
}
with open(REPORTS_DIR / "phase18_shopee_finance_returns_summary_2026-09-08.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print("\nSummary written:", REPORTS_DIR / "phase18_shopee_finance_returns_summary_2026-09-08.json")
