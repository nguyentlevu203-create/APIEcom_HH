#!/usr/bin/env python3
"""
P0 — Net Sales semantic bridge: pull N real COMPLETED orders, fetch
order_detail + get_escrow_detail for each, and check whether a
candidate Net Sales / fee decomposition arithmetically reconciles to
Shopee's own escrow_amount for that order.

This is an INTERNAL consistency check against Shopee's own reported
numbers (order_income.* fields must sum to escrow_amount). It is NOT an
external reconciliation against a separate Seller Centre financial
export/CSV — no such export is available to this pilot. That limitation
is stated explicitly in the report, not glossed over.

Output: pilot_reporting/raw/net_sales_reconciliation_sample.json (full
per-order data, no secrets) and
pilot_reporting/reports/net_sales_reconciliation.csv (flat table).
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shopee_client import ShopeeClient  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "raw"
REPORTS_DIR = BASE_DIR / "reports"

SAMPLE_SIZE = 30

# Fields we pull out of order_income for the CSV / reconciliation.
FIELDS = [
    "order_original_price", "order_discounted_price", "order_selling_price",
    "order_seller_discount", "cost_of_goods_sold", "original_cost_of_goods_sold",
    "shopee_discount", "original_shopee_discount", "voucher_from_seller",
    "voucher_from_shopee", "seller_discount", "commission_fee", "service_fee",
    "seller_transaction_fee", "credit_card_transaction_fee", "buyer_transaction_fee",
    "campaign_fee", "order_ams_commission_fee", "actual_shipping_fee",
    "estimated_shipping_fee", "buyer_paid_shipping_fee", "shipping_fee_discount_from_3pl",
    "final_shipping_fee", "shopee_shipping_rebate", "reverse_shipping_fee",
    "seller_return_refund", "drc_adjustable_refund", "seller_lost_compensation",
    "escrow_tax", "vat_on_imported_goods", "withholding_tax", "total_adjustment_amount",
    "escrow_amount", "escrow_amount_after_adjustment", "buyer_total_amount",
]


def main() -> int:
    client = ShopeeClient()
    now = int(time.time())

    order_list_resp = client.get(
        "/api/v2/order/get_order_list",
        {
            "time_range_field": "create_time",
            "time_from": now - 15 * 24 * 3600,
            "time_to": now,
            "page_size": SAMPLE_SIZE,
            "order_status": "COMPLETED",
        },
    )
    order_sns = [o["order_sn"] for o in order_list_resp.get("response", {}).get("order_list", [])][:SAMPLE_SIZE]
    print(f"Sampled {len(order_sns)} COMPLETED orders.")

    rows = []
    raw_records = []

    for i, order_sn in enumerate(order_sns):
        escrow = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": order_sn})
        time.sleep(0.25)

        if escrow.get("error"):
            rows.append({"order_sn": order_sn, "_error": escrow.get("error")})
            raw_records.append({"order_sn": order_sn, "escrow_detail": escrow})
            continue

        income = escrow.get("response", {}).get("order_income", {})
        row = {"order_sn": order_sn}
        for f in FIELDS:
            row[f] = income.get(f, "")
        rows.append(row)
        raw_records.append({"order_sn": order_sn, "escrow_detail": escrow})

        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{len(order_sns)} fetched")

    with open(RAW_DIR / "net_sales_reconciliation_sample.json", "w", encoding="utf-8") as f:
        json.dump(raw_records, f, indent=2, ensure_ascii=False)

    csv_path = REPORTS_DIR / "net_sales_reconciliation.csv"
    fieldnames = ["order_sn"] + FIELDS + ["_error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    print(f"Wrote {len(rows)} rows to {csv_path}")
    print(f"Wrote raw evidence to {RAW_DIR / 'net_sales_reconciliation_sample.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
