#!/usr/bin/env python3
"""
FINANCE RECONCILIATION V2 — final assembly (Sections 4, 5, 6, 7, 8, 10 of
the follow-up spec). Reads only what collect.py and finance_lifecycle.py
already wrote to normalized/*_<date>.csv — no API calls here.

    python3 reconciliation_v2.py --date 2026-09-07
    python3 reconciliation_v2.py --date 2026-09-01
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pilot_common import NORMALIZED_DIR, REPORTS_DIR

SHOP_NAME = "Le Petit Marseillais Vietnam"


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


# ---------------------------------------------------------------------------
# Section 5 — Net Sales field mapping (fixed documentation table, grounded
# in fields actually observed live in this project — not invented).
# ---------------------------------------------------------------------------

NET_SALES_MAPPING = [
    {
        "hh_metric": "Gross Order Value", "tiktok_field": "payment.total_amount",
        "endpoint": "GET /order/202309/orders (Order Detail)",
        "business_meaning": "Buyer-paid total per order, pre-settlement",
        "include_exclude": "REFERENCE ONLY — not Net Sales",
        "settled_or_unsettled": "both", "confidence": "HIGH",
        "evidence": "orders_<date>.csv",
    },
    {
        "hh_metric": "Cancelled Order Value", "tiktok_field": "payment.total_amount (status=CANCELLED)",
        "endpoint": "GET /order/202309/orders",
        "business_meaning": "Value of orders that never completed",
        "include_exclude": "EXCLUDE from Net Sales",
        "settled_or_unsettled": "n/a", "confidence": "HIGH",
        "evidence": "orders_<date>.csv — 5/89 on 2026-09-07, 7/62 on 2026-09-01",
    },
    {
        "hh_metric": "Settled Revenue", "tiktok_field": "revenue_amount",
        "endpoint": "GET /finance/202501/orders/{order_id}/statement_transactions",
        "business_meaning": "TikTok-recognized revenue AFTER refund netting, at settlement",
        "include_exclude": "INCLUDE — this is the Net Sales candidate numerator",
        "settled_or_unsettled": "settled only", "confidence": "HIGH",
        "evidence": "order_finance_lifecycle_<date>.csv final_revenue column; "
                    "proven correct on 2026-09-01 (55/55 eligible orders SETTLED, all "
                    "revenue_amount values non-zero and internally consistent)",
    },
    {
        "hh_metric": "Settled Fee & Tax", "tiktok_field": "fee_and_tax_amount",
        "endpoint": "GET /finance/202501/orders/{order_id}/statement_transactions",
        "business_meaning": "Platform fees/taxes deducted at settlement",
        "include_exclude": "EXCLUDE from Net Sales (it's a cost, feeds CM1/CM2 later)",
        "settled_or_unsettled": "settled only", "confidence": "HIGH",
        "evidence": "order_finance_lifecycle_<date>.csv final_fee column",
    },
    {
        "hh_metric": "Settled Shipping Cost", "tiktok_field": "shipping_cost_amount",
        "endpoint": "GET /finance/202501/orders/{order_id}/statement_transactions",
        "business_meaning": "Net shipping cost impact at settlement",
        "include_exclude": "EXCLUDE from Net Sales — contextual cost line for CM only",
        "settled_or_unsettled": "settled only", "confidence": "MEDIUM (mostly 0 in sample so far)",
        "evidence": "order_finance_lifecycle_<date>.csv final_shipping column",
    },
    {
        "hh_metric": "Statement Adjustment", "tiktok_field": "adjustment_amount",
        "endpoint": "GET /finance/202501/statements/{statement_id}/statement_transactions",
        "business_meaning": "Statement-level correction/dispute entry",
        "include_exclude": "EXCLUDE from Net Sales unless traced to a specific order-level "
                            "revenue correction (not observed yet)",
        "settled_or_unsettled": "settled only", "confidence": "MEDIUM (0 observed so far)",
        "evidence": "finance_transactions_<date>.csv adjustment_amount column",
    },
    {
        "hh_metric": "Statement net_sales_amount (TikTok's own field)",
        "tiktok_field": "net_sales_amount",
        "endpoint": "GET /finance/202309/statements",
        "business_meaning": "TikTok's own STATEMENT-level (not per-order) aggregate, name unverified",
        "include_exclude": "EXCLUDE — do NOT use for VN Net Sales: this is a multi-order, "
                            "multi-date statement total (one statement can span many orders "
                            "across days), and no official doc/response we've seen defines its "
                            "business formula for the VN market. Using it would silently mix "
                            "unrelated orders into a single-day HH metric.",
        "settled_or_unsettled": "settled only", "confidence": "LOW — explicitly not applicable until proven",
        "evidence": "finance_statements_<date>.csv net_sales_amount column",
    },
    {
        "hh_metric": "Return Refund Amount", "tiktok_field": "refund_amount",
        "endpoint": "POST /return_refund/202602/returns/search",
        "business_meaning": "Buyer refund amount on an approved/executed return",
        "include_exclude": "EXCLUDE as a separate deduction — for CANCELLED orders the "
                            "refund is already netted into revenue_amount=0 at settlement "
                            "(Section 1 finding); subtracting it again would double-count",
        "settled_or_unsettled": "cross-cuts both", "confidence": "MEDIUM (n=1 sample so far)",
        "evidence": "returns_<date>.csv",
    },
    {
        "hh_metric": "Estimated Revenue (pre-settlement)", "tiktok_field": "est_revenue_amount",
        "endpoint": "GET /finance/202507/orders/unsettled",
        "business_meaning": "Forward-looking estimate before final settlement — TikTok's own "
                             "docs call this 'subject to change... for reference only'",
        "include_exclude": "EXCLUDE from Net Sales; usable only as ESTIMATED FINANCE VALUE "
                            "(a separate, clearly-labeled line — Section 4)",
        "settled_or_unsettled": "unsettled only", "confidence": "MEDIUM (TikTok itself "
                                "documents this as non-final)",
        "evidence": "finance_unsettled_<date>.csv / order_finance_lifecycle_<date>.csv "
                    "estimated_revenue column",
    },
]


def net_sales_verdict(lifecycle_rows: list[dict]) -> dict[str, Any]:
    eligible = [r for r in lifecycle_rows if r["cancelled"] != "True"]
    settled = [r for r in eligible if r["finance_state"] == "SETTLED"]
    coverage = (len(settled) / len(eligible)) if eligible else 0.0

    if not eligible:
        return {"status": "NOT YET RECONCILED", "mapping": "AMBER",
                "value": None, "reason": "no eligible orders on this date"}

    if coverage >= 1.0:
        net_sales = sum((D(r["final_revenue"]) for r in settled), Decimal("0"))
        return {
            "status": "WORKING (proven via full-coverage reconciliation)",
            "mapping": "PASS",
            "value": str(net_sales),
            "basis": "SUM(revenue_amount) over finance_state=SETTLED eligible orders "
                     f"— {len(settled)}/{len(eligible)} eligible orders reached SETTLED "
                     "(100% coverage) on this date",
            "reason": f"{len(settled)}/{len(eligible)} eligible orders SETTLED (100%)",
        }
    if coverage == 0.0:
        return {
            "status": "NOT YET RECONCILED", "mapping": "AMBER",
            "value": None,
            "reason": f"0/{len(eligible)} eligible orders SETTLED yet — normal for a D-1 date, "
                      "revisit once settlement completes",
        }
    net_sales_partial = sum((D(r["final_revenue"]) for r in settled), Decimal("0"))
    return {
        "status": "PARTIAL — NOT REPRESENTATIVE", "mapping": "AMBER",
        "value": str(net_sales_partial),
        "basis": f"SUM(revenue_amount) over the {len(settled)}/{len(eligible)} eligible orders "
                 "that HAVE settled so far — excludes the rest, not a full-day Net Sales figure",
        "reason": f"{len(settled)}/{len(eligible)} eligible orders SETTLED "
                  f"({coverage:.0%}) — partial coverage",
    }


def four_values(report_date: str, orders: list[dict], product_rows: list[dict],
                 lifecycle_rows: list[dict]) -> dict[str, Any]:
    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    order_api_value = sum((D(o["total_amount"]) for o in eligible), Decimal("0"))

    analytics_gmv = sum((D(r.get("total_performance.gmv.amount")) for r in product_rows), Decimal("0"))

    unsettled = [r for r in lifecycle_rows if r["finance_state"] == "UNSETTLED"]
    estimated_finance_value = sum((D(r["estimated_settlement"]) for r in unsettled), Decimal("0"))

    settled = [r for r in lifecycle_rows if r["finance_state"] == "SETTLED"]
    final_settlement_value = sum((D(r["final_settlement"]) for r in settled), Decimal("0"))

    return {
        "ORDER_API_VALUE": str(order_api_value),
        "ANALYTICS_GMV": str(analytics_gmv),
        "ESTIMATED_FINANCE_VALUE": str(estimated_finance_value),
        "ESTIMATED_FINANCE_VALUE_basis": f"SUM(est_settlement_amount) over {len(unsettled)} "
                                          "UNSETTLED orders — TikTok's own pre-settlement "
                                          "estimate, subject to change",
        "FINAL_SETTLEMENT_VALUE": str(final_settlement_value),
        "FINAL_SETTLEMENT_VALUE_basis": f"SUM(final_settlement) over {len(settled)} SETTLED orders",
        "note": "These four values are NEVER forced equal — each has a different basis, "
                "coverage, and point in the order/settlement lifecycle.",
    }


# ---------------------------------------------------------------------------
# Section 7 — analytics basis kept separate (no summing across channels)
# ---------------------------------------------------------------------------

def analytics_basis(product_rows: list[dict], video_rows: list[dict], affiliate_rows: list[dict]) -> dict[str, Any]:
    product_gmv = sum((D(r.get("total_performance.gmv.amount")) for r in product_rows), Decimal("0"))
    sku_orders = sum(D(r.get("total_performance.sku_orders")) for r in product_rows)
    items_sold = sum(D(r.get("total_performance.items_sold")) for r in product_rows)
    video_gmv = sum((D(r.get("gmv.amount")) for r in video_rows), Decimal("0"))
    affiliate_orders = {r.get("order_id") for r in affiliate_rows if r.get("order_id")}

    return {
        "product_analytics_gmv": str(product_gmv),
        "sku_orders": str(sku_orders),
        "items_sold": str(items_sold),
        "video_attributed_gmv": str(video_gmv),
        "affiliate_eligible_orders": len(affiliate_orders),
        "note": "product_analytics_gmv and video_attributed_gmv are NOT summed — TikTok does "
                "not document them as mutually exclusive channels. affiliate_eligible_orders "
                "counts orders returned by Search Seller Affiliate Orders (commission-eligible), "
                "which is NOT the same as 'total affiliate-attributed GMV'.",
    }


# ---------------------------------------------------------------------------
# Section 8 — CEO report cleanup (headline vs evidence-layer separation)
# ---------------------------------------------------------------------------

def build_ceo_v2(report_date: str, orders: list[dict], lines: list[dict],
                  lifecycle_rows: list[dict], product_rows: list[dict], video_rows: list[dict],
                  returns_rows: list[dict], affiliate_rows: list[dict],
                  four_vals: dict, net_sales: dict, an_basis: dict) -> dict[str, Any]:
    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    cancelled = [o for o in orders if o["status"] == "CANCELLED"]
    order_total = sum((D(o["total_amount"]) for o in eligible), Decimal("0"))
    aov = (order_total / len(eligible)).quantize(Decimal("0.01")) if eligible else Decimal("0")

    impressions = sum(D(r.get("total_performance.product_impressions")) for r in product_rows)
    clicks = sum(D(r.get("total_performance.product_clicks")) for r in product_rows)
    ctr = (clicks / impressions).quantize(Decimal("0.0001")) if impressions else Decimal("0")
    cvr = (Decimal(len(eligible)) / impressions).quantize(Decimal("0.0001")) if impressions else Decimal("0")

    hero = sorted(product_rows, key=lambda r: D(r.get("total_performance.gmv.amount")), reverse=True)
    hero_sku = hero[0]["id"] if hero else None
    hero_gmv = hero[0].get("total_performance.gmv.amount") if hero else None

    settled = [r for r in lifecycle_rows if r["finance_state"] == "SETTLED"]
    recon_pct = (len(settled) / len(eligible) * 100) if eligible else 0

    exceptions = []
    unknown = [r for r in lifecycle_rows if r["finance_state"] == "UNKNOWN"]
    if unknown:
        exceptions.append(f"{len(unknown)} order(s) in an UNKNOWN finance state — see "
                           "order_finance_lifecycle csv 'evidence' column")
    missing = [r for r in lifecycle_rows if r["finance_state"] == "MISSING_FINANCE"]
    if missing:
        exceptions.append(f"{len(missing)} order(s) MISSING_FINANCE — not yet tracked by "
                           "TikTok's Unsettled Transactions feed (mostly pre-delivery orders)")
    if returns_rows:
        exceptions.append(f"{len(returns_rows)} return(s) on file for this date")

    return {
        "report_date": report_date,
        "shop": SHOP_NAME,
        "net_sales": {"status": net_sales["status"], "value": net_sales.get("value"),
                      "mapping": net_sales["mapping"]},
        "gmv": {
            "order_api_value": four_vals["ORDER_API_VALUE"],
            "analytics_gmv": four_vals["ANALYTICS_GMV"],
        },
        "orders": len(orders),
        "eligible_orders": len(eligible),
        "AOV": str(aov),
        "cancel_count": len(cancelled),
        "return_count": len(returns_rows),
        "traffic": {"impressions": str(impressions), "clicks": str(clicks)},
        "CTR": str(ctr),
        "CVR_working": str(cvr),
        "affiliate_eligible_orders": an_basis["affiliate_eligible_orders"],
        "hero_sku": {"product_id": hero_sku, "gmv": hero_gmv},
        "finance_reconciliation_pct": f"{recon_pct:.1f}%",
        "CM1_readiness": "NOT READY — COGS SOURCE NOT CONNECTED",
        "CM2_readiness": "NOT READY — WAITING ADS (TikTok API for Business Developer Profile UNDER REVIEW) + COGS not connected",
        "exceptions": exceptions[:5],
    }


def final_output_text(report_date: str, orders: list[dict], lifecycle_rows: list[dict],
                       net_sales: dict, settlement_mapping: str, four_vals: dict) -> str:
    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    cancelled = [o for o in orders if o["status"] == "CANCELLED"]
    state_counts = Counter(r["finance_state"] for r in lifecycle_rows)
    settled = [r for r in lifecycle_rows if r["finance_state"] == "SETTLED"]
    unsettled = [r for r in lifecycle_rows if r["finance_state"] == "UNSETTLED"]

    est_revenue = sum((D(r["estimated_revenue"]) for r in unsettled), Decimal("0"))
    est_fee = sum((D(r["estimated_fee"]) for r in unsettled), Decimal("0"))
    est_settlement = sum((D(r["estimated_settlement"]) for r in unsettled), Decimal("0"))
    final_revenue = sum((D(r["final_revenue"]) for r in settled), Decimal("0"))
    final_fee = sum((D(r["final_fee"]) for r in settled), Decimal("0"))
    final_shipping = sum((D(r["final_shipping"]) for r in settled), Decimal("0"))
    final_adjustment = sum((D(r["final_adjustment"]) for r in settled), Decimal("0"))
    final_settlement = sum((D(r["final_settlement"]) for r in settled), Decimal("0"))

    net_sales_mapping = net_sales["mapping"]
    cogs_connected = "NOT CONNECTED"
    cm1 = "NOT READY"
    cm2 = "WAITING ADS"

    if settlement_mapping == "PASS" and net_sales_mapping == "PASS":
        overall = "PASS"
    elif settlement_mapping == "FAIL" or net_sales_mapping == "FAIL":
        overall = "FAIL"
    else:
        overall = "AMBER"

    lines = [
        "TIKTOK FINANCE RECONCILIATION V2",
        "",
        f"Order date: {report_date}",
        f"Orders: {len(orders)}",
        f"Eligible: {len(eligible)}",
        f"Cancelled: {len(cancelled)}",
        "",
        f"Unsettled: {state_counts.get('UNSETTLED', 0)}",
        f"Settled: {state_counts.get('SETTLED', 0)}",
        f"Cancelled no settlement expected: {state_counts.get('CANCELLED_NO_SETTLEMENT_EXPECTED', 0)}",
        f"Missing finance: {state_counts.get('MISSING_FINANCE', 0)}",
        "",
        f"Estimated Revenue: {est_revenue}",
        f"Estimated Fees: {est_fee}",
        f"Estimated Settlement: {est_settlement}",
        "",
        f"Final Revenue: {final_revenue}",
        f"Final Fees: {final_fee}",
        f"Final Shipping: {final_shipping}",
        f"Final Adjustment: {final_adjustment}",
        f"Final Settlement: {final_settlement}",
        "",
        f"Settlement mapping: {settlement_mapping}",
        "",
        f"Net Sales mapping: {net_sales_mapping}",
        f"  (Net Sales value: {net_sales.get('value')}; {net_sales.get('reason')})",
        "",
        f"COGS: {cogs_connected}",
        "",
        f"CM1: {cm1}",
        "",
        f"CM2: {cm2}",
        "",
        f"Overall: {overall}",
    ]
    if overall != "PASS":
        lines.append("")
        lines.append("(Not calling this PRODUCTION READY — settlement mapping and/or "
                      "Net Sales mapping have not both reached PASS.)")
    return "\n".join(lines)


def build_ceo_markdown(report_date: str, ceo_v2: dict) -> Path:
    lines = [
        f"# CEO Report — {report_date}",
        "",
        f"Shop: **{SHOP_NAME}**",
        "",
        f"- **Net Sales**: {ceo_v2['net_sales']['status']}"
        + (f" — {ceo_v2['net_sales']['value']} VND" if ceo_v2['net_sales']['value'] else ""),
        f"- **GMV (Order API)**: {ceo_v2['gmv']['order_api_value']} VND",
        f"- **GMV (Analytics, native)**: {ceo_v2['gmv']['analytics_gmv']} VND",
        f"- **Orders**: {ceo_v2['orders']} total, {ceo_v2['eligible_orders']} eligible",
        f"- **AOV**: {ceo_v2['AOV']} VND",
        f"- **Cancelled / Returned**: {ceo_v2['cancel_count']} cancelled, {ceo_v2['return_count']} returned",
        f"- **Traffic**: {ceo_v2['traffic']['impressions']} impressions, {ceo_v2['traffic']['clicks']} clicks",
        f"- **CTR**: {ceo_v2['CTR']}   **CVR (working)**: {ceo_v2['CVR_working']}",
        f"- **Affiliate**: {ceo_v2['affiliate_eligible_orders']} eligible orders",
        f"- **Hero SKU**: {ceo_v2['hero_sku']['product_id']} (GMV {ceo_v2['hero_sku']['gmv']})",
        f"- **Finance reconciliation**: {ceo_v2['finance_reconciliation_pct']} of eligible orders settled",
        f"- **CM1**: {ceo_v2['CM1_readiness']}",
        f"- **CM2**: {ceo_v2['CM2_readiness']}",
        "",
        "## Exceptions",
        "",
    ]
    if ceo_v2["exceptions"]:
        for i, ex in enumerate(ceo_v2["exceptions"], 1):
            lines.append(f"{i}. {ex}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("_Technical counts (statements fetched, raw row counts, page counts) are "
                  "intentionally omitted from this layer — see DATA STATUS / evidence manifest._")
    path = REPORTS_DIR / f"CEO_REPORT_V2_{report_date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    report_date = args.date

    orders = read_csv(report_date, "orders")
    lines = read_csv(report_date, "order_lines")
    lifecycle_rows = read_csv(report_date, "order_finance_lifecycle")
    product_rows = read_csv(report_date, "product_analytics")
    video_rows = read_csv(report_date, "video_analytics")
    returns_rows = read_csv(report_date, "returns")
    affiliate_rows = read_csv(report_date, "affiliate_orders")

    if not lifecycle_rows:
        print(f"No order_finance_lifecycle_{report_date}.csv — run finance_lifecycle.py first.")
        return 1

    lifecycle_summary_path = REPORTS_DIR / f"finance_lifecycle_summary_{report_date}.json"
    settlement_mapping = "FAIL"
    if lifecycle_summary_path.exists():
        settlement_mapping = json.loads(lifecycle_summary_path.read_text())["settlement_mapping"]

    net_sales = net_sales_verdict(lifecycle_rows)
    four_vals = four_values(report_date, orders, product_rows, lifecycle_rows)
    an_basis = analytics_basis(product_rows, video_rows, affiliate_rows)
    ceo_v2 = build_ceo_v2(report_date, orders, lines, lifecycle_rows, product_rows, video_rows,
                           returns_rows, affiliate_rows, four_vals, net_sales, an_basis)

    out = {
        "report_date": report_date,
        "net_sales_mapping_table": NET_SALES_MAPPING,
        "net_sales_verdict": net_sales,
        "four_values": four_vals,
        "analytics_basis": an_basis,
        "ceo_v2": ceo_v2,
        "settlement_mapping": settlement_mapping,
    }
    out_path = REPORTS_DIR / f"finance_reconciliation_v2_{report_date}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    ceo_md_path = build_ceo_markdown(report_date, ceo_v2)

    text = final_output_text(report_date, orders, lifecycle_rows, net_sales, settlement_mapping, four_vals)
    print(text)
    print(f"\nWritten: {out_path}")
    print(f"CEO report (clean): {ceo_md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
