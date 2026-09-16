#!/usr/bin/env python3
"""
Business Mart (pipeline step 15) — Sections 4, 7, 9 of the follow-up spec.

Aggregates COMMERCIAL data only (orders, finance, product/video/LIVE
analytics, affiliate, COGS join) into one clean structure for the CEO
Daily Report. Deliberately excludes anything from
MARKETING_INTELLIGENCE_MART (bestselling products/creators/videos/LIVE,
Creator Marketplace, product opportunities) — this pilot never collected
those into pilot_reporting/normalized/ in the first place (they live only
in the separate ../data/raw/tiktok/ smoke-test snapshots from run_all.py),
so the separation already holds structurally; this module just documents
and enforces it going forward (see MARKETING_INTELLIGENCE_SOURCES below).

Reads only already-collected normalized/*.csv + reports/*.json for the
date. Makes no API calls. Does not alter finance_lifecycle.py /
reconciliation_v21.py logic (already PASS) — reads their outputs as-is.
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from pilot_common import NORMALIZED_DIR, REPORTS_DIR
import cogs_mapping

# Data this pipeline explicitly does NOT touch — kept separate by design.
# If any of these are ever collected, they must land in a
# MARKETING_INTELLIGENCE_MART, never merged into normalized/ commercial
# tables or the CEO Daily Report's BUSINESS/ECONOMICS sections.
MARKETING_INTELLIGENCE_SOURCES = [
    "Get Bestselling Products", "Get Bestselling Creators",
    "Get Bestselling Videos", "Get Bestselling LIVE Sessions",
    "Creator Marketplace search", "Product Opportunities / Diagnose and Optimize Product",
]

COGS_COVERAGE_THRESHOLD_PCT = 95.0  # PLACEHOLDER — not yet business-approved


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


def read_json(path_obj) -> dict:
    return json.loads(path_obj.read_text(encoding="utf-8")) if path_obj.exists() else {}


FINALIZED_STATES = {"SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}


def build(report_date: str) -> dict[str, Any]:
    orders = read_csv(report_date, "orders")
    lines = read_csv(report_date, "order_lines")
    lifecycle = read_csv(report_date, "order_finance_lifecycle")
    product_rows = read_csv(report_date, "product_analytics")
    video_rows = read_csv(report_date, "video_analytics")
    live_rows = read_csv(report_date, "live_analytics")
    affiliate_rows = read_csv(report_date, "affiliate_orders")
    returns_rows = read_csv(report_date, "returns")
    products_rows = read_csv(report_date, "products")

    # ---- BUSINESS ----
    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    cancelled = [o for o in orders if o["status"] == "CANCELLED"]
    gmv = sum((D(o["total_amount"]) for o in eligible), Decimal("0"))
    aov = (gmv / len(eligible)).quantize(Decimal("0.01")) if eligible else Decimal("0")

    finalized = [r for r in lifecycle if r["finance_state"] in FINALIZED_STATES]
    net_sales_candidate = sum((D(r["final_revenue"]) for r in finalized), Decimal("0"))
    net_sales_coverage_pct = (len(finalized) / len(eligible) * 100) if eligible else 0.0

    business = {
        "orders": len(orders),
        "eligible_orders": len(eligible),
        "units": len(lines),
        "gmv": str(gmv),
        "gmv_basis": "SUM(payment.total_amount), Order API, eligible (non-cancelled) orders",
        "hh_net_sales_candidate": str(net_sales_candidate),
        "hh_net_sales_candidate_basis": "SUM(revenue_amount) over finance_state IN "
                                        "(SETTLED, STATEMENT_ISSUED, PAYMENT_PENDING, PAID)",
        "hh_net_sales_candidate_coverage_pct": round(net_sales_coverage_pct, 1),
        "hh_net_sales_candidate_metadata": {
            "metric_name": "HH_NET_SALES_CANDIDATE",  # never renamed to NET_SALES_FINAL
            "source": "Finance revenue_amount (GET /finance/202501/orders/{order_id}/statement_transactions)",
            "business_signoff": "PENDING",
        },
        "aov": str(aov),
        "cancel_count": len(cancelled),
        "return_count": len(returns_rows),
    }

    # ---- FUNNEL ----
    impressions = sum(D(r.get("total_performance.product_impressions")) for r in product_rows)
    clicks = sum(D(r.get("total_performance.product_clicks")) for r in product_rows)
    sku_orders = sum(D(r.get("total_performance.sku_orders")) for r in product_rows)
    ctr = (clicks / impressions).quantize(Decimal("0.0001")) if impressions else Decimal("0")
    cvr = (sku_orders / clicks).quantize(Decimal("0.0001")) if clicks else Decimal("0")
    funnel = {
        "impressions": str(impressions),
        "product_clicks": str(clicks),
        "ctr": str(ctr),
        "cvr_ctor": str(cvr),
        "orders_from_analytics": str(sku_orders),
    }

    # ---- ECONOMICS (COGS / GM / CM1 / CM2) ----
    cogs = cogs_mapping.join_lines(report_date)
    cogs_ready = (
        cogs["cogs_source_connected"]
        and cogs["value_coverage_pct"] >= COGS_COVERAGE_THRESHOLD_PCT
    )
    fee_sum = sum((D(r["final_fee"]) for r in finalized), Decimal("0"))
    shipping_sum = sum((D(r["final_shipping"]) for r in finalized), Decimal("0"))

    if cogs_ready:
        cogs_value = D(cogs["matched_value"])  # placeholder wiring — real COGS currency amount
        gm_working = net_sales_candidate - cogs_value
        cm1_working = gm_working + fee_sum + shipping_sum  # fee/shipping already negative
        gm_status = "READY"
        cm1_status = "READY"
    else:
        gm_working = None
        cm1_working = None
        gm_status = "NOT READY — COGS SOURCE NOT CONNECTED" if not cogs["cogs_source_connected"] \
            else f"NOT READY — COGS coverage {cogs['value_coverage_pct']}% below approved threshold {COGS_COVERAGE_THRESHOLD_PCT}%"
        cm1_status = gm_status

    economics = {
        "cogs_coverage_value_pct": cogs["value_coverage_pct"],
        "cogs_coverage_unit_pct": cogs["unit_coverage_pct"],
        "cogs_coverage_threshold_pct": COGS_COVERAGE_THRESHOLD_PCT,
        "cogs_coverage_threshold_note": "PLACEHOLDER — not yet business-approved",
        "cogs_missing_sku_count": cogs["missing_sku_count"],
        "gross_margin_working": str(gm_working) if gm_working is not None else None,
        "gross_margin_status": gm_status,
        "fees": str(fee_sum),
        "shipping": str(shipping_sum),
        "cm1_working": str(cm1_working) if cm1_working is not None else None,
        "cm1_status": cm1_status,
        "ads_status": "WAITING_ADS_API",
        "cm2_final_status": "WAITING_ADS_API",
    }

    # ---- GROWTH ----
    video_gmv = sum((D(r.get("gmv.amount")) for r in video_rows), Decimal("0"))
    live_gmv = sum((D(r.get("sales_performance.gmv.amount")) for r in live_rows), Decimal("0"))
    affiliate_order_ids = {r.get("order_id") for r in affiliate_rows if r.get("order_id")}
    growth = {
        "affiliate_eligible_orders": len(affiliate_order_ids),
        "video_count": len(video_rows),
        "video_gmv": str(video_gmv),
        "live_sessions": len(live_rows),
        "live_gmv": str(live_gmv),
    }

    # ---- HERO SKU (top 10 by native Analytics GMV — never invented) ----
    # Hero SKU is keyed by product_id (that's what Get Shop Product
    # Performance List's "id" field is) — aggregate inventory to the same
    # grain (sum across all of that product's SKUs), not per-seller_sku.
    stock_by_product: dict[str, int] = {}
    for p in products_rows:
        prod_id = p.get("product_id")
        if prod_id:
            stock_by_product[prod_id] = stock_by_product.get(prod_id, 0) + int(p.get("inventory_total_qty") or 0)
    return_order_ids = {r["order_id"] for r in returns_rows}
    return_count_by_product: dict[str, int] = {}
    for li in lines:
        if li["order_id"] in return_order_ids:
            pid = li.get("product_id")
            return_count_by_product[pid] = return_count_by_product.get(pid, 0) + 1

    top10 = sorted(product_rows, key=lambda r: D(r.get("total_performance.gmv.amount")), reverse=True)[:10]
    hero_skus = []
    for r in top10:
        pid = r.get("id")
        sku_ord = D(r.get("total_performance.sku_orders"))
        r_clicks = D(r.get("total_performance.product_clicks"))
        sku_cvr = (sku_ord / r_clicks).quantize(Decimal("0.0001")) if r_clicks else Decimal("0")
        hero_skus.append({
            "product_id": pid,
            "gmv": r.get("total_performance.gmv.amount"),
            "currency": r.get("total_performance.gmv.currency"),
            "impressions": r.get("total_performance.product_impressions"),
            "clicks": r.get("total_performance.product_clicks"),
            "sku_orders": str(sku_ord),
            "cvr": str(sku_cvr),
            "stock_qty": stock_by_product.get(pid),
            "return_count": return_count_by_product.get(pid, 0),
        })

    # ---- EXCEPTIONS (max 5) ----
    exceptions = []
    unknown = [r for r in lifecycle if r["finance_state"] == "UNKNOWN"]
    if unknown:
        exceptions.append(f"{len(unknown)} order(s) in an UNKNOWN finance state — manual review needed")
    missing_finance = [r for r in lifecycle if r["finance_state"] == "MISSING_FINANCE"]
    if missing_finance:
        exceptions.append(f"{len(missing_finance)} order(s) not yet tracked by TikTok's Unsettled "
                          "Transactions feed (normal for pre-delivery orders)")
    if not cogs["cogs_source_connected"]:
        exceptions.append("COGS source not connected — GM/CM1 blocked, Net Sales unaffected")
    if len(cancelled) > 0:
        exceptions.append(f"{len(cancelled)}/{len(orders)} orders cancelled")
    pending_returns = [r for r in returns_rows if r.get("return_status") not in
                        ("RETURN_OR_REFUND_REQUEST_COMPLETE", "RETURN_OR_REFUND_REQUEST_CANCEL",
                         "REFUND_OR_RETURN_REQUEST_REJECT")]
    if pending_returns:
        exceptions.append(f"{len(pending_returns)} return(s) still pending resolution")

    # ---- ACTIONS (max 3) ----
    actions = []
    if not cogs["cogs_source_connected"]:
        actions.append("Connect an authoritative HH SKU/COGS master to "
                       "pilot_reporting/cogs_master/hh_sku_cogs_master.csv to unblock GM/CM1")
    if net_sales_coverage_pct < 100:
        actions.append(f"Revisit this date's HH_NET_SALES_CANDIDATE once "
                       f"{len(eligible) - len(finalized)} remaining order(s) settle "
                       "(rolling backfill tracks this automatically)")
    actions.append("Escalate reports/HH_TIKTOK_PAYMENT_ID_SUPPORT_CASE.md to TikTok support to "
                   "unblock Cash Reconciliation")

    return {
        "report_date": report_date,
        "BUSINESS": business,
        "FUNNEL": funnel,
        "ECONOMICS": economics,
        "GROWTH": growth,
        "HERO_SKU": hero_skus,
        "EXCEPTIONS": exceptions[:5],
        "ACTIONS": actions[:3],
        "marketing_intelligence_note": (
            f"The following are intentionally NOT in this mart: {MARKETING_INTELLIGENCE_SOURCES}"
        ),
    }
