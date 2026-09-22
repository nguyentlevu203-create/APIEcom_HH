#!/usr/bin/env python3
"""P6C4 Section 1-2, 7-8 — Gold affiliate metrics.

P8.5 Shopee Affiliate financial wiring (approved 2026-09-15): commission
is now computed with ORDER-LEVEL precedence, per order_id, not a
date-level either/or:

  1st priority: core.fact_settlement.order_ams_commission_fee for that
                 order_id, when non-zero. Basis=SETTLEMENT_ACTUAL.
  fallback:      core.fact_shopee_affiliate_conversion SUM(
                 item_brand_commission_to_affiliate + item_brand_commission_to_mcn)
                 for that order_id. Basis=ORDER_LEVEL_ACTUAL.
  NEVER summed together for the same order — settlement supersedes the
  provisional order-level value once it exists.

core.fact_shopee_affiliate_validation (the old FINAL_BILLED source) is
empty (0 rows) in current production data and is no longer used.

business_date is taken from the CONVERSION row (the order's own date),
never the settlement posting date — settlement can lag by >30 days
(937 order/date mismatches confirmed live), which would otherwise
misattribute affiliate expense to an unrelated later date.

Ownership-guarded (mart.gold_metric_ownership): volume/traffic metrics
and affiliate_commission_order_level/_settled are BASE_GOLD; the
canonical affiliate_commission (and cm2/cm2_known, written by
_p6c5_shopee_cm2.py) are PNL_ENRICHMENT."""
from __future__ import annotations

import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402


def main():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT shop_id FROM core.fact_order WHERE channel='SHOPEE' LIMIT 1;")
    shop_id = cur.fetchone()[0]

    cur.execute(
        "SELECT business_date, sales, items_sold, orders, clicks, roi, total_buyers, new_buyers "
        "FROM core.fact_shopee_affiliate_performance_daily WHERE grain_type='SHOP';"
    )
    shop_rows = {r[0]: r for r in cur.fetchall()}

    cur.execute(
        "SELECT business_date, count(DISTINCT affiliate_id) FROM core.fact_shopee_affiliate_performance_daily "
        "WHERE grain_type='AFFILIATE' AND affiliate_id IS NOT NULL GROUP BY 1;"
    )
    creator_counts = dict(cur.fetchall())

    # P8.5 order-level precedence commission computation. Grouped by
    # (order_id, business_date) — business_date is constant per order in
    # this table (it is the order's own date, not a settlement date).
    cur.execute(
        "SELECT order_id, business_date, "
        "sum(item_brand_commission_to_affiliate + item_brand_commission_to_mcn) "
        "FROM core.fact_shopee_affiliate_conversion GROUP BY order_id, business_date;"
    )
    conv_orders = cur.fetchall()

    cur.execute("SELECT order_id, order_ams_commission_fee FROM core.fact_settlement WHERE channel='SHOPEE';")
    settle_by_order = dict(cur.fetchall())

    conn.close()

    per_date = defaultdict(lambda: {
        "orders": 0, "settled": 0, "fallback": 0,
        "order_level_total": Decimal(0), "settled_total": Decimal(0), "canonical_total": Decimal(0),
    })
    for order_id, bd, order_level_commission in conv_orders:
        order_level_commission = order_level_commission or Decimal(0)
        settled_fee = settle_by_order.get(order_id)
        d = per_date[bd]
        d["orders"] += 1
        d["order_level_total"] += order_level_commission
        if settled_fee is not None and settled_fee != 0:
            d["settled"] += 1
            d["settled_total"] += settled_fee
            d["canonical_total"] += settled_fee
        else:
            d["fallback"] += 1
            d["canonical_total"] += order_level_commission

    all_dates = sorted(set(shop_rows) | set(per_date))
    rows = []
    commission_basis_log = {}

    for bd in all_dates:
        s = shop_rows.get(bd)
        if s is not None:
            _, sales, items_sold, orders, clicks, roi, total_buyers, new_buyers = s
            rows.append((bd, "SHOPEE", shop_id, "affiliate_gmv", sales, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_orders", orders, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_units", items_sold, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_clicks", clicks, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_roi", roi, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_new_buyers", new_buyers, "ESTIMATED_PERFORMANCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_total_buyers", total_buyers, "ESTIMATED_PERFORMANCE"))
        rows.append((bd, "SHOPEE", shop_id, "affiliate_creator_count", creator_counts.get(bd), "ESTIMATED_PERFORMANCE"))

        d = per_date.get(bd)
        if d is None or d["orders"] == 0:
            rows.append((bd, "SHOPEE", shop_id, "affiliate_commission_order_level", None, "MISSING_SOURCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_commission_settled", None, "MISSING_SOURCE"))
            rows.append((bd, "SHOPEE", shop_id, "affiliate_commission", None, "MISSING_SOURCE"))
            commission_basis_log[bd] = "MISSING_SOURCE"
            continue

        if d["settled"] == d["orders"]:
            basis = "SETTLEMENT_ACTUAL"
        elif d["settled"] > 0:
            basis = "MIXED_SETTLEMENT_AND_ORDER_LEVEL_ACTUAL"
        else:
            basis = "ORDER_LEVEL_ACTUAL"

        rows.append((bd, "SHOPEE", shop_id, "affiliate_commission_order_level", d["order_level_total"], "DERIVABLE"))
        rows.append((bd, "SHOPEE", shop_id, "affiliate_commission_settled", d["settled_total"], "DERIVABLE"))
        rows.append((bd, "SHOPEE", shop_id, "affiliate_commission", d["canonical_total"], basis))
        commission_basis_log[bd] = basis

    base_gold_rows = [r for r in rows if r[3] != "affiliate_commission"]
    pnl_rows = [r for r in rows if r[3] == "affiliate_commission"]
    written = guarded_upsert(get_conn(), "BASE_GOLD", base_gold_rows)
    written2 = guarded_upsert(get_conn(), "PNL_ENRICHMENT", pnl_rows)
    print(f"BASE_GOLD rows written={written}, PNL_ENRICHMENT rows written={written2}")
    print(f"total_orders={sum(d['orders'] for d in per_date.values())} "
          f"settled={sum(d['settled'] for d in per_date.values())} "
          f"fallback={sum(d['fallback'] for d in per_date.values())}")
    for bd, basis in sorted(commission_basis_log.items()):
        print(f"  {bd}: commission_basis={basis}")


if __name__ == "__main__":
    main()
