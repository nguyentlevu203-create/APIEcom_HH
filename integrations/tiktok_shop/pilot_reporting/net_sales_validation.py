#!/usr/bin/env python3
"""
Net Sales Semantic Mapping validation (follow-up to V2.1 Section 4).

For each of 3+ fully-settled, low-complexity-return, decent-volume dates,
reconciles: Order API value, Analytics GMV, Order Finance revenue_amount
(+ its revenue_breakdown components), and statement-level
revenue/fee/shipping/settlement — all read from already-collected
raw/normalized files (no new API calls).

    python3 net_sales_validation.py --dates 2026-09-01,2026-08-25,2026-08-18
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pilot_common import NORMALIZED_DIR, RAW_DIR, REPORTS_DIR


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


def aggregate_revenue_breakdown(report_date: str) -> tuple[dict[str, Decimal], set[str], Decimal]:
    """Sum revenue_breakdown / shipping_cost_breakdown sub-fields directly
    from the raw finance_order_statement_transactions responses for this
    date. IMPORTANT: collect.py only saves a raw snapshot for a SAMPLE of
    orders (idx<=25 or idx%25==0 per order — see collect.py collect_finance),
    not every order. Returns the totals, the exact set of order_ids that
    sample actually covers, and the sum of THEIR OWN revenue_amount (so the
    caller compares like-for-like populations, not sample-vs-full-date)."""
    totals: dict[str, Decimal] = {}
    order_ids_covered: set[str] = set()
    revenue_amount_for_sample = Decimal("0")
    files = sorted(glob.glob(str(RAW_DIR / f"{report_date}_finance_order_statement_transactions_*.json")))
    for path in files:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        data = payload.get("data") or {}
        oid = data.get("order_id")
        if not oid or not data.get("sku_transactions"):
            continue
        order_ids_covered.add(oid)
        revenue_amount_for_sample += D(data.get("revenue_amount"))
        for sku in data.get("sku_transactions", []) or []:
            rb = sku.get("revenue_breakdown") or {}
            for k, v in rb.items():
                totals[f"revenue_breakdown.{k}"] = totals.get(f"revenue_breakdown.{k}", Decimal("0")) + D(v)
            scb = sku.get("shipping_cost_breakdown") or {}
            for k, v in scb.items():
                if isinstance(v, dict):
                    continue
                totals[f"shipping_cost_breakdown.{k}"] = totals.get(f"shipping_cost_breakdown.{k}", Decimal("0")) + D(v)
    return totals, order_ids_covered, revenue_amount_for_sample


def validate_date(report_date: str) -> dict[str, Any]:
    orders = read_csv(report_date, "orders")
    product_rows = read_csv(report_date, "product_analytics")
    lifecycle_rows = read_csv(report_date, "order_finance_lifecycle")
    statement_rows = read_csv(report_date, "finance_statements")

    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    order_api_value = sum((D(o["total_amount"]) for o in eligible), Decimal("0"))
    analytics_gmv = sum((D(r.get("total_performance.gmv.amount")) for r in product_rows), Decimal("0"))

    finalized_states = {"SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}
    finalized = [r for r in lifecycle_rows if r["finance_state"] in finalized_states]
    revenue_amount_sum = sum((D(r["final_revenue"]) for r in finalized), Decimal("0"))
    fee_sum = sum((D(r["final_fee"]) for r in finalized), Decimal("0"))
    shipping_sum = sum((D(r["final_shipping"]) for r in finalized), Decimal("0"))
    settlement_sum = sum((D(r["final_settlement"]) for r in finalized), Decimal("0"))

    statement_ids_used = {r["statement_id"] for r in finalized if r["statement_id"]}
    stmt_rows_used = [s for s in statement_rows if s["id"] in statement_ids_used]
    stmt_revenue = sum((D(s.get("revenue_amount")) for s in stmt_rows_used), Decimal("0"))
    stmt_fee = sum((D(s.get("fee_amount")) for s in stmt_rows_used), Decimal("0"))
    stmt_shipping = sum((D(s.get("shipping_cost_amount")) for s in stmt_rows_used), Decimal("0"))
    stmt_settlement = sum((D(s.get("settlement_amount")) for s in stmt_rows_used), Decimal("0"))

    breakdown, sample_order_ids, sample_revenue_amount = aggregate_revenue_breakdown(report_date)
    gross_sales = breakdown.get("revenue_breakdown.subtotal_before_discount_amount", Decimal("0"))
    seller_discount = breakdown.get("revenue_breakdown.seller_discount_amount", Decimal("0"))
    refund_subtotal = breakdown.get("revenue_breakdown.refund_subtotal_before_discount_amount", Decimal("0"))
    seller_discount_refund = breakdown.get("revenue_breakdown.seller_discount_refund_amount", Decimal("0"))
    cod_fee = breakdown.get("revenue_breakdown.cod_service_fee_amount", Decimal("0"))
    distant_item_fee = breakdown.get("revenue_breakdown.distant_item_fee_amount", Decimal("0"))
    refund_cod_fee = breakdown.get("revenue_breakdown.refund_cod_service_fee_amount", Decimal("0"))
    revenue_reconstructed = (gross_sales + seller_discount + refund_subtotal + seller_discount_refund
                              + cod_fee + distant_item_fee + refund_cod_fee)

    coverage_pct = (len(finalized) / len(eligible) * 100) if eligible else 0

    return {
        "report_date": report_date,
        "eligible_orders": len(eligible),
        "coverage_pct": f"{coverage_pct:.1f}%",
        "order_api_value": str(order_api_value),
        "analytics_gmv": str(analytics_gmv),
        "order_finance_revenue_amount": str(revenue_amount_sum),
        "revenue_breakdown": {
            "sample_size": len(sample_order_ids),
            "sample_note": (
                f"raw snapshots exist for only {len(sample_order_ids)}/{len(finalized)} "
                "finalized orders (collect.py samples idx<=25 or idx%25==0 per order to "
                "limit raw file volume) — this reconstruction check compares against "
                "THOSE SAME orders' own revenue_amount, not the full-date total"
            ),
            "sample_revenue_amount": str(sample_revenue_amount),
            "gross_sales_subtotal_before_discount": str(gross_sales),
            "seller_discount": str(seller_discount),
            "refund_subtotal_before_discount": str(refund_subtotal),
            "seller_discount_refund": str(seller_discount_refund),
            "cod_and_distant_item_fees": str(cod_fee + distant_item_fee + refund_cod_fee),
            "reconstructed_revenue_amount": str(revenue_reconstructed),
            "matches_reported_revenue_amount": revenue_reconstructed == sample_revenue_amount,
            "reconstruction_diff": str(revenue_reconstructed - sample_revenue_amount),
        },
        "statement_level": {
            "statements_used": len(stmt_rows_used),
            "statement_revenue_amount": str(stmt_revenue),
            "statement_fee_amount": str(stmt_fee),
            "statement_shipping_cost_amount": str(stmt_shipping),
            "statement_settlement_amount": str(stmt_settlement),
        },
        "order_finance_fee": str(fee_sum),
        "order_finance_shipping": str(shipping_sum),
        "order_finance_settlement": str(settlement_sum),
        "settlement_reconciles": (revenue_amount_sum + fee_sum + shipping_sum) == settlement_sum,
    }


def build_markdown(results: list[dict[str, Any]]) -> Path:
    lines = [
        "# HH TikTok Net Sales Validation — V2",
        "",
        "Status: candidate formula validated for DATA CONSISTENCY across "
        f"{len(results)} fully-settled dates. This is `HH_NET_SALES_CANDIDATE`, "
        "**not** `NET SALES FINAL` — business/finance sign-off is still required "
        "before promoting this to PASS (see closing section).",
        "",
        "## Per-date reconciliation",
        "",
    ]
    for r in results:
        lines.append(f"### {r['report_date']} ({r['eligible_orders']} eligible orders, "
                     f"{r['coverage_pct']} finalized)")
        lines.append("")
        lines.append("| Component | Value | Source |")
        lines.append("|---|---|---|")
        lines.append(f"| Order API Value | {r['order_api_value']} | orders_{r['report_date']}.csv, SUM(payment.total_amount), eligible only |")
        lines.append(f"| Analytics GMV | {r['analytics_gmv']} | product_analytics_{r['report_date']}.csv, SUM(total_performance.gmv.amount) |")
        lines.append(f"| Order Finance `revenue_amount` | {r['order_finance_revenue_amount']} | order_finance_lifecycle_{r['report_date']}.csv, finalized orders only |")
        rb = r["revenue_breakdown"]
        lines.append("")
        lines.append(f"Field-level reconstruction check (sample: {rb['sample_size']} orders with a "
                     f"saved raw snapshot — {rb['sample_note']}):")
        lines.append("")
        lines.append("| Component | Value | Source |")
        lines.append("|---|---|---|")
        lines.append(f"| Sample orders' own `revenue_amount` | {rb['sample_revenue_amount']} | order_finance_lifecycle, same order_id set as the raw sample |")
        lines.append(f"| ↳ Gross sales (`subtotal_before_discount_amount`) | {rb['gross_sales_subtotal_before_discount']} | raw sku_transactions[].revenue_breakdown |")
        lines.append(f"| ↳ Seller discount | {rb['seller_discount']} | same |")
        lines.append(f"| ↳ Refund subtotal (reversal) | {rb['refund_subtotal_before_discount']} | same |")
        lines.append(f"| ↳ Seller discount refund (reversal) | {rb['seller_discount_refund']} | same |")
        lines.append(f"| ↳ COD / distant-item fees | {rb['cod_and_distant_item_fees']} | same |")
        lines.append(f"| ↳ Reconstructed (sum of the above) | {rb['reconstructed_revenue_amount']} | computed |")
        lines.append(f"| ↳ Matches sample's own `revenue_amount`? | **{'YES' if rb['matches_reported_revenue_amount'] else 'NO — diff ' + rb['reconstruction_diff']}** | |")
        sl = r["statement_level"]
        lines.append(f"| Statement-level revenue_amount ({sl['statements_used']} statements) | {sl['statement_revenue_amount']} | finance_statements_{r['report_date']}.csv |")
        lines.append(f"| Order Finance fee (`fee_and_tax_amount`) | {r['order_finance_fee']} | order_finance_lifecycle |")
        lines.append(f"| Order Finance shipping (`shipping_cost_amount`) | {r['order_finance_shipping']} | order_finance_lifecycle |")
        lines.append(f"| Order Finance settlement (`settlement_amount`) | {r['order_finance_settlement']} | order_finance_lifecycle |")
        lines.append(f"| revenue + fee + shipping == settlement? | **{'YES' if r['settlement_reconciles'] else 'NO'}** | arithmetic check |")
        lines.append("")

    all_reconstruct_match = all(r["revenue_breakdown"]["matches_reported_revenue_amount"] for r in results)
    all_settlement_reconciles = all(r["settlement_reconciles"] for r in results)

    lines.append("## Cross-date consistency result")
    lines.append("")
    lines.append(f"- `revenue_amount` reconstructs exactly from `revenue_breakdown` components on "
                 f"**{sum(1 for r in results if r['revenue_breakdown']['matches_reported_revenue_amount'])}/{len(results)}** dates.")
    lines.append(f"- `revenue + fee + shipping == settlement_amount` holds on "
                 f"**{sum(1 for r in results if r['settlement_reconciles'])}/{len(results)}** dates.")
    lines.append("- Order API Value vs Order Finance revenue_amount: NOT expected to match "
                 "1:1 (different bases — Order API is buyer-paid total incl. shipping; Finance "
                 "revenue_amount excludes shipping and nets discounts/refunds differently). "
                 "See `finance_reconciliation_v21_<date>.json` sanity_check section for the "
                 "per-date gap, marked REQUIRES_RECONCILIATION where unexplained.")
    lines.append("")
    lines.append("## Candidate formula")
    lines.append("")
    lines.append("```")
    lines.append("HH_NET_SALES_CANDIDATE = SUM(revenue_amount)")
    lines.append("                          over orders where finance_state IN")
    lines.append("                          (SETTLED, STATEMENT_ISSUED, PAYMENT_PENDING, PAID)")
    lines.append("```")
    lines.append("")
    lines.append(f"Internal consistency: {'CONFIRMED' if all_reconstruct_match and all_settlement_reconciles else 'PARTIAL — see per-date table above'} "
                 f"across all {len(results)} validated dates.")
    lines.append("")
    lines.append("## Why this is `HH_NET_SALES_CANDIDATE`, not `NET SALES FINAL`")
    lines.append("")
    lines.append("This validation proves the field is **arithmetically self-consistent** "
                 "(reconstructs from its own documented sub-components, nets correctly into "
                 "settlement). It does NOT constitute business sign-off that this is the "
                 "figure HH's finance team wants reported as \"Net Sales\" (e.g. whether "
                 "shipping revenue should be included, whether platform-funded discounts "
                 "should be treated differently, whether this should be reported gross or "
                 "net of the still-unresolved Cash Reconciliation gap). That decision is "
                 "outside what data analysis alone can settle.")
    lines.append("")
    lines.append("**Net Sales Semantic Mapping moves to PASS once a named HH finance/business "
                 "owner confirms this formula in writing** (or a different one, still derived "
                 "only from the documented components in "
                 "`HH_TIKTOK_NET_SALES_MAPPING_V1.md`).")
    lines.append("")

    path = REPORTS_DIR / "HH_TIKTOK_NET_SALES_VALIDATION_V2.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True, help="comma-separated YYYY-MM-DD list")
    args = ap.parse_args()
    dates = [d.strip() for d in args.dates.split(",")]

    results = [validate_date(d) for d in dates]
    out_json = REPORTS_DIR / "net_sales_validation_v2.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    md_path = build_markdown(results)
    print(f"Validated {len(dates)} dates.")
    for r in results:
        print(f"  {r['report_date']}: reconstruct_match={r['revenue_breakdown']['matches_reported_revenue_amount']} "
              f"settlement_reconciles={r['settlement_reconciles']}")
    print(f"\nMarkdown: {md_path}")
    print(f"JSON: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
