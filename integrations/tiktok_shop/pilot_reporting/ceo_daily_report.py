#!/usr/bin/env python3
"""
CEO Daily Report (pipeline step 16) — Section 8 of the follow-up spec.

Reads business_mart.build() output + readiness flags. No raw technical
stats (statement counts, page counts, request IDs) — those stay in
DATA STATUS / evidence, per the explicit rule.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pilot_common import REPORTS_DIR


def build_markdown(report_date: str, mart: dict[str, Any], readiness: dict[str, Any]) -> Path:
    b, f, e, g = mart["BUSINESS"], mart["FUNNEL"], mart["ECONOMICS"], mart["GROWTH"]
    lines = [
        f"# TikTok CEO Daily Report — {report_date}",
        "",
        "## DATA STATUS",
        "",
        f"- Commercial Reporting Readiness: **{readiness['commercial_reporting_readiness']}**",
        f"- Finance Settlement Readiness: **{readiness['finance_settlement_readiness']}**",
        f"- P&L Readiness: **{readiness['pnl_readiness']}**",
        "",
        "## BUSINESS",
        "",
        f"- Orders: {b['orders']} ({b['eligible_orders']} eligible)",
        f"- Units: {b['units']}",
        f"- GMV: {b['gmv']} VND",
        f"- HH Net Sales Candidate: {b['hh_net_sales_candidate']} VND "
        f"({b['hh_net_sales_candidate_coverage_pct']}% of eligible orders settled; "
        f"business_signoff={b['hh_net_sales_candidate_metadata']['business_signoff']})",
        f"- AOV: {b['aov']} VND",
        f"- Cancel: {b['cancel_count']}",
        f"- Return: {b['return_count']}",
        "",
        "## FUNNEL",
        "",
        f"- Impressions: {f['impressions']}",
        f"- Product Clicks: {f['product_clicks']}",
        f"- CTR: {f['ctr']}",
        f"- CVR/CTOR: {f['cvr_ctor']}",
        f"- Orders (analytics-attributed): {f['orders_from_analytics']}",
        "",
        "## ECONOMICS",
        "",
        f"- COGS coverage: {e['cogs_coverage_value_pct']}% value / {e['cogs_coverage_unit_pct']}% units "
        f"(threshold {e['cogs_coverage_threshold_pct']}% — {e['cogs_coverage_threshold_note']})",
        f"- GM Working: {e['gross_margin_working'] if e['gross_margin_working'] else e['gross_margin_status']}",
        f"- Fees: {e['fees']} VND",
        f"- CM1 Working: {e['cm1_working'] if e['cm1_working'] else e['cm1_status']}",
        f"- Ads: {e['ads_status']}",
        f"- CM2 Final: {e['cm2_final_status']}",
        "",
        "## GROWTH",
        "",
        f"- Affiliate eligible orders: {g['affiliate_eligible_orders']}",
        f"- Video: {g['video_count']} videos, GMV {g['video_gmv']} VND",
        f"- LIVE: {g['live_sessions']} sessions, GMV {g['live_gmv']} VND",
        "",
        "## HERO SKU (top 10 by native Analytics GMV)",
        "",
        "| Product ID | GMV | Impressions | Clicks | CVR | Stock | Returns |",
        "|---|---|---|---|---|---|---|",
    ]
    for h in mart["HERO_SKU"]:
        lines.append(f"| {h['product_id']} | {h['gmv']} | {h['impressions']} | {h['clicks']} | "
                     f"{h['cvr']} | {h['stock_qty'] if h['stock_qty'] is not None else 'N/A'} | "
                     f"{h['return_count']} |")
    lines.append("")
    lines.append("## EXCEPTIONS")
    lines.append("")
    for i, ex in enumerate(mart["EXCEPTIONS"], 1):
        lines.append(f"{i}. {ex}")
    if not mart["EXCEPTIONS"]:
        lines.append("(none)")
    lines.append("")
    lines.append("## ACTIONS")
    lines.append("")
    for i, ac in enumerate(mart["ACTIONS"], 1):
        lines.append(f"{i}. {ac}")
    lines.append("")

    path = REPORTS_DIR / f"CEO_DAILY_{report_date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_json(report_date: str, mart: dict[str, Any], readiness: dict[str, Any]) -> Path:
    out = {"report_date": report_date, "readiness": readiness, **{
        k: v for k, v in mart.items() if k != "report_date"
    }}
    path = REPORTS_DIR / f"ceo_daily_{report_date}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
