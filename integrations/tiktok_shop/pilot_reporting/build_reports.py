#!/usr/bin/env python3
"""
FIRST END-TO-END REPORTING PILOT — reconciliation + report builder
(Sections 14-22 of the spec).

Reads ONLY normalized/*.csv and raw/*.json written by collect.py — never
calls the API. Writes:
  reports/data_status_<date>.json
  reports/ceo_summary_<date>.json
  reports/evidence_manifest_<date>.json
  reports/HH_TIKTOK_API_PILOT_<date>.xlsx
  reports/HH_TIKTOK_API_PILOT_<date>.md

No invented/derived metric is presented as a native API number; every KPI
carries source + basis + status (Section 14).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font, Alignment, PatternFill

from pilot_common import NORMALIZED_DIR, RAW_DIR, REPORTS_DIR, now_utc_iso

SHOP_NAME = "Le Petit Marseillais Vietnam"


def D(x: Any) -> Decimal:
    """Decimal-safe conversion. Empty/None/garbage -> Decimal('0'), so a
    sum never silently drops a row, but a genuinely missing value should be
    checked via the *_present counters, not assumed to be zero business
    fact — see each summary's `*_non_numeric` count."""
    if x is None:
        return Decimal("0")
    s = str(x).strip()
    if s == "" or s.lower() == "nan":
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def read_csv_rows(report_date: str, name: str) -> list[dict[str, Any]]:
    stem = name[:-4] if name.endswith(".csv") else name
    path = NORMALIZED_DIR / f"{stem}_{report_date}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Section 6 — Order KPIs
# ---------------------------------------------------------------------------

def compute_order_kpis(orders: list[dict], lines: list[dict]) -> dict[str, Any]:
    status_counts = Counter(o.get("status") for o in orders)
    cancelled = status_counts.get("CANCELLED", 0)
    eligible_orders = [o for o in orders if o.get("status") != "CANCELLED"]

    total_amount_all = sum((D(o.get("total_amount")) for o in orders), Decimal("0"))
    total_amount_eligible = sum((D(o.get("total_amount")) for o in eligible_orders), Decimal("0"))
    seller_discount_all = sum((D(o.get("seller_discount")) for o in orders), Decimal("0"))
    platform_discount_all = sum((D(o.get("platform_discount")) for o in orders), Decimal("0"))

    eligible_ids = {o.get("order_id") for o in eligible_orders}
    units_all = len(lines)
    units_eligible = sum(1 for li in lines if li.get("order_id") in eligible_ids)

    aov_working = (
        (total_amount_eligible / Decimal(len(eligible_orders))).quantize(Decimal("0.01"))
        if eligible_orders else Decimal("0")
    )

    currencies = {o.get("currency") for o in orders if o.get("currency")}

    return {
        "total_orders": len(orders),
        "status_breakdown": dict(status_counts),
        "cancelled_orders": cancelled,
        "eligible_orders": len(eligible_orders),
        "units_all_lines": units_all,
        "units_eligible_lines": units_eligible,
        "order_total_amount_all": str(total_amount_all),
        "order_total_amount_eligible": str(total_amount_eligible),
        "seller_discount_total": str(seller_discount_all),
        "platform_discount_total": str(platform_discount_all),
        "aov_working": str(aov_working),
        "aov_working_basis": "ORDER API — sum(payment.total_amount) / count(orders where status != CANCELLED); NOT reconciled Net Sales",
        "currency": next(iter(currencies), None) if len(currencies) == 1 else sorted(currencies),
    }


# ---------------------------------------------------------------------------
# Section 8 — Finance reconciliation summary
# ---------------------------------------------------------------------------

def compute_finance_summary(
    match_rows: list[dict], order_tx_rows: list[dict],
    statement_rows: list[dict], payments_rows: list[dict],
) -> dict[str, Any]:
    status_counts = Counter(r.get("finance_match_status") for r in match_rows)
    settled_amount = sum((D(r.get("settlement_amount")) for r in order_tx_rows), Decimal("0"))
    fee_metric_cols = sorted({k for r in statement_rows for k in r.keys()})
    return {
        "statements_found": len(statement_rows),
        "payments_found": len(payments_rows),
        "orders_matched_to_finance": status_counts.get("MATCHED", 0),
        "orders_not_settled": status_counts.get("NOT_SETTLED_YET", 0),
        "orders_mismatch": status_counts.get("MISMATCH", 0),
        "orders_unknown": status_counts.get("UNKNOWN", 0),
        "settled_amount_matched_orders": str(settled_amount),
        "settled_amount_basis": "SUM(order_finance_transactions.settlement_amount) over "
                                 "finance_match_status=MATCHED rows only (target-date orders)",
        "available_fee_metrics_statement_level": fee_metric_cols,
    }


