#!/usr/bin/env python3
"""P8.5 Section 3 — TikTok equivalent of _p6c1_shopee_pnl_extend.py.

Closes the same class of gap _p6c1_shopee_pnl_extend.py closed for
Shopee: _p6b3_finalize.py's TikTok CM1/actual-fee writer was hard-coded
to 2026-09-01..2026-09-11 (the P6B.3 validation window). Any later date
had a real base Gold row but no PNL enrichment -> gold row stays
MISSING_SOURCE forever unless this is re-run with a wider window.

FORMULA_CHANGE = NO. This file does not reimplement or alter any
formula — it reuses the exact same functions _p6b3_finalize.py itself
uses (build_cogs_by_day, tiktok_net_sales_from_db) and copies its
per-date fee/net_sales/gm1/cm1 arithmetic verbatim (see
_p6b3_finalize.py:99-140 for the original). The only functional change
is DATE_FROM/DATE_TO: hard-coded constants -> --date-from/--date-to CLI
args, defaulting to the live MIN/MAX(business_date) in
core.fact_order for TIKTOK (mirrors the Shopee extend script's
dynamic-range default exactly).

Source precedence unchanged: core.fact_settlement_sku_fee per-SKU
actual fees (SETTLEMENT_ACTUAL) are always used when present; the
legacy fixed-percentage rates from hh-lpm-etl remain diagnostic-only
(never substituted in) exactly as in the original script.

Idempotent (ON CONFLICT upsert via guarded_upsert, PNL_ENRICHMENT
owner only — cannot touch any BASE_GOLD-owned metric). Safe to run on
every orchestrator pass going forward, same as the Shopee extend
script.

Usage:
    python3 _p6b3_pnl_extend.py
    python3 _p6b3_pnl_extend.py --date-from 2026-09-12 --date-to 2026-09-14
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402
from _p6b_pnl_build import build_cogs_by_day, tiktok_net_sales_from_db  # noqa: E402
from _p6c1_cogs_status import downgrade_status, get_cogs_incomplete_dates  # noqa: E402


def D(x):
    return x if x is not None else Decimal(0)


def fetch_sku_fee_by_date(date_from: date, date_to: date) -> dict:
    """Identical query to _p6b3_finalize.py's fetch_sku_fee_by_date(),
    parameterized instead of reading module-level DATE_FROM/DATE_TO."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT business_date, count(*), count(DISTINCT order_id),
               sum(revenue_amount), sum(fixed_fee), sum(payment_fee), sum(vxp_fee),
               sum(infrastructure_fee), count(*) FILTER (WHERE infrastructure_fee != 0),
               sum(affiliate_fee), count(*) FILTER (WHERE affiliate_fee != 0)
        FROM core.fact_settlement_sku_fee
        WHERE channel='TIKTOK' AND business_date BETWEEN %s AND %s
        GROUP BY 1 ORDER BY 1;
        """,
        (date_from, date_to),
    )
    rows = cur.fetchall()
    conn.close()
    cols = ["lines", "orders", "revenue", "fixed_fee", "payment_fee", "vxp_fee", "infra_fee",
            "infra_lines_nonzero", "affiliate_fee", "affiliate_lines_nonzero"]
    return {r[0]: dict(zip(cols, r[1:])) for r in rows}


def default_date_range() -> tuple[date, date]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT min(business_date), max(business_date) FROM core.fact_order WHERE channel='TIKTOK';")
    d_from, d_to = cur.fetchone()
    conn.close()
    return d_from, d_to


def main(date_from: date, date_to: date):
    sku_fee = fetch_sku_fee_by_date(date_from, date_to)
    cogs = build_cogs_by_day()
    tt_net_sales = tiktok_net_sales_from_db(date_from, date_to)
    incomplete = get_cogs_incomplete_dates()

    pnl_rows = []
    per_day = []
    d = date_from
    while d <= date_to:
        sf = sku_fee.get(d)
        cogs_d = cogs.get(("TIKTOK", d), {"sellable": Decimal(0), "promo": Decimal(0), "packaging": Decimal(0)})
        ns = tt_net_sales.get(d, {})

        if not sf:
            row = {
                "business_date": d, "net_sales": ns.get("revenue"),
                "sellable_cogs": cogs_d["sellable"], "promo_gift_cost": cogs_d["promo"],
                "gm1": (ns["revenue"] - cogs_d["sellable"] - cogs_d["promo"]) if ns.get("revenue") is not None else None,
                "fixed_fee_actual": None, "payment_fee_actual": None, "vxp_fee_actual": None,
                "infrastructure_fee_actual": None, "affiliate_fee_actual": None,
                "hh_internal_packaging_cost": cogs_d["packaging"], "cm1": None,
                "cm1_status": "MISSING_SOURCE (no MATCHED sku-fee data for this date)",
            }
        else:
            fixed_fee = -D(sf["fixed_fee"])
            payment_fee = -D(sf["payment_fee"])
            vxp_fee = -D(sf["vxp_fee"])
            infra_fee = -D(sf["infra_fee"])
            affiliate_fee = -D(sf["affiliate_fee"])
            net_sales = ns.get("revenue")
            gm1 = (net_sales - cogs_d["sellable"] - cogs_d["promo"]) if net_sales is not None else None
            cm1 = None
            cm1_status = "MISSING_SOURCE"
            if gm1 is not None:
                cm1 = gm1 - fixed_fee - payment_fee - vxp_fee - infra_fee - cogs_d["packaging"]
                cm1_status = "READY"
            row = {
                "business_date": d, "net_sales": net_sales, "sellable_cogs": cogs_d["sellable"],
                "promo_gift_cost": cogs_d["promo"], "gm1": gm1, "fixed_fee_actual": fixed_fee,
                "payment_fee_actual": payment_fee, "vxp_fee_actual": vxp_fee,
                "infrastructure_fee_actual": infra_fee, "affiliate_fee_actual": affiliate_fee,
                "hh_internal_packaging_cost": cogs_d["packaging"], "cm1": cm1, "cm1_status": cm1_status,
            }

        per_day.append(row)

        for metric, val, status in (
            ("net_sales", row["net_sales"], "DERIVABLE" if row["net_sales"] is not None else "MISSING_SOURCE"),
            ("gm1", row["gm1"], "DERIVABLE" if row["gm1"] is not None else "MISSING_SOURCE"),
            ("cm1", row["cm1"], row["cm1_status"]),
            ("tiktok_fixed_fee_actual", row["fixed_fee_actual"], "READY" if row["fixed_fee_actual"] is not None else "MISSING_SOURCE"),
            ("tiktok_payment_fee_actual", row["payment_fee_actual"], "READY" if row["payment_fee_actual"] is not None else "MISSING_SOURCE"),
            ("tiktok_vxp_fee_actual", row["vxp_fee_actual"], "READY" if row["vxp_fee_actual"] is not None else "MISSING_SOURCE"),
            ("tiktok_infrastructure_fee_actual", row["infrastructure_fee_actual"], "READY" if row["infrastructure_fee_actual"] is not None else "MISSING_SOURCE"),
            ("tiktok_affiliate_fee_actual", row["affiliate_fee_actual"], "READY" if row["affiliate_fee_actual"] is not None else "MISSING_SOURCE"),
        ):
            # exact same downgrade rule as _p6b3_finalize.py:173 — COGS-incomplete
            # dates never report a falsely-confident READY/DERIVABLE status.
            status = downgrade_status("TIKTOK", d, metric, status, incomplete)
            pnl_rows.append((d, "TIKTOK", None, metric, val, status))
            if metric == "cm1":
                row["cm1"], row["cm1_status"] = val, status
        d = d + timedelta(days=1)

    # shop_id required by guarded_upsert row shape (business_date, channel, shop_id, metric, value, status)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT shop_id FROM core.fact_order WHERE channel='TIKTOK' LIMIT 1;")
    shop_id = cur.fetchone()[0]
    conn.close()
    pnl_rows = [(bd, ch, shop_id, m, v, s) for (bd, ch, _none, m, v, s) in pnl_rows]

    written = guarded_upsert(get_conn(), "PNL_ENRICHMENT", pnl_rows)
    print(f"tiktok_pnl_extend: dates={len(per_day)} rows_written={written} range={date_from}..{date_to}")
    for r in per_day:
        print(f"  {r['business_date']} net_sales={r['net_sales']} gm1={r['gm1']} cm1={r['cm1']} cm1_status={r['cm1_status']}")
    return per_day


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", type=str, default=None)
    ap.add_argument("--date-to", type=str, default=None)
    args = ap.parse_args()

    if args.date_from and args.date_to:
        d_from = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        d_to = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    else:
        d_from, d_to = default_date_range()

    main(d_from, d_to)
