#!/usr/bin/env python3
"""
P2D — backfill derived ctr/roas/conversion on the 2 existing Shopee
fact_ads_daily rows (SHOP_TOTAL_DIRECT / SHOP_TOTAL_BROAD, loaded by
P2A). Does NOT touch p2a_shopee_to_postgres.py or re-call the Shopee
Ads API — purely a derived-math UPDATE over already-committed,
already-reconciled columns (impressions, clicks, spend, gmv), via
hh_etl_writer (DML only, no schema change).

ctr = clicks / impressions
roas = gmv / spend

`conversion` (BIGINT, a distinct conversion-EVENT count) is deliberately
NOT backfilled here: Shopee's Ads API
(get_product_campaign_daily_performance) exposes 'cr'/'direct_cr' as a
RATE, not a separate conversion count, and that rate is already fully
represented by the existing 'orders' column combined with 'clicks' — a
report can compute orders/clicks itself. Writing a redundant duplicate
of 'orders' into 'conversion', or force-casting a rate into a BIGINT
column, would misrepresent the source. Left NULL until a source exposes
a genuinely distinct conversion-event count.
"""
from __future__ import annotations

import keyring
import psycopg2


def get_db_conn():
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    conn = psycopg2.connect(url)
    del url
    return conn


def main() -> None:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE core.fact_ads_daily
        SET
            ctr = CASE WHEN impressions > 0 THEN ROUND(clicks::numeric / impressions, 4) ELSE NULL END,
            roas = CASE WHEN spend > 0 THEN ROUND(gmv / spend, 4) ELSE NULL END
        WHERE channel = 'SHOPEE' AND campaign_id IN ('SHOP_TOTAL_DIRECT', 'SHOP_TOTAL_BROAD')
        RETURNING channel, campaign_id, ctr, roas, conversion;
        """
    )
    for row in cur.fetchall():
        print(row)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