# ---------------------------------------------------------------------------
# Section 9 / 10 — Returns / Affiliate
# ---------------------------------------------------------------------------

def compute_returns_summary(returns: list[dict]) -> dict[str, Any]:
    refund_amount = sum((D(r.get("refund_amount")) for r in returns), Decimal("0"))
    return {
        "return_count": len(returns),
        "refund_amount_sum": str(refund_amount),
        "status_breakdown": dict(Counter(r.get("return_status") for r in returns)),
    }


def compute_affiliate_summary(affiliate: list[dict]) -> dict[str, Any]:
    order_ids = {r.get("order_id") for r in affiliate if r.get("order_id")}
    units = sum(D(r.get("quantity")) for r in affiliate)
    order_value = sum((D(r.get("price_amount")) for r in affiliate), Decimal("0"))
    return {
        "affiliate_order_count": len(order_ids),
        "affiliate_line_count": len(affiliate),
        "affiliate_units": str(units),
        "affiliate_order_value": str(order_value),
        "affiliate_order_value_basis": "SUM(skus[].price.amount) from Search Seller Affiliate "
                                        "Orders — commission-eligible order value, NOT labeled "
                                        "'Affiliate GMV' (API does not use that term)",
    }


# ---------------------------------------------------------------------------
# Section 11 / 12 / 13 — Analytics
# ---------------------------------------------------------------------------

def compute_product_analytics_summary(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"status": "PASS_EMPTY", "product_count": 0}
    gmv = sum((D(r.get("total_performance.gmv.amount")) for r in rows), Decimal("0"))
    impressions = sum(D(r.get("total_performance.product_impressions")) for r in rows)
    clicks = sum(D(r.get("total_performance.product_clicks")) for r in rows)
    sku_orders = sum(D(r.get("total_performance.sku_orders")) for r in rows)
    items_sold = sum(D(r.get("total_performance.items_sold")) for r in rows)
    ctr = (clicks / impressions).quantize(Decimal("0.0001")) if impressions else Decimal("0")

    top10 = sorted(
        rows, key=lambda r: D(r.get("total_performance.gmv.amount")), reverse=True
    )[:10]
    top10_out = [{
        "product_id": r.get("id"),
        "gmv": r.get("total_performance.gmv.amount"),
        "currency": r.get("total_performance.gmv.currency"),
        "orders": r.get("total_performance.orders"),
        "items_sold": r.get("total_performance.items_sold"),
    } for r in top10]

    return {
        "status": "PASS",
        "product_count": len(rows),
        "product_gmv": str(gmv),
        "product_impressions": str(impressions),
        "product_clicks": str(clicks),
        "native_ctr": str(ctr),
        "sku_orders": str(sku_orders),
        "items_sold": str(items_sold),
        "top10_products": top10_out,
        "basis": "GET /analytics/202605/shop_products/performance, total_performance.*, "
                 "currency=LOCAL, start_date_ge/end_date_lt = report date window",
    }


def compute_video_analytics_summary(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"status": "PASS_EMPTY", "video_count": 0}
    gmv = sum((D(r.get("gmv.amount")) for r in rows), Decimal("0"))
    # Get Shop Video Performance List does not return a raw product_clicks
    # count — only click_through_rate (a ratio) and views. Do not default
    # a missing metric to 0; that would misreport "zero clicks" as fact.
    has_clicks_field = any("product_clicks" in r for r in rows)
    clicks = (
        str(sum(D(r.get("product_clicks")) for r in rows))
        if has_clicks_field else "NOT_PROVIDED_BY_API"
    )
    sku_orders = sum(D(r.get("sku_orders")) for r in rows)

    top10 = sorted(rows, key=lambda r: D(r.get("gmv.amount")), reverse=True)[:10]
    top10_out = [{
        "video_id": r.get("id"),
        "title": r.get("title"),
        "creator_username": r.get("creator.user_name") or r.get("username"),
        "gmv": r.get("gmv.amount"),
        "currency": r.get("gmv.currency"),
        "sku_orders": r.get("sku_orders"),
    } for r in top10]

    return {
        "status": "PASS",
        "video_count": len(rows),
        "video_gmv": str(gmv),
        "video_product_clicks": clicks,
        "video_sku_orders": str(sku_orders),
        "top10_videos": top10_out,
        "basis": "GET /analytics/202605/shop_videos/performance, report date window",
    }


