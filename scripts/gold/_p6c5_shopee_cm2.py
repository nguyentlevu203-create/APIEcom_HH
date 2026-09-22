#!/usr/bin/env python3
"""P8.5 Section 8-9 — activate the Shopee CM2 bridge (approved 2026-09-15).

Affiliate is a below-CM1 marketing cost. CM1 is NOT touched by this
script (read-only against it). For SHOPEE only (TikTok Ads/affiliate
wiring was not part of this approval):

    cm2_known = cm1 - ads_spend - affiliate_commission

Booking/KOL/KOC and Live/internal marketing costs have no approved
source for Shopee yet and are never invented/defaulted to 0 (per
instruction). So the canonical `cm2` metric carries the SAME numeric
value as cm2_known whenever it is computable (never hidden just because
another cost is still missing), but its status communicates the
incompleteness:

    - all 3 inputs present -> cm2 = cm2_known, status =
      PARTIAL_MISSING_INTERNAL_MARKETING_SOURCE (Booking/KOL/KOC and
      Live/internal marketing are always still missing for Shopee today
      - there is no case where this pipeline can currently mark a date
      COMPLETE; that status is reserved for when those sources exist).
    - any of cm1/ads_spend/affiliate_commission missing -> cm2 = NULL,
      status = MISSING_SOURCE (fail-closed, never a fabricated 0).

Ownership-guarded PNL_ENRICHMENT write (mart.gold_metric_ownership);
supersedes the old NULL/NOT_APPLICABLE placeholder cm2 rows for SHOPEE
via the normal ON CONFLICT DO UPDATE upsert path. TIKTOK cm2 rows are
untouched by this script.
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
    cur.execute("SELECT shop_id FROM core.fact_order WHERE channel='SHOPEE' LIMIT 1;")
    shop_id = cur.fetchone()[0]

    def load(metric):
        cur.execute(
            "SELECT business_date, metric_value FROM mart.gold_channel_daily "
            "WHERE channel='SHOPEE' AND metric_name=%s;",
            (metric,),
        )
        return dict(cur.fetchall())

    cm1_raw = load("cm1")
    op_packaging = load("hh_operational_packaging_cost")
    ads_spend = load("ads_spend")
    affiliate_commission = load("affiliate_commission")
    conn.close()

    # cm1_final matches mart.v_ceo_ecom_daily.cm1 EXACTLY (sql/046):
    # cm1_final = cm1_raw - hh_operational_packaging_cost. cm2_known must
    # be built on the SAME cm1 users actually see, not the pre-packaging
    # raw Gold value — the CM1_BEFORE=CM1_AFTER invariant (Section 14)
    # refers to that final, exposed cm1.
    all_dates = sorted(set(cm1_raw) | set(ads_spend) | set(affiliate_commission))
    rows = []
    for bd in all_dates:
        c1_raw, ads, aff = cm1_raw.get(bd), ads_spend.get(bd), affiliate_commission.get(bd)
        if c1_raw is None or ads is None or aff is None:
            rows.append((bd, "SHOPEE", shop_id, "cm2_known", None, "MISSING_SOURCE"))
            rows.append((bd, "SHOPEE", shop_id, "cm2", None, "MISSING_SOURCE"))
            continue
        cm1_final = c1_raw - (op_packaging.get(bd) or 0)
        known = cm1_final - ads - aff
        rows.append((bd, "SHOPEE", shop_id, "cm2_known", known, "DERIVABLE"))
        rows.append((bd, "SHOPEE", shop_id, "cm2", known, "PARTIAL_MISSING_INTERNAL_MARKETING_SOURCE"))

    written = guarded_upsert(get_conn(), "PNL_ENRICHMENT", rows)
    print(f"shopee_cm2: dates={len(all_dates)} rows_written={written}")
    computable = sum(1 for bd in all_dates if cm1_raw.get(bd) is not None and ads_spend.get(bd) is not None and affiliate_commission.get(bd) is not None)
    print(f"computable={computable} missing={len(all_dates) - computable}")


if __name__ == "__main__":
    main()
