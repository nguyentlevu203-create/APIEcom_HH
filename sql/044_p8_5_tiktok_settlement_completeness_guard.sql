-- =====================================================================
-- 044_p8_5_tiktok_settlement_completeness_guard.sql
-- P8.5 Section 3-7 — TikTok settlement-completeness data-quality guard.
--
-- PROBLEM: mart.v_ceo_ecom_daily's TikTok net_sales_status only checked
-- whether ANY order for the date was still NOT_SETTLED_YET (out of ALL
-- orders), and never withheld net_sales/gm1/cm1 even when coverage was
-- extremely low. Verified live: 2026-09-12 (41/79 orders still
-- IN_TRANSIT, only 27 MATCHED), 2026-09-13 (69/74 still IN_TRANSIT),
-- 2026-09-14 (55/57 still IN_TRANSIT) were all reporting a numeric
-- net_sales/gm1/cm1 computed from a tiny, unrepresentative matched
-- cohort mixed with full-population COGS -- producing falsely negative
-- GM1/CM1 that looked like real numbers.
--
-- FIX IS A COMPLETENESS GATE ONLY. No change to:
--   - net_sales source precedence (still SETTLEMENT_ACTUAL from
--     core.fact_settlement/fact_settlement_sku_fee)
--   - COGS formula
--   - fee formulas
--   - GM1/CM1 formula
-- The only change is WHEN a TikTok date's net_sales/gm1/cm1 are exposed
-- as a number at all, vs withheld as NULL with an explicit status.
--
-- RULE (validated against all 14 real TikTok dates 2026-09-01..09-14,
-- see P8_5_DATA_RECONCILIATION.csv for the full before/after table):
--   eligible_orders = orders with order_status IN
--                      ('DELIVERED','COMPLETED','CANCELLED')
--                      -- terminal states only; TikTok cannot settle an
--                      -- order still IN_TRANSIT/AWAITING_* by definition
--   pending_orders  = total_orders - eligible_orders
--   pending_pct     = pending_orders / total_orders
--   matched_eligible = eligible orders with settlement_type='MATCHED'
--   coverage_pct    = matched_eligible / eligible_orders
--
--   total_orders = 0                    -> NO_ELIGIBLE_ORDERS
--   eligible_orders = 0                 -> SOURCE_LAGGING
--   pending_pct > 15%                   -> SOURCE_LAGGING
--   matched_eligible = 0                -> NOT_SETTLED_YET
--   coverage_pct < 95%                  -> PARTIAL_SETTLEMENT_COVERAGE
--   else                                -> COMPLETE_SETTLEMENT_COVERAGE
--
-- net_sales/gm1/cm1 are withheld (NULL) only for SOURCE_LAGGING,
-- NOT_SETTLED_YET, NO_ELIGIBLE_ORDERS. PARTIAL_SETTLEMENT_COVERAGE
-- (small residual gaps, e.g. 09-11 at 86.1% coverage / 7.3% pending)
-- KEEPS the existing numeric value -- this exactly matches every date
-- in the already human-validated 2026-09-01..2026-09-10 window (all
-- land on COMPLETE_SETTLEMENT_COVERAGE, values unchanged) plus 09-11
-- (only a status refinement, value unchanged). Only 09-12/09-13/09-14
-- actually change: their net_sales/gm1/cm1 flip from a misleading
-- partial number to NULL + SOURCE_LAGGING.
--
-- Diagnostic-only columns (tiktok_eligible_orders, tiktok_matched_orders,
-- tiktok_settlement_coverage_pct, tiktok_partial_matched_revenue) are
-- added so the withheld/partial figure stays inspectable without ever
-- being presented as complete Net Sales.
-- =====================================================================