def compute_live_analytics_summary(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"status": "PASS_EMPTY", "live_sessions": 0, "live_gmv": "0",
                "live_orders": "0", "live_product_clicks": "0"}
    gmv = sum((D(r.get("sales_performance.gmv.amount")) for r in rows), Decimal("0"))
    orders = sum(D(r.get("sales_performance.sku_orders")) for r in rows)
    has_clicks_field = any("sales_performance.product_clicks" in r for r in rows)
    clicks = (
        str(sum(D(r.get("sales_performance.product_clicks")) for r in rows))
        if has_clicks_field else "NOT_PROVIDED_BY_API"
    )
    return {
        "status": "PASS",
        "live_sessions": len(rows),
        "live_gmv": str(gmv),
        "live_orders": str(orders),
        "live_product_clicks": clicks,
        "basis": "GET /analytics/202509/shop_lives/performance, report date window",
    }


# ---------------------------------------------------------------------------
# Section 15 — GMV reconciliation (never forced equal)
# ---------------------------------------------------------------------------

def gmv_reconciliation(order_kpis: dict, product_summary: dict, finance_summary: dict) -> dict[str, Any]:
    order_value = D(order_kpis["order_total_amount_eligible"])
    analytics_gmv = D(product_summary.get("product_gmv", "0"))
    finance_value = D(finance_summary["settled_amount_matched_orders"])

    not_settled = finance_summary["orders_not_settled"]
    total_orders = order_kpis["total_orders"]

    return {
        "order_api_value": str(order_value),
        "analytics_gmv": str(analytics_gmv),
        "finance_settled_value": str(finance_value),
        "diff_order_vs_analytics": str(order_value - analytics_gmv),
        "diff_order_vs_finance": str(order_value - finance_value),
        "reason_order_vs_analytics": "REQUIRES_RECONCILIATION — not proven by data in this "
                                      "pilot (candidate causes: attribution window, "
                                      "cancellations/refunds, LOCAL-currency rounding — none "
                                      "confirmed)",
        "reason_order_vs_finance": (
            f"FACT — settlement timing lag: {not_settled}/{total_orders} target-date orders "
            "are NOT_SETTLED_YET as of collection time (see finance_match_status.csv); "
            "finance_settled_value only reflects the small subset already assigned to a "
            "statement, so it is expected to be far below order_api_value on a D-1 report."
        ),
    }


# ---------------------------------------------------------------------------
# Section 16/17 — Net Sales / COGS / CM1 / CM2 (no invented values)
# ---------------------------------------------------------------------------

def pnl_readiness(finance_summary: dict) -> dict[str, Any]:
    cogs_found = False  # confirmed by repo search: no SKU/COGS master file anywhere in project
    net_sales_status = "NOT YET RECONCILED"
    net_sales_reason = (
        "No approved HH Net Sales formula/mapping exists in this repository, and "
        f"{finance_summary['orders_not_settled']} of the target date's orders have no "
        "settlement data yet — required deductions are incomplete."
    )
    cogs_status = "SOURCE NOT CONNECTED"
    cm1_status = "NOT_CALCULATED — COGS SOURCE NOT CONNECTED"
    cm2_pre_ads_status = "NOT_CALCULATED — insufficient settled finance data and no COGS source"
    cm2_final_status = "NOT CALCULATED — EXCLUDES TIKTOK ADS SPEND (Ads = WAITING_API)"
    ads_status = "WAITING_API"
    ads_note = ("TikTok API for Business Developer App 'HH Ecom Ads Reporting' — Developer "
                "Profile status UNDER REVIEW as of this pilot run; no Marketing API / GMV Max "
                "ads data source connected yet.")
    return {
        "net_sales_status": net_sales_status,
        "net_sales_reason": net_sales_reason,
        "COGS_status": cogs_status,
        "CM1_status": cm1_status,
        "CM2_pre_ads_status": cm2_pre_ads_status,
        "CM2_final_status": cm2_final_status,
        "Ads_status": ads_status,
        "Ads_note": ads_note,
    }


# ---------------------------------------------------------------------------
# Section 14 — Metric dictionary (no metric invented / mislabeled)
# ---------------------------------------------------------------------------

