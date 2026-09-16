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


def get_conn():
    url = keyring.get_password("HH_ECOM_NEON", "neondb_owner_database_url")
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

    # 3. Source coverage / freshness — flag API_ERROR as YELLOW (external,
    #    not RED — matches MONITORING_AND_ALERT_RULES_V1.md), SOURCE_LAGGING
    #    as YELLOW, anything else unexpected as RED.
    cur.execute(
        "SELECT platform, source_name, coverage_status, latest_db_date, last_error_message "
        "FROM mart.v_ai_source_coverage ORDER BY 1,2;"
    )
    for platform, source, status, latest_date, err in cur.fetchall():
        label = f"SOURCE[{platform}.{source}]"
        if status in ("CURRENT",):
            findings.append(("GREEN", label, f"{status} latest={latest_date}"))
        elif status in ("SOURCE_LAGGING", "NOT_SETTLED_YET", "API_ERROR", "NO_DATA", "NOT_SCHEDULED"):
            findings.append(("YELLOW", label, f"{status} latest={latest_date} err={err}"))
        else:
            findings.append(("RED", label, f"UNEXPECTED_STATUS={status}"))

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
