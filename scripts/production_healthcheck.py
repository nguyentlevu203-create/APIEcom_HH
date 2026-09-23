#!/usr/bin/env python3
"""P9.0 — Read-only production health check for HH Ecom Data + AI.

Checks, in order: source coverage/freshness, Gold exceptions, duplicate
Gold keys, latest CORE/GOLD/MART dates, CM2 formula mismatches, Shopee
Ads reconciliation, Shopee Affiliate reconciliation, MCP availability
and tool count. Prints a concise GREEN/YELLOW/RED verdict with exact
reasons.

READ ONLY — this script never writes to the database, never retries a
failed source, never redeploys anything. It only reports state.

Usage: python3 scripts/production_healthcheck.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal

import keyring
import psycopg2

MCP_URL = "https://hh-ecom-reporting-mcp.nguyenthienlevu.workers.dev/mcp"
EXPECTED_TOOLS = {
    "get_ecom_overview", "get_cost_breakdown", "get_operations",
    "get_video_performance", "get_live_performance",
    "get_affiliate_performance", "get_data_coverage",
}

# P11-HEAL — required/optional source map for STALE (and true absence)
# classification. Explicit by BUSINESS ROLE, never inferred from row
# counts: "required" = feeds core CM1/CM2/GM1/net_sales P&L computation
# directly (orders, returns, settlement/finance, ad spend, affiliate
# commission) — a required source going STALE means the numbers CEO
# reporting / AI queries see are built on stale inputs, so it's RED.
# "Optional" = inventory snapshots and TikTok engagement analytics
# (product_analytics/live/shop_traffic) — useful reporting, not a P&L
# input; STALE there is degraded but not RED. Every domain
# mart.v_ai_source_coverage currently tracks (sql/049) must appear in
# exactly one of these two sets — see test_healthcheck_stale_semantics.py.
REQUIRED_SOURCES = {
    ("SHOPEE", "orders"), ("SHOPEE", "returns"), ("SHOPEE", "finance"),
    ("SHOPEE", "ads"), ("SHOPEE", "affiliate_ams"),
    ("TIKTOK", "orders"), ("TIKTOK", "returns"), ("TIKTOK", "finance"), ("TIKTOK", "affiliate"),
}
OPTIONAL_SOURCES = {
    ("SHOPEE", "product_inventory"), ("TIKTOK", "product_inventory"),
    ("TIKTOK", "product_analytics"), ("TIKTOK", "live"), ("TIKTOK", "shop_traffic"),
}
# Statuses mart.v_ai_source_coverage (sql/049) can report that are
# already policed by known, non-required-vs-optional policy — unchanged
# by this fix. NOT_SETTLED_YET is kept for forward-compat with sources
# that may report it; not currently emitted by sql/049 itself.
KNOWN_YELLOW_STATUSES = ("SOURCE_LAGGING", "NOT_SETTLED_YET", "API_ERROR", "NO_DATA", "NOT_SCHEDULED")


def _classify_source_status(platform: str, source: str, status: str) -> str:
    """The finding level for one mart.v_ai_source_coverage row. CURRENT
    is always GREEN. The known lag/external-error statuses stay YELLOW
    exactly as before. STALE escalates to RED only for a REQUIRED
    source — for an OPTIONAL source it's YELLOW (degraded, not a P&L
    correctness problem). Any other, unrecognized status is still RED
    — fail-closed, unchanged from before this fix."""
    if status == "CURRENT":
        return "GREEN"
    if status in KNOWN_YELLOW_STATUSES:
        return "YELLOW"
    if status == "STALE":
        return "RED" if (platform, source) in REQUIRED_SOURCES else "YELLOW"
    return "RED"


def get_conn():
    # Phase 6C scheduler-migration — env var (GitHub Secret) takes
    # precedence over Keychain; unset on the Mac local/production path.
    url = os.environ.get("HH_NEONDB_OWNER_DATABASE_URL") or keyring.get_password("HH_ECOM_NEON", "neondb_owner_database_url")
    conn = psycopg2.connect(url)
    del url
    conn.autocommit = True
    return conn


def check_database(findings: list[tuple[str, str, str]]):
    conn = get_conn()
    cur = conn.cursor()

    # 1. Duplicate Gold keys
    cur.execute(
        "SELECT count(*) FROM (SELECT business_date,channel,shop_id,metric_name,count(*) c "
        "FROM mart.gold_channel_daily GROUP BY 1,2,3,4 HAVING count(*)>1) x;"
    )
    dup = cur.fetchone()[0]
    findings.append(("RED" if dup else "GREEN", "DUPLICATE_GOLD_KEYS", f"{dup}"))

    # 2. Latest CORE / GOLD / MART dates
    cur.execute("SELECT max(business_date) FROM core.fact_order;")
    core_latest = cur.fetchone()[0]
    cur.execute("SELECT max(business_date) FROM mart.gold_channel_daily;")
    gold_latest = cur.fetchone()[0]
    cur.execute("SELECT max(business_date) FROM mart.v_ceo_ecom_daily WHERE net_sales IS NOT NULL;")
    mart_latest = cur.fetchone()[0]
    findings.append(("GREEN", "LATEST_CORE_DATE", str(core_latest)))
    findings.append(("GREEN", "LATEST_GOLD_DATE", str(gold_latest)))
    findings.append(("GREEN", "LATEST_MART_DATE_WITH_NET_SALES", str(mart_latest)))

    # 3. Source coverage / freshness — see _classify_source_status():
    #    CURRENT is GREEN, known lag/external-error statuses stay
    #    YELLOW, STALE is RED only for a REQUIRED (P&L-critical) source
    #    and YELLOW for an OPTIONAL one, anything else unrecognized is
    #    still RED (fail-closed).
    cur.execute(
        "SELECT platform, source_name, coverage_status, latest_db_date, last_error_message "
        "FROM mart.v_ai_source_coverage ORDER BY 1,2;"
    )
    rows = cur.fetchall()
    seen_sources = set()
    for platform, source, status, latest_date, err in rows:
        seen_sources.add((platform, source))
        label = f"SOURCE[{platform}.{source}]"
        level = _classify_source_status(platform, source, status)
        detail = f"{status} latest={latest_date}" if level == "GREEN" else f"{status} latest={latest_date} err={err}"
        if level == "RED" and status not in KNOWN_YELLOW_STATUSES and status != "STALE":
            detail = f"UNEXPECTED_STATUS={status}"
        findings.append((level, label, detail))

    # A REQUIRED source missing from the view entirely (config drift,
    # not a data-freshness problem) is a structural RED — never
    # silently converted to zero/absent.
    for platform, source in sorted(REQUIRED_SOURCES - seen_sources):
        findings.append(("RED", f"SOURCE[{platform}.{source}]", "MISSING_REQUIRED_SOURCE: not present in mart.v_ai_source_coverage"))

    # 4. Gold exceptions (COGS) — 0 expected per last _p6a_gold_build.py run
    cur.execute(
        "SELECT count(*) FROM core.fact_order_item foi "
        "LEFT JOIN core.map_platform_product mp ON mp.channel=foi.channel AND "
        "(mp.platform_identifier=foi.sku OR mp.item_id=foi.platform_product_id) "
        "WHERE mp.hh_sku IS NULL AND mp.bom_pattern_id IS NULL AND foi.sku IS NOT NULL;"
    )
    unresolved = cur.fetchone()[0]
    findings.append(("YELLOW" if unresolved else "GREEN", "COGS_UNRESOLVED_LINES", f"{unresolved}"))

    # 5. Shopee Ads reconciliation — MAX(impressions) across BROAD/DIRECT
    #    pair must be bit-for-bit identical per date (the single-count
    #    invariant this whole fix depends on).
    cur.execute(
        "SELECT business_date, count(DISTINCT impressions), count(DISTINCT clicks), count(DISTINCT spend) "
        "FROM core.fact_ads_daily WHERE channel='SHOPEE' GROUP BY 1 "
        "HAVING count(DISTINCT impressions)>1 OR count(DISTINCT clicks)>1 OR count(DISTINCT spend)>1;"
    )
    ads_mismatches = cur.fetchall()
    findings.append(("RED" if ads_mismatches else "GREEN", "SHOPEE_ADS_BROAD_DIRECT_INVARIANT",
                      f"{len(ads_mismatches)} date(s) with divergent shared fields" if ads_mismatches else "0 mismatches"))

    # 6. Shopee Affiliate reconciliation — order-identity match rate
    cur.execute("SELECT count(DISTINCT order_id) FROM core.fact_shopee_affiliate_conversion;")
    conv_orders = cur.fetchone()[0]
    cur.execute(
        "SELECT count(DISTINCT c.order_id) FROM core.fact_shopee_affiliate_conversion c "
        "JOIN core.fact_settlement s ON s.channel='SHOPEE' AND s.order_id=c.order_id;"
    )
    matched_orders = cur.fetchone()[0]
    unmatched = conv_orders - matched_orders
    findings.append(("YELLOW" if unmatched else "GREEN", "SHOPEE_AFFILIATE_UNMATCHED_ORDERS", f"{unmatched} of {conv_orders}"))

    # 7. CM2 formula mismatches (both channels)
    cur.execute(
        "SELECT count(*) FROM mart.v_ceo_ecom_daily WHERE channel='SHOPEE' "
        "AND cm1 IS NOT NULL AND ads_spend IS NOT NULL AND affiliate_commission IS NOT NULL "
        "AND cm2_known IS NOT NULL AND abs(cm2_known - (cm1 - ads_spend - affiliate_commission)) > 0.01;"
    )
    shopee_cm2_mismatch = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM mart.v_ceo_ecom_daily WHERE channel='TIKTOK' "
        "AND cm1 IS NOT NULL AND affiliate_fee IS NOT NULL AND cm2_known IS NOT NULL "
        "AND abs(cm2_known - (cm1 - affiliate_fee)) > 0.01;"
    )
    tiktok_cm2_mismatch = cur.fetchone()[0]
    findings.append(("RED" if shopee_cm2_mismatch else "GREEN", "SHOPEE_CM2_FORMULA_MISMATCHES", f"{shopee_cm2_mismatch}"))
    findings.append(("RED" if tiktok_cm2_mismatch else "GREEN", "TIKTOK_CM2_FORMULA_MISMATCHES", f"{tiktok_cm2_mismatch}"))

    # 8. TikTok product funnel CORE->MART diff
    cur.execute("SELECT sum(impressions), sum(clicks) FROM core.fact_product_analytics_daily;")
    core_pf = cur.fetchone()
    cur.execute("SELECT sum(impressions), sum(clicks) FROM mart.v_ai_product_traffic_daily;")
    mart_pf = cur.fetchone()
    pf_diff = core_pf != mart_pf
    findings.append(("RED" if pf_diff else "GREEN", "PRODUCT_FUNNEL_CORE_TO_MART_DIFF", "MISMATCH" if pf_diff else "0"))

    conn.close()


def check_mcp(findings: list[tuple[str, str, str]]):
    token = None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "HH_ECOM_MCP", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            token = out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass

    if not token:
        findings.append(("YELLOW", "MCP_AUTH", "token not found in local Keychain — MCP live check skipped"))
        return

    def call(tool, args):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}})
        proc = subprocess.run(
            ["curl", "-s", "-X", "POST", MCP_URL,
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "-H", "Accept: application/json, text/event-stream",
             "-d", payload],
            capture_output=True, text=True, timeout=20,
        )
        body = proc.stdout
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:])
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                return json.loads(payload["result"]["content"][0]["text"])
        raise RuntimeError("no data: line in response")

    ok_count = 0
    for tool, args in [
        ("get_ecom_overview", {"from_date": "2026-09-10", "to_date": "2026-09-10"}),
        ("get_cost_breakdown", {"from_date": "2026-09-10", "to_date": "2026-09-10"}),
        ("get_operations", {"platform": "SHOPEE"}),
        ("get_video_performance", {"from_date": "2026-09-05", "to_date": "2026-09-05", "platform": "TIKTOK", "limit": 1}),
        ("get_live_performance", {"from_date": "2026-09-05", "to_date": "2026-09-05", "account_type": "ALL", "limit": 1}),
        ("get_affiliate_performance", {"from_date": "2026-09-05", "to_date": "2026-09-05", "platform": "SHOPEE", "limit": 1}),
        ("get_data_coverage", {}),
    ]:
        try:
            call(tool, args)
            ok_count += 1
        except Exception as e:  # noqa: BLE001
            findings.append(("RED", f"MCP_TOOL[{tool}]", f"FAILED: {e}"))

    findings.append(("GREEN" if ok_count == 7 else "RED", "MCP_TOOL_COUNT_PASS", f"{ok_count}/7"))


def main():
    findings: list[tuple[str, str, str]] = []
    try:
        check_database(findings)
    except Exception as e:  # noqa: BLE001
        findings.append(("RED", "DATABASE_CHECK", f"EXCEPTION: {e}"))
    try:
        check_mcp(findings)
    except Exception as e:  # noqa: BLE001
        findings.append(("RED", "MCP_CHECK", f"EXCEPTION: {e}"))

    order = {"RED": 0, "YELLOW": 1, "GREEN": 2}
    findings.sort(key=lambda f: order[f[0]])

    print("=" * 70)
    print("HH ECOM PRODUCTION HEALTH CHECK (read-only)")
    print("=" * 70)
    for level, name, detail in findings:
        print(f"[{level:6}] {name}: {detail}")

    overall = "GREEN"
    if any(f[0] == "RED" for f in findings):
        overall = "RED"
    elif any(f[0] == "YELLOW" for f in findings):
        overall = "YELLOW"
    print("=" * 70)
    print(f"OVERALL: {overall}")
    print("=" * 70)
    sys.exit(0 if overall != "RED" else 1)


if __name__ == "__main__":
    main()