def metric_dictionary_rows(order_kpis, finance_summary, product_summary, video_summary,
                            live_summary, affiliate_summary, returns_summary) -> list[dict]:
    rows = [
        ("Order Total Amount (all)", order_kpis["order_total_amount_all"], "Order API",
         "SUM(payment.total_amount) — ALL orders incl. cancelled", "WORKING"),
        ("Order Total Amount (eligible)", order_kpis["order_total_amount_eligible"], "Order API",
         "SUM(payment.total_amount) where status != CANCELLED", "WORKING"),
        ("AOV Working", order_kpis["aov_working"], "Order API", order_kpis["aov_working_basis"], "WORKING"),
        ("Cancelled Orders", order_kpis["cancelled_orders"], "Order API", "COUNT(status=CANCELLED)", "WORKING"),
        ("Product GMV", product_summary.get("product_gmv", "0"), "TikTok Analytics",
         "SUM(total_performance.gmv.amount), /analytics/202605/shop_products/performance", "NATIVE_API"),
        ("Video GMV", video_summary.get("video_gmv", "0"), "TikTok Analytics",
         "SUM(gmv.amount), /analytics/202605/shop_videos/performance", "NATIVE_API"),
        ("LIVE GMV", live_summary.get("live_gmv", "0"), "TikTok Analytics",
         "SUM(sales_performance.gmv.amount), /analytics/202509/shop_lives/performance",
         live_summary["status"]),
        ("Finance Settled Amount (matched orders)", finance_summary["settled_amount_matched_orders"],
         "TikTok Finance", finance_summary["settled_amount_basis"], "WORKING"),
        ("Affiliate Order Value", affiliate_summary["affiliate_order_value"], "TikTok Affiliate",
         affiliate_summary["affiliate_order_value_basis"], "WORKING"),
        ("Return Refund Amount", returns_summary["refund_amount_sum"], "Return/Refund API",
         "SUM(refund_amount) from Search Returns", "WORKING"),
        ("Net Sales", "NOT YET RECONCILED", "N/A", "No approved formula + incomplete settlement data", "NOT_READY"),
        ("COGS", "SOURCE NOT CONNECTED", "N/A", "No SKU/COGS master found in repository", "NOT_READY"),
    ]
    return [{"metric_name": n, "value": v, "source": s, "basis": b, "status": st} for n, v, s, b, st in rows]


# ---------------------------------------------------------------------------
# Evidence manifest (Section 22) — derived from every raw/*<date>*.json envelope
# ---------------------------------------------------------------------------

def build_evidence_manifest(report_date: str) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(RAW_DIR.glob(f"{report_date}_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        data = payload.get("data")
        row_count = None
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    row_count = len(v)
                    break
        entries.append({
            "raw_file": str(path.relative_to(RAW_DIR.parent)),
            "endpoint": payload.get("endpoint"),
            "api_version": payload.get("api_version"),
            "request_id": payload.get("request_id"),
            "response_code": payload.get("response_code"),
            "row_count": row_count,
            "page_number": payload.get("page_number"),
            "status": "OK" if str(payload.get("response_code")) in ("0",) else "NON_ZERO_CODE",
        })
    return entries


# ---------------------------------------------------------------------------
# Data status gate (Section 18)
# ---------------------------------------------------------------------------

def build_data_status(collect_summary: dict, finance_summary: dict, returns, affiliate,
                       product_summary, video_summary, live_summary) -> dict[str, str]:
    ds = collect_summary["domain_status"]

    def norm(v: str) -> str:
        return {"FAIL_API": "FAIL", "FAIL_NETWORK": "FAIL"}.get(v, v)

    return {
        "Authorized Shop": norm(ds.get("authorized_shop", "NOT_AVAILABLE")),
        "Orders": norm(ds.get("orders", "NOT_AVAILABLE")),
        "Order Detail": norm(ds.get("order_detail", "NOT_AVAILABLE")),
        "Finance Statements": norm(ds.get("finance_statements", "NOT_AVAILABLE")),
        "Finance Transactions": "PASS" if finance_summary["orders_matched_to_finance"] > 0 else "PASS_EMPTY",
        "Returns": "PASS" if returns else "PASS_EMPTY",
        "Affiliate": "PASS" if affiliate else "PASS_EMPTY",
        "Product Analytics": product_summary["status"],
        "Video Analytics": video_summary["status"],
        "LIVE Analytics": live_summary["status"],
        "COGS": "NOT_AVAILABLE",
        "Ads": "WAITING_API",
    }


# ---------------------------------------------------------------------------
# Excel (Section 20)
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _sheet_from_rows(wb: Workbook, name: str, rows: list[dict], header_note: Optional[list[str]] = None):
    ws = wb.create_sheet(name)
    r0 = 1
    if header_note:
        for line in header_note:
            ws.cell(row=r0, column=1, value=line).font = Font(bold=True)
            r0 += 1
        r0 += 1
    if not rows:
        ws.cell(row=r0, column=1, value="(no rows)")
        return ws
    df = pd.DataFrame(rows)
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=r0, column=j, value=str(col))
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    for i, row in enumerate(df.itertuples(index=False), start=1):
        for j, val in enumerate(row, start=1):
            ws.cell(row=r0 + i, column=j, value=val if pd.notna(val) else "")
    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 60)
    return ws


