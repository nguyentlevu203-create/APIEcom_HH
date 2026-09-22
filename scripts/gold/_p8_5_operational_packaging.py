#!/usr/bin/env python3
"""P8.5 Section 2 — hh_operational_packaging_cost (approved).

Formula: 2,000 VND x non-cancelled unit quantity, per channel/business_date.
Source: core.fact_order + core.fact_order_item (both real, no estimation).
Matches the legacy formula exactly (scripts/send_staff_scorecard_emails.py
:233,278 in hh-lpm-etl, corroborated by build_ceo_daily_pnl_package_v4_1.py
and v4_5.py -- 3 independent sources, all agree, per-UNIT not per-order).

BASE_GOLD-owned (registered in mart.gold_metric_ownership this pass) --
computed straight from CORE, no PNL_ENRICHMENT dependency, so it covers
every date CORE has, including historical ones with no PNL run yet.

Economically distinct from, and additive to, the existing 'packaging_cost'
metric (physical packaging-item COGS) -- this script writes ONLY
hh_operational_packaging_cost, never touches packaging_cost.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402

RATE = 2000


def main():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT fo.business_date, fo.channel, fo.shop_id,
               SUM(foi.qty) FILTER (WHERE fo.order_status <> 'CANCELLED') AS non_cancelled_units
        FROM core.fact_order fo
        JOIN core.fact_order_item foi
          ON foi.channel = fo.channel AND foi.shop_id = fo.shop_id AND foi.order_id = fo.order_id
        GROUP BY fo.business_date, fo.channel, fo.shop_id;
    """)
    rows = cur.fetchall()
    conn.close()

    gold_rows = []
    for business_date, channel, shop_id, units in rows:
        units = units or 0
        cost = units * RATE
        status = "DERIVABLE"  # real units x approved rate, both channels
        gold_rows.append((business_date, channel, shop_id, "hh_operational_packaging_cost", cost, status))

    written = guarded_upsert(get_conn(), "BASE_GOLD", gold_rows)
    print(f"hh_operational_packaging_cost: rows_written={written} dates={len(rows)}")

    by_channel = {}
    for business_date, channel, shop_id, units in rows:
        by_channel.setdefault(channel, 0)
        by_channel[channel] += (units or 0) * RATE
    for ch, total in by_channel.items():
        print(f"  {ch}: total = {total:,} VND")
    return by_channel


if __name__ == "__main__":
    main()
