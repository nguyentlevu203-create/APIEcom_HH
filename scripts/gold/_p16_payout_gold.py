#!/usr/bin/env python3
"""P16 — Gold payout ("tiền về") per business_date x channel.

Reads core.fact_order (order business_date + status) LEFT JOIN
core.fact_settlement (one row per order: Shopee escrow / TikTok
statement or unsettled estimate) and applies the pure rules in
_p16_payout_logic.py. Writes 6 PNL_ENRICHMENT metrics (registered by
sql/067): payout, payout_actual, payout_estimated, payout_orders_actual,
payout_orders_estimated, payout_orders_missing.

Attribution is by the ORDER's business_date — never fact_settlement.
business_date, which for Shopee is the ingestion window date. Read-only
on core; CM1/CM2 untouched. Every run recomputes full history, so an
order whose estimate was replaced by a real settlement moves from
payout_estimated to payout_actual on the next Gold build.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402
from _p16_payout_logic import aggregate, classify_order  # noqa: E402

OWNER = "PNL_ENRICHMENT"
METRIC_STATUS = {
    "payout_actual": "ACTUAL",
    "payout_estimated": "PLATFORM_ESTIMATE",
    "payout_orders_actual": "READY",
    "payout_orders_estimated": "READY",
    "payout_orders_missing": "READY",
}

QUERY = """
SELECT fo.business_date, fo.channel, fo.shop_id, fo.order_status,
       fs.settlement_type, fs.settlement_amount, fs.est_settlement_amount
FROM core.fact_order fo
LEFT JOIN core.fact_settlement fs
  ON fs.channel = fo.channel AND fs.shop_id = fo.shop_id AND fs.order_id = fo.order_id
WHERE fo.channel IN ('SHOPEE', 'TIKTOK');
"""


def build_rows(order_rows):
    groups = defaultdict(list)
    for bd, channel, shop_id, order_status, s_type, s_amount, est_amount in order_rows:
        groups[(bd, channel, shop_id)].append(
            classify_order(channel, order_status, s_type, s_amount, est_amount))
    out = []
    for (bd, channel, shop_id), classified in sorted(groups.items()):
        agg = aggregate(classified)
        out.append((bd, channel, shop_id, "payout", agg["payout"], agg["payout_status"]))
        for metric, status in METRIC_STATUS.items():
            value = agg[metric]
            out.append((bd, channel, shop_id, metric, value, status if value is not None else "MISSING_SOURCE"))
    return out


def main():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(QUERY)
    order_rows = cur.fetchall()
    rows = build_rows(order_rows)
    written = guarded_upsert(conn, OWNER, rows)
    conn.close()
    days = len({(r[0], r[1]) for r in rows})
    print(f"P16 payout gold: orders={len(order_rows)} channel_days={days} rows_written={written}")


if __name__ == "__main__":
    main()