CREATE OR REPLACE VIEW mart.v_ceo_ecom_daily AS
WITH gold_wide AS (
    SELECT gold_channel_daily.business_date,
        gold_channel_daily.channel,
        gold_channel_daily.shop_id,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'orders') AS orders,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'units') AS units,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cancelled_orders') AS cancelled_orders,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'platform_gmv') AS platform_gmv,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'net_sales') AS net_sales,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'net_sales') AS net_sales_status_raw,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'sellable_cogs') AS sellable_cogs,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'promo_gift_cost') AS promo_gift_cost,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'gm1') AS gm1,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'gm1') AS gm1_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cm1') AS cm1,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'cm1') AS cm1_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cm2') AS cm2,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'cm2') AS cm2_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_spend') AS ads_spend,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'ads_spend') AS ads_spend_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'packaging_cost') AS hh_internal_packaging_cost,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_fixed_fee_actual') AS tt_fixed_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_payment_fee_actual') AS tt_payment_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_vxp_fee_actual') AS tt_vxp_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_infrastructure_fee_actual') AS tt_infra_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_affiliate_fee_actual') AS tt_affiliate_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'affiliate_commission_estimated') AS tt_affiliate_seller_api,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'shipping_or_fulfillment_fees') AS tt_ship_fulfill_fee,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'shipping_or_fulfillment_fees') AS tt_ship_fulfill_status,
        max(gold_channel_daily.updated_at) AS gold_updated_at
    FROM mart.gold_channel_daily
    GROUP BY gold_channel_daily.business_date, gold_channel_daily.channel, gold_channel_daily.shop_id
), shopee_fee AS (
    SELECT fo.business_date,
        sum(fs.commission_fee) FILTER (WHERE fo.order_status <> 'CANCELLED') AS fixed_fee,
        sum(fs.service_fee) FILTER (WHERE fo.order_status <> 'CANCELLED') AS service_fee,
        sum(fs.seller_transaction_fee) FILTER (WHERE fo.order_status <> 'CANCELLED') AS payment_fee,
        sum(fs.actual_shipping_fee - fs.shopee_shipping_rebate - fs.buyer_paid_shipping_fee + fs.seller_return_refund + fs.drc_adjustable_refund + fs.seller_lost_compensation) FILTER (WHERE fo.order_status <> 'CANCELLED') AS cancel_return_logistics_cost,
        count(*) FILTER (WHERE fo.order_status <> 'CANCELLED') AS non_cancelled_orders,
        count(*) FILTER (WHERE fs.order_id IS NOT NULL) AS orders_with_escrow,
        count(*) AS total_orders,
        max(fs.ingested_at) AS source_updated_at
    FROM core.fact_order fo
        LEFT JOIN core.fact_settlement fs ON fs.channel = fo.channel AND fs.shop_id = fo.shop_id AND fs.order_id = fo.order_id AND fs.settlement_type = 'escrow_estimate'
    WHERE fo.channel = 'SHOPEE'
    GROUP BY fo.business_date
), tiktok_fee_pop AS (
    -- P8.5 completeness guard: eligible = terminal order_status only.
    SELECT fo.business_date,
        count(*) AS total_orders,
        count(*) FILTER (WHERE fo.order_status IN ('DELIVERED','COMPLETED','CANCELLED')) AS eligible_orders,
        count(*) FILTER (WHERE fo.order_status NOT IN ('DELIVERED','COMPLETED','CANCELLED')) AS pending_orders,
        count(*) FILTER (WHERE fo.order_status IN ('DELIVERED','COMPLETED','CANCELLED') AND fs.settlement_type = 'MATCHED') AS matched_eligible_orders,
        count(*) FILTER (WHERE fs.settlement_type = 'MATCHED') AS matched_orders,
        count(*) FILTER (WHERE fs.settlement_type = 'NOT_SETTLED_YET') AS not_settled_orders,
        max(fs.ingested_at) AS source_updated_at
    FROM core.fact_order fo
        LEFT JOIN core.fact_settlement fs ON fs.channel = fo.channel AND fs.shop_id = fo.shop_id AND fs.order_id = fo.order_id
    WHERE fo.channel = 'TIKTOK'
    GROUP BY fo.business_date
), tiktok_completeness AS (
    SELECT business_date,
        total_orders, eligible_orders, pending_orders, matched_eligible_orders,
        CASE WHEN eligible_orders > 0 THEN round(100.0 * matched_eligible_orders / eligible_orders, 1) ELSE NULL END AS coverage_pct,
        CASE
            WHEN total_orders = 0 THEN 'NO_ELIGIBLE_ORDERS'
            WHEN eligible_orders = 0 THEN 'SOURCE_LAGGING'
            WHEN pending_orders::numeric / total_orders > 0.15 THEN 'SOURCE_LAGGING'
            WHEN matched_eligible_orders = 0 THEN 'NOT_SETTLED_YET'
            WHEN matched_eligible_orders::numeric / eligible_orders < 0.95 THEN 'PARTIAL_SETTLEMENT_COVERAGE'
            ELSE 'COMPLETE_SETTLEMENT_COVERAGE'
        END AS completeness_status
    FROM tiktok_fee_pop
)
SELECT g.business_date,
    g.channel,
    g.shop_id,
    g.orders,
    g.units,
    g.cancelled_orders,
    CASE WHEN g.orders > 0::numeric THEN round(g.cancelled_orders / g.orders, 4) ELSE NULL::numeric END AS cancellation_rate,
    g.platform_gmv,
    -- withheld only for the three "incomplete" TikTok completeness states
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN NULL
        ELSE g.net_sales
    END AS net_sales,
    CASE
        WHEN g.channel = 'SHOPEE' THEN g.net_sales_status_raw
        WHEN g.channel = 'TIKTOK' THEN tc.completeness_status
        ELSE g.net_sales_status_raw
    END AS net_sales_status,
    CASE
        WHEN g.channel = 'SHOPEE' THEN 'API_ACTUAL/ESTIMATE (Shopee escrow is always "escrow_estimate", never a confirmed final payout in current evidence)'
        ELSE 'SETTLEMENT_ACTUAL'
    END AS net_sales_basis,
    g.sellable_cogs,
    g.promo_gift_cost,
    COALESCE(g.sellable_cogs, 0::numeric) + COALESCE(g.promo_gift_cost, 0::numeric) AS total_cogs,
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN NULL
        ELSE g.gm1
    END AS gm1,
    CASE
        WHEN g.net_sales IS NOT NULL AND g.net_sales <> 0::numeric AND g.gm1 IS NOT NULL
             AND NOT (g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS'))
        THEN round(g.gm1 / g.net_sales, 4)
        ELSE NULL::numeric
    END AS gm1_margin,
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN tc.completeness_status
        ELSE g.gm1_status
    END AS gm1_status,
    CASE WHEN g.channel = 'SHOPEE' THEN sf.fixed_fee ELSE g.tt_fixed_fee END AS fixed_fee,
    CASE WHEN g.channel = 'SHOPEE' THEN sf.service_fee ELSE NULL::numeric END AS service_fee,
    CASE WHEN g.channel = 'SHOPEE' THEN sf.payment_fee ELSE g.tt_payment_fee END AS payment_fee,
    CASE WHEN g.channel = 'TIKTOK' THEN g.tt_vxp_fee ELSE NULL::numeric END AS vxp_fee,
    CASE WHEN g.channel = 'TIKTOK' THEN g.tt_infra_fee ELSE NULL::numeric END AS infrastructure_fee,
    CASE WHEN g.channel = 'TIKTOK' THEN g.tt_affiliate_fee ELSE NULL::numeric END AS affiliate_fee,
    g.ads_spend,
    g.hh_internal_packaging_cost,
    CASE WHEN g.channel = 'TIKTOK' AND g.tt_ship_fulfill_status = 'READY' THEN g.tt_ship_fulfill_fee ELSE NULL::numeric END AS platform_packaging_or_fulfillment_fee,
    CASE WHEN g.channel = 'SHOPEE' THEN sf.cancel_return_logistics_cost ELSE NULL::numeric END AS cancel_return_logistics_cost,
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN NULL
        ELSE g.cm1
    END AS cm1,
    CASE
        WHEN g.net_sales IS NOT NULL AND g.net_sales <> 0::numeric AND g.cm1 IS NOT NULL
             AND NOT (g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS'))
        THEN round(g.cm1 / g.net_sales, 4)
        ELSE NULL::numeric
    END AS cm1_margin,
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN tc.completeness_status
        ELSE g.cm1_status
    END AS cm1_status,
    NULL::numeric AS booking_kol_koc,
    NULL::numeric AS live_inhouse_cost,
    g.cm2,
    CASE WHEN g.net_sales IS NOT NULL AND g.net_sales <> 0::numeric AND g.cm2 IS NOT NULL THEN round(g.cm2 / g.net_sales, 4) ELSE NULL::numeric END AS cm2_margin,
    COALESCE(g.cm2_status, 'MISSING_SOURCE') AS cm2_status,
    NULL::numeric AS backoffice_cost,
    NULL::numeric AS profit,
    NULL::numeric AS profit_margin,
    CASE WHEN g.cm2 IS NULL THEN COALESCE(g.cm2_status, 'MISSING_SOURCE') ELSE 'RULE_NOT_APPROVED' END AS profit_status,
    GREATEST(g.gold_updated_at, CASE WHEN g.channel = 'SHOPEE' THEN sf.source_updated_at ELSE tp.source_updated_at END) AS source_updated_at,
    CASE
        WHEN g.channel = 'TIKTOK' AND tc.completeness_status IN ('SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS') THEN 'SOURCE_LATENCY'
        WHEN g.net_sales IS NULL THEN 'STALE'
        ELSE 'FRESH'
    END AS data_freshness_status,
    -- diagnostic-only, TikTok: the partial/withheld figures stay inspectable,
    -- never substituted for net_sales/gm1/cm1 above.
    CASE WHEN g.channel = 'TIKTOK' THEN tc.eligible_orders ELSE NULL::bigint END AS tiktok_eligible_orders,
    CASE WHEN g.channel = 'TIKTOK' THEN tc.matched_eligible_orders ELSE NULL::bigint END AS tiktok_matched_orders,
    CASE WHEN g.channel = 'TIKTOK' THEN tc.coverage_pct ELSE NULL::numeric END AS tiktok_settlement_coverage_pct,
    CASE WHEN g.channel = 'TIKTOK' THEN g.net_sales ELSE NULL::numeric END AS tiktok_partial_matched_revenue
FROM gold_wide g
    LEFT JOIN shopee_fee sf ON g.channel = 'SHOPEE' AND sf.business_date = g.business_date
    LEFT JOIN tiktok_fee_pop tp ON g.channel = 'TIKTOK' AND tp.business_date = g.business_date
    LEFT JOIN tiktok_completeness tc ON g.channel = 'TIKTOK' AND tc.business_date = g.business_date;
