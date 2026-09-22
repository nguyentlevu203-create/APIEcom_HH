#!/usr/bin/env python3
"""P6C.1 — close a genuine pre-existing gap surfaced by the new
mart.v_gold_build_freshness view: P6B.1's Shopee net_sales/gm1/cm1
writer only ever covered 2026-09-01..09-09 (the validated-workbook
calibration window). Any later date (09-10, 09-11, and onward) had a
real base Gold row but no PNL enrichment at all -> PNL_STALE_VS_BASE.

This is NOT a redesign: reuses the exact same shopee_net_sales_from_db()
/ CM1 formula already proven in P6B.1, just over the full current
business_date range instead of the fixed validation window. Idempotent,
ownership-guarded (PNL_ENRICHMENT), safe to run on every orchestrator
pass going forward.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402
from _p6b1_finalize import shopee_net_sales_from_db  # noqa: E402
from _p6b_pnl_build import build_cogs_by_day  # noqa: E402
from _p6c1_cogs_status import downgrade_status, get_cogs_incomplete_dates  # noqa: E402


def main():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT min(business_date), max(business_date) FROM core.fact_order WHERE channel='SHOPEE';")
    date_from, date_to = cur.fetchone()
    cur.execute("SELECT shop_id FROM core.fact_order WHERE channel='SHOPEE' LIMIT 1;")
    shop_id = cur.fetchone()[0]
    conn.close()

    shopee_db = shopee_net_sales_from_db(date_from, date_to)
    cogs = build_cogs_by_day()
    incomplete = get_cogs_incomplete_dates()

    rows = []
    for d, cur_v in shopee_db.items():
        cogs_d = cogs.get(("SHOPEE", d))
        net_sales = cur_v.get("net_sales")
        gm1 = cm1 = None
        if net_sales is not None and cogs_d is not None:
            gm1 = net_sales - cogs_d["sellable"] - cogs_d["promo"]
            cm1 = gm1 - (cur_v["commission_fee"] or Decimal(0)) - (cur_v["service_fee"] or Decimal(0)) \
                - (cur_v["payment_fee"] or Decimal(0)) - cogs_d["packaging"] - (cur_v["shipping_net"] or Decimal(0)) \
                - (cur_v["refund"] or Decimal(0))

        for metric, val in (("net_sales", net_sales), ("gm1", gm1), ("cm1", cm1)):
            status = "READY" if metric != "cm1" and val is not None else ("DERIVABLE" if val is not None else "MISSING_SOURCE")
            status = downgrade_status("SHOPEE", d, metric, status, incomplete)
            rows.append((d, "SHOPEE", shop_id, metric, val, status))

    written = guarded_upsert(get_conn(), "PNL_ENRICHMENT", rows)
    print(f"shopee_pnl_extend: dates={len(shopee_db)} rows_written={written} range={date_from}..{date_to}")


if __name__ == "__main__":
    main()