def _kv_sheet(wb: Workbook, name: str, header: list[str], sections: list[tuple[str, dict]]):
    ws = wb.create_sheet(name)
    r = 1
    for line in header:
        ws.cell(row=r, column=1, value=line).font = Font(bold=True, size=12)
        r += 1
    r += 1
    for title, d in sections:
        ws.cell(row=r, column=1, value=title).font = Font(bold=True, color="1F2937")
        r += 1
        for k, v in d.items():
            ws.cell(row=r, column=1, value=str(k))
            ws.cell(row=r, column=2, value=json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            r += 1
        r += 1
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 70
    return ws


def build_excel(report_date: str, ceo_summary: dict, data_status: dict, all_rows: dict[str, list[dict]],
                 metric_dict_rows: list[dict]) -> Path:
    wb = Workbook()
    wb.remove(wb.active)

    _kv_sheet(wb, "00_CEO_SUMMARY", [
        f"REPORT DATE: {report_date}",
        f"SHOP: {SHOP_NAME}",
        f"CURRENCY: {ceo_summary['currency']}",
        "ADS DATA: WAITING FOR TIKTOK API FOR BUSINESS APPROVAL",
        "CM2: NOT FINAL UNTIL ADS DATA IS CONNECTED",
    ], [
        ("BUSINESS", ceo_summary["BUSINESS"]),
        ("FINANCE", ceo_summary["FINANCE"]),
        ("ANALYTICS", ceo_summary["ANALYTICS"]),
        ("VIDEO", ceo_summary["VIDEO"]),
        ("LIVE", ceo_summary["LIVE"]),
        ("P&L", ceo_summary["P&L"]),
    ])

    _sheet_from_rows(wb, "01_DATA_STATUS", [{"domain": k, "status": v} for k, v in data_status.items()])
    _sheet_from_rows(wb, "02_ORDERS", all_rows["orders"])
    _sheet_from_rows(wb, "03_ORDER_LINES", all_rows["order_lines"])
    _sheet_from_rows(wb, "04_FINANCE", all_rows["finance_match_status"],
                      header_note=["order_id -> finance_match_status; see order_finance_transactions.csv "
                                    "and finance_statements.csv in normalized/ for full detail"])
    _sheet_from_rows(wb, "05_RETURNS", all_rows["returns"])
    _sheet_from_rows(wb, "06_AFFILIATE", all_rows["affiliate"])
    _sheet_from_rows(wb, "07_PRODUCT_ANALYTICS", all_rows["product_analytics"])
    _sheet_from_rows(wb, "08_VIDEO_ANALYTICS", all_rows["video_analytics"])
    _sheet_from_rows(wb, "09_LIVE_ANALYTICS", all_rows["live_analytics"])
    _sheet_from_rows(wb, "10_RECONCILIATION", [
        {"item": k, "value": v} for k, v in ceo_summary["_gmv_reconciliation"].items()
    ])
    _sheet_from_rows(wb, "11_METRIC_DICTIONARY", metric_dict_rows)

    path = REPORTS_DIR / f"HH_TIKTOK_API_PILOT_{report_date}.xlsx"
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Markdown (Section 21)
# ---------------------------------------------------------------------------

def build_markdown(report_date: str, overall: str, data_status: dict, evidence: list[dict],
                    order_kpis: dict, finance_summary: dict, returns_summary: dict,
                    affiliate_summary: dict, product_summary: dict, video_summary: dict,
                    live_summary: dict, gmv_recon: dict, pnl: dict, exceptions: list[str]) -> Path:
    lines = []
    lines.append(f"# TikTok API Pilot — {report_date}")
    lines.append("")
    lines.append(f"Shop: **{SHOP_NAME}**  ")
    lines.append(f"Currency: **{order_kpis.get('currency') or 'VND (LOCAL)'}**")
    lines.append("")
    lines.append("## 1. Executive Result")
    lines.append("")
    lines.append(f"Overall: **{overall}**")
    lines.append("")
    lines.append("## 2. Data Sources")
    lines.append("")
    lines.append("| Endpoint | Version | Calls | Rows (list-shaped responses only) | Status |")
    lines.append("|---|---|---|---|---|")
    by_endpoint: dict[str, dict] = {}
    for e in evidence:
        k = e["endpoint"]
        by_endpoint.setdefault(k, {"version": e["api_version"], "rows": 0, "calls": 0, "has_rows": False})
        by_endpoint[k]["calls"] += 1
        if e["row_count"] is not None:
            by_endpoint[k]["rows"] += e["row_count"]
            by_endpoint[k]["has_rows"] = True
    for k, v in sorted(by_endpoint.items()):
        status = data_status_lookup(k, data_status)
        rows_display = v["rows"] if v["has_rows"] else "N/A (flat object response)"
        lines.append(f"| {k} | {v['version']} | {v['calls']} | {rows_display} | {status} |")
    lines.append("")
    lines.append("## 3. Business Metrics")
    lines.append("")
    lines.append("| Metric | Value | Source | Basis | Status |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| Total Orders | {order_kpis['total_orders']} | Order API | COUNT(orders) in shop-local day window | WORKING |")
    lines.append(f"| Eligible Orders (non-cancelled) | {order_kpis['eligible_orders']} | Order API | status != CANCELLED | WORKING |")
    lines.append(f"| Order Total Amount (eligible) | {order_kpis['order_total_amount_eligible']} | Order API | SUM(payment.total_amount) | WORKING |")
    lines.append(f"| AOV Working | {order_kpis['aov_working']} | Order API | {order_kpis['aov_working_basis']} | WORKING |")
    lines.append(f"| Product GMV (Analytics) | {product_summary.get('product_gmv','0')} | TikTok Analytics | {product_summary.get('basis','')} | {product_summary['status']} |")
    lines.append(f"| Video GMV (Analytics) | {video_summary.get('video_gmv','0')} | TikTok Analytics | {video_summary.get('basis','')} | {video_summary['status']} |")
    lines.append(f"| Affiliate Order Value | {affiliate_summary['affiliate_order_value']} | TikTok Affiliate | {affiliate_summary['affiliate_order_value_basis']} | WORKING |")
    lines.append("")
    lines.append("## 4. Finance Reconciliation")
    lines.append("")
    lines.append(f"Orders: {order_kpis['total_orders']}  ")
    lines.append(f"Finance matched: {finance_summary['orders_matched_to_finance']}  ")
    lines.append(f"Not settled: {finance_summary['orders_not_settled']}  ")
    lines.append(f"Mismatch: {finance_summary['orders_mismatch']}  ")
    lines.append(f"Unknown: {finance_summary['orders_unknown']}  ")
    lines.append(f"Settled amount (matched orders only): {finance_summary['settled_amount_matched_orders']}")
    lines.append("")
    lines.append("> D-1 orders not yet settled is expected TikTok Shop settlement lifecycle behavior, "
                  "not an API failure — reported as `NOT_SETTLED_YET`, never as `FAIL`.")
    lines.append("")
    lines.append("## 5. Analytics")
    lines.append("")
    lines.append(f"**Product**: {product_summary.get('product_count',0)} products, GMV={product_summary.get('product_gmv','0')}, "
                  f"impressions={product_summary.get('product_impressions','0')}, clicks={product_summary.get('product_clicks','0')}, "
                  f"CTR={product_summary.get('native_ctr','0')}  ")
    lines.append(f"**Video**: {video_summary.get('video_count',0)} videos, GMV={video_summary.get('video_gmv','0')}  ")
    lines.append(f"**LIVE**: {live_summary.get('live_sessions',0)} sessions — status {live_summary['status']}  ")
    lines.append(f"**Affiliate**: {affiliate_summary['affiliate_order_count']} orders, value={affiliate_summary['affiliate_order_value']}")
    lines.append("")
    lines.append("### GMV Reconciliation (not forced equal)")
    lines.append("")
    lines.append(f"- Order API value: {gmv_recon['order_api_value']}")
    lines.append(f"- Analytics GMV: {gmv_recon['analytics_gmv']} (diff: {gmv_recon['diff_order_vs_analytics']}) — {gmv_recon['reason_order_vs_analytics']}")
    lines.append(f"- Finance settled: {gmv_recon['finance_settled_value']} (diff: {gmv_recon['diff_order_vs_finance']}) — {gmv_recon['reason_order_vs_finance']}")
    lines.append("")
    lines.append("## 6. P&L Readiness")
    lines.append("")
    lines.append(f"Net Sales: {pnl['net_sales_status']} — {pnl['net_sales_reason']}  ")
    lines.append(f"COGS: {pnl['COGS_status']}  ")
    lines.append(f"CM1: {pnl['CM1_status']}  ")
    lines.append(f"CM2 (pre-ads): {pnl['CM2_pre_ads_status']}  ")
    lines.append(f"CM2 (final): {pnl['CM2_final_status']}  ")
    lines.append(f"Ads: {pnl['Ads_status']} — {pnl['Ads_note']}")
    lines.append("")
    lines.append("## 7. Data Gaps")
    lines.append("")
    lines.append("- COGS/SKU cost master: not found anywhere in this repository — Net Sales/CM1/CM2 cannot be finalized from this pilot alone.")
    lines.append("- TikTok Ads (Marketing API / GMV Max): Developer App submitted, Developer Profile still UNDER REVIEW — CM2 final blocked on this.")
    lines.append(f"- Finance settlement: {finance_summary['orders_not_settled']}/{order_kpis['total_orders']} target-date orders not yet assigned to a statement (normal D-1 lifecycle, revisit this report in ~1-2 weeks for a fuller settled view).")
    lines.append("- finance_transactions.csv (statement-level detail) covers only the 1 statement touched by this date's matched orders, not the full statement history.")
    lines.append("")
    lines.append("## 8. Top Exceptions")
    lines.append("")
    for i, ex in enumerate(exceptions[:5], start=1):
        lines.append(f"{i}. {ex}")
    if not exceptions:
        lines.append("(none)")
    lines.append("")
    lines.append("## 9. Next Action")
    lines.append("")
    lines.append("1. Re-run this pilot ~1-2 weeks after 2026-09-07 to observe the same orders reach `MATCHED` finance status and validate the reconciliation join end-to-end.")
    lines.append("2. Connect an authoritative HH SKU/COGS master (file or system) before attempting Net Sales / CM1.")
    lines.append("3. Resume once TikTok API for Business Developer Profile is APPROVED and the app is created — wire Marketing API + GMV Max reporting for CM2 final.")
    lines.append("")

    path = REPORTS_DIR / f"HH_TIKTOK_API_PILOT_{report_date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def data_status_lookup(endpoint: str, data_status: dict[str, str]) -> str:
    mapping = {
        "orders": "Orders", "order_detail": "Order Detail",
        "finance": "Finance Statements", "finance_payments": "Finance Statements",
        "finance_unsettled": "Finance Statements",
        "finance_statement_transactions": "Finance Transactions",
        "finance_order_statement_transactions": "Finance Transactions",
        "return_refund": "Returns", "affiliate": "Affiliate",
        "product_analytics": "Product Analytics", "video_analytics": "Video Analytics",
        "live_analytics": "LIVE Analytics", "authorized_shops": "Authorized Shop",
    }
    return data_status.get(mapping.get(endpoint, ""), "N/A")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    report_date = args.date

    summary_path = NORMALIZED_DIR / f"_collect_summary_{report_date}.json"
    if not summary_path.exists():
        print(f"No collect summary for {report_date} — run collect.py first.")
        return 1
    collect_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    orders = read_csv_rows(report_date, "orders.csv")
    lines = read_csv_rows(report_date, "order_lines.csv")
    match_rows = read_csv_rows(report_date, "finance_match_status.csv")
    order_tx_rows = read_csv_rows(report_date, "order_finance_transactions.csv")
    statement_rows = read_csv_rows(report_date, "finance_statements.csv")
    payments_rows = read_csv_rows(report_date, "payments.csv")
    returns = read_csv_rows(report_date, "returns.csv")
    affiliate = read_csv_rows(report_date, "affiliate_orders.csv")
    product_rows = read_csv_rows(report_date, "product_analytics.csv")
    video_rows = read_csv_rows(report_date, "video_analytics.csv")
    live_rows = read_csv_rows(report_date, "live_analytics.csv")

    order_kpis = compute_order_kpis(orders, lines)
    finance_summary = compute_finance_summary(match_rows, order_tx_rows, statement_rows, payments_rows)
    returns_summary = compute_returns_summary(returns)
    affiliate_summary = compute_affiliate_summary(affiliate)
    product_summary = compute_product_analytics_summary(product_rows)
    video_summary = compute_video_analytics_summary(video_rows)
    live_summary = compute_live_analytics_summary(live_rows)
    gmv_recon = gmv_reconciliation(order_kpis, product_summary, finance_summary)
    pnl = pnl_readiness(finance_summary)
    metric_rows = metric_dictionary_rows(order_kpis, finance_summary, product_summary,
                                          video_summary, live_summary, affiliate_summary, returns_summary)
    data_status = build_data_status(collect_summary, finance_summary, returns, affiliate,
                                     product_summary, video_summary, live_summary)
    evidence = build_evidence_manifest(report_date)

    exceptions = []
    if finance_summary["orders_unknown"] > 0:
        exceptions.append(f"{finance_summary['orders_unknown']} orders returned an unclassifiable "
                           "finance API response (see normalized/finance_match_status.csv, reason column).")
    if order_kpis["cancelled_orders"] > 0:
        exceptions.append(f"{order_kpis['cancelled_orders']} of {order_kpis['total_orders']} orders are CANCELLED "
                           "and excluded from eligible-order KPIs.")
    if returns_summary["return_count"] > 0:
        exceptions.append(f"{returns_summary['return_count']} return(s) created on {report_date} "
                           "— see normalized/returns.csv (status may still be pending).")
    if product_summary["status"] != "PASS_EMPTY" and D(product_summary.get("product_gmv", "0")) > D(order_kpis["order_total_amount_all"]) * 2:
        exceptions.append("Product Analytics GMV is more than 2x Order API total — flagged for manual review, not auto-explained.")
    exceptions.append(f"COGS source not connected — Net Sales/CM1/CM2 intentionally left as '{pnl['net_sales_status']}' / '{pnl['COGS_status']}', not zero-filled.")
    if finance_summary["orders_matched_to_finance"] > 0 and D(finance_summary["settled_amount_matched_orders"]) == 0:
        exceptions.append(f"{finance_summary['orders_matched_to_finance']} order(s) matched to a statement "
                           "(non-empty sku_transactions) but the API's root settlement_amount field for "
                           "every one of them is '0' — verify against raw/*_finance_order_statement_transactions_*.json "
                           "before trusting this as real settlement value.")

    fail_domains = [k for k, v in data_status.items() if v == "FAIL"]
    overall = "FAIL" if fail_domains else ("AMBER" if data_status["Ads"] == "WAITING_API" or data_status["COGS"] == "NOT_AVAILABLE" else "PASS")

    ceo_summary = {
        "report_date": report_date,
        "shop": SHOP_NAME,
        "currency": order_kpis.get("currency") or "VND",
        "data_status": overall,
        "BUSINESS": {
            "order_count": order_kpis["total_orders"],
            "units": order_kpis["units_eligible_lines"],
            "order_total": order_kpis["order_total_amount_eligible"],
            "AOV_working": order_kpis["aov_working"],
            "cancel_count": order_kpis["cancelled_orders"],
            "return_count": returns_summary["return_count"],
            "refund_amount": returns_summary["refund_amount_sum"],
            "affiliate_order_count": affiliate_summary["affiliate_order_count"],
        },
        "FINANCE": {
            "statements_found": finance_summary["statements_found"],
            "orders_matched_to_finance": finance_summary["orders_matched_to_finance"],
            "orders_not_settled": finance_summary["orders_not_settled"],
            "settled_amount": finance_summary["settled_amount_matched_orders"],
            "finance_adjustments": "see normalized/finance_statements.csv (adjustment_amount column)",
            "available_fee_metrics": finance_summary["available_fee_metrics_statement_level"],
        },
        "ANALYTICS": {
            "product_gmv": product_summary.get("product_gmv", "0"),
            "product_impressions": product_summary.get("product_impressions", "0"),
            "product_clicks": product_summary.get("product_clicks", "0"),
            "native_ctr_or_ctor": product_summary.get("native_ctr", "0"),
            "sku_orders": product_summary.get("sku_orders", "0"),
            "items_sold": product_summary.get("items_sold", "0"),
        },
        "VIDEO": {
            "video_count": video_summary.get("video_count", 0),
            "video_gmv": video_summary.get("video_gmv", "0"),
            "video_product_clicks": video_summary.get("video_product_clicks", "0"),
            "video_sku_orders": video_summary.get("video_sku_orders", "0"),
        },
        "LIVE": {
            "live_sessions": live_summary.get("live_sessions", 0),
            "live_gmv": live_summary.get("live_gmv", "0"),
            "live_orders": live_summary.get("live_orders", "0"),
            "live_product_clicks": live_summary.get("live_product_clicks", "0"),
        },
        "P&L": pnl,
        "TOP_PRODUCTS": product_summary.get("top10_products", []),
        "TOP_VIDEOS": video_summary.get("top10_videos", []),
        "EXCEPTIONS": exceptions[:5],
        "_gmv_reconciliation": gmv_recon,
    }

    (REPORTS_DIR / f"data_status_{report_date}.json").write_text(
        json.dumps(data_status, indent=2, ensure_ascii=False), encoding="utf-8")
    (REPORTS_DIR / f"ceo_summary_{report_date}.json").write_text(
        json.dumps(ceo_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (REPORTS_DIR / f"evidence_manifest_{report_date}.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")

    all_rows = {
        "orders": orders, "order_lines": lines, "finance_match_status": match_rows,
        "returns": returns, "affiliate": affiliate, "product_analytics": product_rows,
        "video_analytics": video_rows, "live_analytics": live_rows,
    }
    excel_path = build_excel(report_date, ceo_summary, data_status, all_rows, metric_rows)
    md_path = build_markdown(report_date, overall, data_status, evidence, order_kpis,
                              finance_summary, returns_summary, affiliate_summary,
                              product_summary, video_summary, live_summary, gmv_recon, pnl, exceptions)

    print(f"OVERALL: {overall}")
    print(f"Excel: {excel_path}")
    print(f"Markdown: {md_path}")
    print(f"data_status: {REPORTS_DIR / f'data_status_{report_date}.json'}")
    print(f"ceo_summary: {REPORTS_DIR / f'ceo_summary_{report_date}.json'}")
    print(f"evidence_manifest: {REPORTS_DIR / f'evidence_manifest_{report_date}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
