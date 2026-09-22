#!/usr/bin/env python3
"""P6A — Gold foundation build for HH Ecom (Shopee + TikTok only).

Populates mart.gold_channel_daily (grain: business_date, channel,
shop_id, metric_name) directly from core.* production tables — no P5
CSV artifacts are read for values, only used later as reconciliation
evidence. Every metric is written as (value, coverage_status); a metric
with no computable source for a given day is written value=NULL with an
explicit status, never silently defaulted to 0.

Read-mostly: one short-lived connection per query burst (Neon
reliability convention), one final short connection for the UPSERT
burst into mart.gold_channel_daily.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import keyring
import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from _gold_ownership import guarded_upsert  # noqa: E402
from _p5b_load import build_role_map_from_db  # noqa: E402
from _p5b_recompute import fetch_snapshot, get_conn, run_pass  # noqa: E402

CHANNELS = ["SHOPEE", "TIKTOK"]

# metric_name -> (business_definition, source_table, source_field/formula, grain)
METRIC_DEFS = {
    "orders": ("Distinct orders placed (all statuses)", "core.fact_order", "count(DISTINCT order_id)", "business_date, channel, shop_id"),
    "units": ("Total units across order-item lines (all statuses)", "core.fact_order_item", "sum(qty)", "business_date, channel, shop_id"),
    "platform_gmv": ("Order-level GMV as reported by the platform (all statuses) — NOT settlement, NOT HH Net Sales", "core.fact_order", "sum(total_amount)", "business_date, channel, shop_id"),
    "cancelled_orders": ("Orders with order_status='CANCELLED'", "core.fact_order", "count(*) WHERE order_status='CANCELLED'", "business_date, channel, shop_id"),
    "cancelled_value": ("total_amount of cancelled orders", "core.fact_order", "sum(total_amount) WHERE order_status='CANCELLED'", "business_date, channel, shop_id"),
    "refund_orders": ("Distinct orders with a return/refund record", "core.fact_return_refund", "count(DISTINCT order_id)", "business_date, channel, shop_id"),
    "refund_value": ("Total refund amount", "core.fact_return_refund", "sum(refund_amount)", "business_date, channel, shop_id"),
    "sellable_cogs": ("COGS for SALE-role lines (direct + combo), HH_OFFICIAL_COGS/TIME_INDEPENDENT", "core.fact_order_item + map_platform_product + dim_cogs + dim_combo_bom", "P5 production compute engine, cost_treatment=SELLABLE_COGS", "business_date, channel, shop_id"),
    "promo_gift_cost": ("Cost of lines with transaction_role=PROMO_GIFT", "same as sellable_cogs", "P5 engine, cost_treatment=PROMO_GIFT_COST", "business_date, channel, shop_id"),
    "packaging_cost": ("Cost of lines with transaction_role=PACKAGING", "same as sellable_cogs", "P5 engine, cost_treatment=PACKAGING_COST", "business_date, channel, shop_id"),
    "total_cost": ("sellable_cogs + promo_gift_cost + packaging_cost", "same as sellable_cogs", "P5 engine, total_cost", "business_date, channel, shop_id"),
    "platform_fees": ("Platform commission fee, isolated from tax/payment fees", "core.fact_settlement", "no field isolates platform commission alone", "business_date, channel, shop_id"),
    "payment_fees": ("Payment processing fee, isolated", "core.fact_settlement", "no field isolates payment fee alone", "business_date, channel, shop_id"),
    "shipping_or_fulfillment_fees": ("Shipping/fulfillment fee deducted at settlement", "core.fact_settlement", "sum(shipping_cost_amount) (TikTok only field)", "business_date, channel, shop_id"),
    "other_settlement_fees": ("Any other settlement deduction not covered above", "core.fact_settlement", "not separable from fee_and_tax_amount", "business_date, channel, shop_id"),
    "settlement_fee_and_tax_total": ("Combined fee+tax deducted at settlement (TikTok escrow field, not decomposable further)", "core.fact_settlement", "sum(fee_and_tax_amount) WHERE settlement_type='MATCHED'", "business_date, channel, shop_id"),
    "settlement_amount_estimated": ("Blended settlement/escrow total as currently reported (Shopee: escrow estimate; TikTok: MATCHED net settlement)", "core.fact_settlement", "sum(settlement_amount)", "business_date, channel, shop_id"),
    "ads_spend": ("Paid ads spend (Shopee CPC ads API; TikTok has no ads collector built)", "core.fact_ads_daily", "sum(spend)", "business_date, channel, shop_id"),
    "affiliate_commission_estimated": ("Estimated affiliate/creator commission, before settlement", "core.fact_affiliate_daily", "sum(estimated_commission)", "business_date, channel, shop_id"),
    "affiliate_commission_settled": ("Affiliate commission that has actually settled (final)", "core.fact_affiliate_daily", "sum(settled_commission) WHERE settlement_status='SETTLED'/'INELIGIBLE' only (no pending rows that day)", "business_date, channel, shop_id"),
    "inventory_units_on_hand": ("Units on hand across SKUs, single point-in-time snapshot", "core.fact_inventory_snapshot", "sum(qty_on_hand)", "business_date, channel, shop_id"),
    "net_sales": ("HH's defined net revenue after all approved deductions from platform_gmv", "TBD — no HH-confirmed formula yet", "NOT COMPUTED", "business_date, channel, shop_id"),
    "gross_margin": ("net_sales - sellable_cogs", "derived", "blocked: net_sales not READY", "business_date, channel, shop_id"),
    "variable_platform_fees": ("platform_fees + payment_fees + shipping_or_fulfillment_fees, all variable-with-sales fees", "derived", "blocked: components not READY", "business_date, channel, shop_id"),
    "cm1": ("gross_margin - variable_platform_fees", "derived", "blocked: upstream not READY", "business_date, channel, shop_id"),
    "cm2": ("cm1 - ads_spend - affiliate_cost - promo_gift_cost - other_approved_variable_cost", "derived", "blocked: ads_spend/affiliate not READY on both channels simultaneously", "business_date, channel, shop_id"),
}

STATUS = {  # shorthand
    "READY": "READY", "DERIVABLE": "DERIVABLE", "NO_PERM": "NO_PERMISSION",
    "SEP_API": "SEPARATE_API_REQUIRED", "LATENCY": "SOURCE_LATENCY",
    "NOT_SETTLED": "NOT_SETTLED_YET", "MISSING": "MISSING_SOURCE", "NA": "NOT_APPLICABLE",
}


def D(x):
    return x if x is not None else None


def main():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT channel, shop_id FROM core.fact_order;")
    shop_by_channel = dict(cur.fetchall())

    cur.execute(
        "SELECT channel, business_date, count(DISTINCT order_id), "
        "count(*) FILTER (WHERE order_status='CANCELLED'), "
        "sum(total_amount), sum(total_amount) FILTER (WHERE order_status='CANCELLED') "
        "FROM core.fact_order GROUP BY 1,2;"
    )
    order_agg = {(c, d): (o, co, gmv, cv) for c, d, o, co, gmv, cv in cur.fetchall()}
    date_universe = sorted(order_agg.keys())

    cur.execute("SELECT channel, business_date, sum(qty) FROM core.fact_order_item GROUP BY 1,2;")
    units_agg = {(c, d): u for c, d, u in cur.fetchall()}

    cur.execute(
        "SELECT channel, business_date, count(DISTINCT order_id), sum(refund_amount) "
        "FROM core.fact_return_refund GROUP BY 1,2;"
    )
    refund_agg = {(c, d): (ro, rv) for c, d, ro, rv in cur.fetchall()}

    # P8.5 — approved ads-aggregation fix. core.fact_ads_daily stores one
    # row per (business_date, attribution_basis in {BROAD, DIRECT}); both
    # rows share identical impressions/clicks/spend (proven live: 14/14
    # dates bit-for-bit equal) but carry genuinely different orders/gmv
    # per attribution basis. The old query summed spend across both rows,
    # double-counting it exactly 2x (137,904,822 vs the real 68,952,411
    # over the 12 previously-populated dates). MAX() is used for the
    # shared delivery fields (not SUM) since they are proven identical,
    # not additive; orders/gmv stay split explicitly by attribution basis
    # and are never summed together.
    cur.execute(
        """
        SELECT business_date,
            max(impressions) AS impressions,
            max(clicks) AS clicks,
            max(spend) AS spend,
            sum(orders) FILTER (WHERE attribution_basis='BROAD') AS orders_broad,
            sum(gmv) FILTER (WHERE attribution_basis='BROAD') AS gmv_broad,
            sum(orders) FILTER (WHERE attribution_basis='DIRECT') AS orders_direct,
            sum(gmv) FILTER (WHERE attribution_basis='DIRECT') AS gmv_direct
        FROM core.fact_ads_daily WHERE channel='SHOPEE' GROUP BY 1;
        """
    )
    cols = ["impressions", "clicks", "spend", "orders_broad", "gmv_broad", "orders_direct", "gmv_direct"]
    shopee_ads_detail = {row[0]: dict(zip(cols, row[1:])) for row in cur.fetchall()}
    shopee_ads_agg = {bd: v["spend"] for bd, v in shopee_ads_detail.items()}

    cur.execute(
        "SELECT business_date, sum(estimated_commission), sum(settled_commission), "
        "bool_or(settlement_status IN ('AWAITING PAYMENT', 'To-SETTLE')) "
        "FROM core.fact_affiliate_daily WHERE channel='TIKTOK' GROUP BY 1;"
    )
    tiktok_aff_agg = {d: (est, settled, pending) for d, est, settled, pending in cur.fetchall()}

    cur.execute(
        "SELECT channel, business_date, settlement_type, sum(settlement_amount), "
        "sum(revenue_amount), sum(fee_and_tax_amount), sum(shipping_cost_amount), count(*) "
        "FROM core.fact_settlement GROUP BY 1,2,3;"
    )
    settlement_rows = cur.fetchall()

    cur.execute("SELECT channel, business_date, sum(qty_on_hand) FROM core.fact_inventory_snapshot GROUP BY 1,2;")
    inv_agg = {(c, d): q for c, d, q in cur.fetchall()}

    conn.close()

    # settlement: bucket by (channel, date) -> dict of settlement_type -> tuple
    settlement_agg = defaultdict(dict)
    for c, d, stype, samt, rev, fee, ship, cnt in settlement_rows:
        settlement_agg[(c, d)][stype] = {"settlement_amount": samt, "revenue": rev, "fee_and_tax": fee, "shipping": ship, "count": cnt}

    # ---------------- COGS per day, via the P5 production engine ----------------
    # P11-QUATER-BIS: role_map now sourced from core.dim_product (Neon),
    # not the local _p5a_keymap.json — see build_role_map_from_db().
    conn = get_conn()
    cur = conn.cursor()
    role_map = build_role_map_from_db(cur)
    snap = fetch_snapshot(cur)
    cur.execute("SELECT order_item_key, business_date FROM core.fact_order_item;")
    bd_by_key = dict(cur.fetchall())
    conn.close()

    lines = run_pass(snap, role_map)
    cogs_agg = defaultdict(lambda: {"sellable": Decimal(0), "promo": Decimal(0), "packaging": Decimal(0), "total": Decimal(0), "exceptions": 0})
    cogs_totals = {"sellable": Decimal(0), "promo": Decimal(0), "packaging": Decimal(0), "total": Decimal(0)}
    for r, row in zip(lines, snap["order_items"]):
        order_item_key = row[0]
        channel, shop_id = r["channel"], r["shop_id"]
        bd = bd_by_key[order_item_key]
        key = (channel, bd)
        if r["cost_status"] != "OK":
            cogs_agg[key]["exceptions"] += 1
            continue
        cogs_agg[key]["sellable"] += r["sellable_product_cogs"]
        cogs_agg[key]["promo"] += r["promo_gift_cost"]
        cogs_agg[key]["packaging"] += r["packaging_cost"]
        cogs_agg[key]["total"] += r["total_cost"]
        cogs_totals["sellable"] += r["sellable_product_cogs"]
        cogs_totals["promo"] += r["promo_gift_cost"]
        cogs_totals["packaging"] += r["packaging_cost"]
        cogs_totals["total"] += r["total_cost"]

    total_cogs_exceptions = sum(v["exceptions"] for v in cogs_agg.values())

    # ---------------- assemble gold rows ----------------
    rows = []  # (business_date, channel, shop_id, metric_name, value, status)

    def emit(bd, channel, metric, value, status):
        shop_id = shop_by_channel[channel]
        rows.append((bd, channel, shop_id, metric, value, status))

    for (channel, bd), (o, co, gmv, cv) in order_agg.items():
        emit(bd, channel, "orders", o, STATUS["READY"])
        emit(bd, channel, "cancelled_orders", co, STATUS["READY"])
        emit(bd, channel, "platform_gmv", gmv, STATUS["READY"])
        emit(bd, channel, "cancelled_value", cv, STATUS["READY"])

        u = units_agg.get((channel, bd))
        emit(bd, channel, "units", u, STATUS["READY"] if u is not None else STATUS["MISSING"])

        # refunds
        if channel == "SHOPEE":
            # Confirmed persistent Shopee-side transient API error (P3/P4) —
            # zero SHOPEE rows ever collected, not a real zero.
            emit(bd, channel, "refund_orders", None, STATUS["LATENCY"])
            emit(bd, channel, "refund_value", None, STATUS["LATENCY"])
        else:
            rf = refund_agg.get((channel, bd))
            if rf is None:
                emit(bd, channel, "refund_orders", 0, STATUS["READY"])
                emit(bd, channel, "refund_value", 0, STATUS["READY"])
            else:
                emit(bd, channel, "refund_orders", rf[0], STATUS["READY"])
                emit(bd, channel, "refund_value", rf[1], STATUS["READY"])

        # COGS
        c = cogs_agg.get((channel, bd))
        if c is None:
            emit(bd, channel, "sellable_cogs", None, STATUS["MISSING"])
            emit(bd, channel, "promo_gift_cost", None, STATUS["MISSING"])
            emit(bd, channel, "packaging_cost", None, STATUS["MISSING"])
            emit(bd, channel, "total_cost", None, STATUS["MISSING"])
        else:
            # Section 11: a date with ANY unresolved COGS line must never
            # report sellable_cogs/total_cost as a falsely-confident READY
            # figure computed from the resolvable subset only.
            cogs_status = "COGS_INCOMPLETE" if c["exceptions"] > 0 else STATUS["READY"]
            emit(bd, channel, "sellable_cogs", c["sellable"], cogs_status)
            emit(bd, channel, "promo_gift_cost", c["promo"], cogs_status)
            emit(bd, channel, "packaging_cost", c["packaging"], cogs_status)
            emit(bd, channel, "total_cost", c["total"], cogs_status)

        # settlement-derived
        by_type = settlement_agg.get((channel, bd), {})
        if channel == "SHOPEE":
            est = by_type.get("escrow_estimate")
            not_elig = by_type.get("not_finance_eligible")
            if est is not None:
                emit(bd, channel, "settlement_amount_estimated", est["settlement_amount"], STATUS["READY"])
            elif not_elig is not None:
                emit(bd, channel, "settlement_amount_estimated", None, STATUS["NOT_SETTLED"])
            else:
                emit(bd, channel, "settlement_amount_estimated", None, STATUS["MISSING"])
            emit(bd, channel, "settlement_fee_and_tax_total", None, STATUS["MISSING"])
            emit(bd, channel, "shipping_or_fulfillment_fees", None, STATUS["MISSING"])
        else:
            matched = by_type.get("MATCHED")
            pending = by_type.get("NOT_SETTLED_YET")
            unknown = by_type.get("UNKNOWN")
            if matched and not (pending or unknown):
                # every order for this date is fully settled — totals are final.
                emit(bd, channel, "settlement_amount_estimated", matched["settlement_amount"], STATUS["READY"])
                emit(bd, channel, "settlement_fee_and_tax_total", matched["fee_and_tax"], STATUS["READY"])
                emit(bd, channel, "shipping_or_fulfillment_fees", matched["shipping"], STATUS["READY"])
            elif matched and (pending or unknown):
                # partial: some orders for this date settled, some still
                # pending — show the partial sum but flag it explicitly so
                # it is never mistaken for the day's final total.
                emit(bd, channel, "settlement_amount_estimated", matched["settlement_amount"], STATUS["NOT_SETTLED"])
                emit(bd, channel, "settlement_fee_and_tax_total", matched["fee_and_tax"], STATUS["NOT_SETTLED"])
                emit(bd, channel, "shipping_or_fulfillment_fees", matched["shipping"], STATUS["NOT_SETTLED"])
            elif pending or unknown:
                emit(bd, channel, "settlement_amount_estimated", None, STATUS["NOT_SETTLED"])
                emit(bd, channel, "settlement_fee_and_tax_total", None, STATUS["NOT_SETTLED"])
                emit(bd, channel, "shipping_or_fulfillment_fees", None, STATUS["NOT_SETTLED"])
            else:
                emit(bd, channel, "settlement_amount_estimated", None, STATUS["MISSING"])
                emit(bd, channel, "settlement_fee_and_tax_total", None, STATUS["MISSING"])
                emit(bd, channel, "shipping_or_fulfillment_fees", None, STATUS["MISSING"])
        emit(bd, channel, "platform_fees", None, STATUS["MISSING"])
        emit(bd, channel, "payment_fees", None, STATUS["MISSING"])
        emit(bd, channel, "other_settlement_fees", None, STATUS["MISSING"])

        # ads — P8.5 approved fix: common delivery metrics counted ONCE
        # (MAX across the BROAD/DIRECT rows, proven identical), attribution
        # -specific orders/gmv kept split, never summed across basis.
        if channel == "SHOPEE":
            ads = shopee_ads_detail.get(bd, {})
            spend = ads.get("spend")
            ready = STATUS["READY"] if spend is not None else STATUS["MISSING"]
            emit(bd, channel, "ads_spend", spend, ready)
            emit(bd, channel, "ads_impressions", ads.get("impressions"), ready)
            emit(bd, channel, "ads_clicks", ads.get("clicks"), ready)
            emit(bd, channel, "ads_orders_broad", ads.get("orders_broad"), ready)
            emit(bd, channel, "ads_gmv_broad", ads.get("gmv_broad"), ready)
            emit(bd, channel, "ads_orders_direct", ads.get("orders_direct"), ready)
            emit(bd, channel, "ads_gmv_direct", ads.get("gmv_direct"), ready)
        else:
            emit(bd, channel, "ads_spend", None, STATUS["SEP_API"])

        # affiliate
        if channel == "SHOPEE":
            emit(bd, channel, "affiliate_commission_estimated", None, STATUS["NO_PERM"])
            emit(bd, channel, "affiliate_commission_settled", None, STATUS["NO_PERM"])
        else:
            a = tiktok_aff_agg.get(bd)
            if a is None:
                emit(bd, channel, "affiliate_commission_estimated", None, STATUS["MISSING"])
                emit(bd, channel, "affiliate_commission_settled", None, STATUS["MISSING"])
            else:
                est, settled, pending = a
                emit(bd, channel, "affiliate_commission_estimated", est, STATUS["DERIVABLE"])
                if pending:
                    emit(bd, channel, "affiliate_commission_settled", None, STATUS["NOT_SETTLED"])
                else:
                    emit(bd, channel, "affiliate_commission_settled", settled or Decimal(0), STATUS["DERIVABLE"])

        # inventory
        inv = inv_agg.get((channel, bd))
        emit(bd, channel, "inventory_units_on_hand", inv, STATUS["DERIVABLE"] if inv is not None else STATUS["MISSING"])

        # net_sales / gm1 / gross_margin / variable_platform_fees / cm1 / cm2 / profit
        # are PNL_ENRICHMENT-owned (mart.gold_metric_ownership, P6C.1 Section 8-9).
        # BASE GOLD MUST NEVER WRITE THEM — see _gold_ownership.py, which refuses
        # any row here whose metric_name is not registered to BASE_GOLD.

    return rows, date_universe, shop_by_channel, order_agg, units_agg, cogs_totals, total_cogs_exceptions, shopee_ads_agg, tiktok_aff_agg


def upsert_rows(rows):
    conn = get_conn()
    n = guarded_upsert(conn, "BASE_GOLD", rows)
    conn.close()
    return n


if __name__ == "__main__":
    rows, date_universe, shop_by_channel, order_agg, units_agg, cogs_totals, total_cogs_exceptions, shopee_ads_agg, tiktok_aff_agg = main()
    upsert_rows(rows)
    print(f"rows_written={len(rows)} dates={len(date_universe)} cogs_exceptions={total_cogs_exceptions}")
    print(f"cogs_totals={dict(cogs_totals)}")
