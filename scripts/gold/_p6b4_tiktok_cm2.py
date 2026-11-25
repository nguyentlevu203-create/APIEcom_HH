#!/usr/bin/env python3
"""P8.5 "FINAL FINISH" Section 4 — activate TikTok cm2_known/cm2
(approved 2026-09-15, does NOT wait for TikTok Ads Developer Profile).

    tiktok_cm2_known = cm1 - tiktok_affiliate_fee - tiktok_ads_spend

P15 — TikTok ads cost (GMV Max, TikTok API for Business) is subtracted
only on dates where Gold ads_spend is READY (collector coverage marker
present). On any other date the pre-P15 behaviour is kept exactly:
cm1 - affiliate_fee, status PARTIAL_MISSING_TIKTOK_ADS_AND_INTERNAL_
MARKETING — ads are never invented as 0. Booking/KOL/KOC and
Live/internal marketing are likewise never invented.

Reads cm1/cm1_status/affiliate_fee from mart.v_ceo_ecom_daily (the VIEW,
not the raw Gold table) specifically so this script automatically
inherits the exact same TikTok settlement-completeness gate and
operational-packaging adjustment already applied there, instead of
duplicating that logic and risking drift. Writes back to
mart.gold_channel_daily (PNL_ENRICHMENT-owned), channel=TIKTOK only —
never touches SHOPEE's cm2/cm2_known rows.

CM1 is read-only in this script — never written, never modified.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_recompute import get_conn  # noqa: E402


def main():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT shop_id FROM core.fact_order WHERE channel='TIKTOK' LIMIT 1;")
    shop_id = cur.fetchone()[0]

    cur.execute(
        "SELECT business_date, cm1, cm1_status, affiliate_fee, ads_spend, ads_spend_status "
        "FROM mart.v_ceo_ecom_daily WHERE channel='TIKTOK' ORDER BY business_date;"
    )
    data = cur.fetchall()
    conn.close()

    rows = []
    for bd, cm1, cm1_status, affiliate_fee, ads_spend, ads_status in data:
        if cm1 is None:
            # Either genuinely MISSING_SOURCE or withheld by the
            # settlement-completeness guard (SOURCE_LAGGING /
            # NOT_SETTLED_YET / NO_ELIGIBLE_ORDERS) — preserve whichever
            # status cm1 itself carries, never force a value.
            status = cm1_status or "MISSING_SOURCE"
            rows.append((bd, "TIKTOK", shop_id, "cm2_known", None, status))
            rows.append((bd, "TIKTOK", shop_id, "cm2", None, status))
            continue
        if affiliate_fee is None:
            rows.append((bd, "TIKTOK", shop_id, "cm2_known", None, "MISSING_SOURCE"))
            rows.append((bd, "TIKTOK", shop_id, "cm2", None, "MISSING_SOURCE"))
            continue
        if ads_status == "READY" and ads_spend is not None:
            known = cm1 - affiliate_fee - ads_spend
            cm2_status = "PARTIAL_MISSING_INTERNAL_MARKETING_SOURCE"
        else:
            known = cm1 - affiliate_fee
            cm2_status = "PARTIAL_MISSING_TIKTOK_ADS_AND_INTERNAL_MARKETING"
        rows.append((bd, "TIKTOK", shop_id, "cm2_known", known, "DERIVABLE"))
        rows.append((bd, "TIKTOK", shop_id, "cm2", known, cm2_status))

    written = guarded_upsert(get_conn(), "PNL_ENRICHMENT", rows)
    computable = sum(1 for r in data if r[1] is not None and r[3] is not None)
    with_ads = sum(1 for r in data if r[1] is not None and r[3] is not None and r[5] == "READY" and r[4] is not None)
    lagging = sum(1 for r in data if r[1] is None and r[2] and "SOURCE_LAGGING" in str(r[2]))
    print(f"tiktok_cm2: dates={len(data)} rows_written={written} computable={computable} "
          f"with_ads_subtracted={with_ads} source_lagging_preserved={lagging}")


if __name__ == "__main__":
    main()
