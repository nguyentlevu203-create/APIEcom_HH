#!/usr/bin/env python3
"""
Analyze the raw net_sales_reconciliation_sample.json and test the
candidate escrow_amount formula against every order in the sample,
printing the residual for each. Flags any order where the residual is
non-zero (beyond +/-1 VND rounding) so those can be inspected by hand.
"""
import json
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "raw" / "net_sales_reconciliation_sample.json"

with open(RAW, encoding="utf-8") as f:
    records = json.load(f)

CANDIDATE_DEDUCTIONS = [
    "commission_fee", "service_fee", "seller_transaction_fee",
    "order_ams_commission_fee", "ads_escrow_top_up_fee_or_technical_support_fee",
    "voucher_from_seller",
    "seller_return_refund", "drc_adjustable_refund", "seller_lost_compensation",
    "escrow_tax", "vat_on_imported_goods", "withholding_tax", "reverse_shipping_fee",
    "reverse_shipping_fee_sst", "shipping_fee_sst",
]

mismatches = 0
total_gross = 0
total_escrow = 0

for r in records:
    order_sn = r["order_sn"]
    income = r["escrow_detail"].get("response", {}).get("order_income", {})
    if not income:
        continue

    selling = income.get("order_selling_price", 0)
    escrow = income.get("escrow_amount", 0)

    net_shipping = (
        income.get("actual_shipping_fee", 0)
        - income.get("shopee_shipping_rebate", 0)
        - income.get("buyer_paid_shipping_fee", 0)
    )

    deductions = sum(income.get(k, 0) or 0 for k in CANDIDATE_DEDUCTIONS)
    candidate = selling - deductions - net_shipping
    residual = escrow - candidate

    total_gross += selling
    total_escrow += escrow

    if abs(residual) > 1:
        mismatches += 1
        print(f"MISMATCH {order_sn}: selling={selling} escrow={escrow} candidate={candidate} residual={residual}")
        for k, v in sorted(income.items()):
            if isinstance(v, (int, float)) and v != 0:
                print(f"    {k} = {v}")

print(f"\nOrders checked: {len(records)}")
print(f"Mismatches (|residual| > 1 VND): {mismatches}")
print(f"Sum(order_selling_price) across sample = {total_gross}")
print(f"Sum(escrow_amount) across sample        = {total_escrow}")
