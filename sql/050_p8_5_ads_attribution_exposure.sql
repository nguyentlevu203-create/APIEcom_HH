-- =====================================================================
-- 050_p8_5_ads_attribution_exposure.sql
-- P8.5 — Shopee Ads attribution double-count fix, Mart exposure.
--
-- Context: core.fact_ads_daily carries 2 rows/date for Shopee (BROAD and
-- DIRECT attribution_basis) that share IDENTICAL delivery metrics
-- (impressions, clicks, spend) but genuinely different orders/gmv.
-- _p6a_gold_build.py previously SUMmed spend across both rows, causing
-- an exact 2x overstatement of ads_spend in mart.gold_channel_daily
-- (verified: 137,904,822 vs correct 68,952,411 over 12 dates). Fixed in
-- _p6a_gold_build.py (approved) to MAX() the shared delivery fields and
-- keep orders/gmv split by attribution_basis, never summed. This
-- migration exposes the corrected/new Gold metrics through
-- mart.v_ceo_ecom_daily. ads_spend itself needs no exposure change —
-- the existing column now just carries the corrected (non-doubled)
-- value automatically.
--
-- New metrics registered as BASE_GOLD in mart.gold_metric_ownership:
-- ads_impressions, ads_clicks, ads_orders_broad, ads_gmv_broad,
-- ads_orders_direct, ads_gmv_direct.
--
-- All new output columns appended at the very end of the SELECT list
-- (CREATE OR REPLACE VIEW cannot reorder/rename existing columns).
--
-- NOTE: this file's CREATE OR REPLACE VIEW body is superseded by
-- sql/051_p8_5_shopee_affiliate_cm2.sql, which appends the Shopee
-- Affiliate / CM2 columns. Kept for history; re-run 051, not this file,
-- to rebuild the view from scratch.
-- =====================================================================

CREATE OR REPLACE VIEW mart.v_ceo_ecom_daily AS
WITH gold_wide AS (
    SELECT gold_channel_daily.business_date,
        gold_channel_daily.channel,
        gold_channel_daily.shop_id,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'orders') AS orders,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'units') AS units,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cancelled_orders') AS cancelled_orders,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cancelled_value') AS cancelled_value,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'cancelled_value') AS cancelled_value_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'refund_orders') AS refund_orders,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'refund_value') AS refund_value,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'refund_value') AS refund_value_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'platform_gmv') AS platform_gmv,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'net_sales') AS net_sales,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'net_sales') AS net_sales_status_raw,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'sellable_cogs') AS sellable_cogs,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'promo_gift_cost') AS promo_gift_cost,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'gm1') AS gm1,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'gm1') AS gm1_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cm1') AS cm1_raw,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'cm1') AS cm1_status_raw,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'cm2') AS cm2,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'cm2') AS cm2_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_spend') AS ads_spend,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'ads_spend') AS ads_spend_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'packaging_cost') AS hh_internal_packaging_cost,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'hh_operational_packaging_cost') AS hh_operational_packaging_cost,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'hh_operational_packaging_cost') AS hh_operational_packaging_cost_status,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_fixed_fee_actual') AS tt_fixed_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_payment_fee_actual') AS tt_payment_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_vxp_fee_actual') AS tt_vxp_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_infrastructure_fee_actual') AS tt_infra_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'tiktok_affiliate_fee_actual') AS tt_affiliate_fee,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'affiliate_commission_estimated') AS tt_affiliate_seller_api,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'shipping_or_fulfillment_fees') AS tt_ship_fulfill_fee,
        max(gold_channel_daily.coverage_status) FILTER (WHERE gold_channel_daily.metric_name = 'shipping_or_fulfillment_fees') AS tt_ship_fulfill_status,
        -- P8.5 ads-attribution fix: common delivery metrics (counted once)
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_impressions') AS ads_impressions,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_clicks') AS ads_clicks,
        -- attribution-specific orders/gmv, kept split, never summed
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_orders_broad') AS ads_orders_broad,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_gmv_broad') AS ads_gmv_broad,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_orders_direct') AS ads_orders_direct,
        max(gold_channel_daily.metric_value) FILTER (WHERE gold_channel_daily.metric_name = 'ads_gmv_direct') AS ads_gmv_direct,
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
    SELECT fo.business_date,
        count(*) AS total_orders,
        count(*) FILTER (WHERE fo.order_status = ANY (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AS eligible_orders,
        count(*) FILTER (WHERE fo.order_status <> ALL (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AS pending_orders,
        count(*) FILTER (WHERE (fo.order_status = ANY (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AND fs.settlement_type = 'MATCHED') AS matched_eligible_orders,
        count(*) FILTER (WHERE fs.settlement_type = 'MATCHED') AS matched_orders,
        count(*) FILTER (WHERE fs.settlement_type = 'NOT_SETTLED_YET') AS not_settled_orders,
        max(fs.ingested_at) AS source_updated_at
    FROM core.fact_order fo
        LEFT JOIN core.fact_settlement fs ON fs.channel = fo.channel AND fs.shop_id = fo.shop_id AND fs.order_id = fo.order_id
    WHERE fo.channel = 'TIKTOK'
    GROUP BY fo.business_date
), tiktok_completeness AS (
    SELECT tiktok_fee_pop.business_date,
        tiktok_fee_pop.total_orders,
        tiktok_fee_pop.eligible_orders,
        tiktok_fee_pop.pending_orders,
        tiktok_fee_pop.matched_eligible_orders,
        CASE
            WHEN tiktok_fee_pop.eligible_orders > 0 THEN round(100.0 * tiktok_fee_pop.matched_eligible_orders::numeric / tiktok_fee_pop.eligible_orders::numeric, 1)
            ELSE NULL::numeric
        END AS coverage_pct,
        CASE
            WHEN tiktok_fee_pop.total_orders = 0 THEN 'NO_ELIGIBLE_ORDERS'
            WHEN tiktok_fee_pop.eligible_orders = 0 THEN 'SOURCE_LAGGING'
            WHEN (tiktok_fee_pop.pending_orders::numeric / tiktok_fee_pop.total_orders::numeric) > 0.15 THEN 'SOURCE_LAGGING'
            WHEN tiktok_fee_pop.matched_eligible_orders = 0 THEN 'NOT_SETTLED_YET'
            WHEN (tiktok_fee_pop.matched_eligible_orders::numeric / tiktok_fee_pop.eligible_orders::numeric) < 0.95 THEN 'PARTIAL_SETTLEMENT_COVERAGE'
            ELSE 'COMPLETE_SETTLEMENT_COVERAGE'
        END AS completeness_status
    FROM tiktok_fee_pop
), incomplete AS (
    SELECT g_1.business_date,
        g_1.channel,
        g_1.channel = 'TIKTOK' AND (tc_1.completeness_status = ANY (ARRAY['SOURCE_LAGGING','NOT_SETTLED_YET','NO_ELIGIBLE_ORDERS'])) AS is_incomplete
    FROM gold_wide g_1
        LEFT JOIN tiktok_completeness tc_1 ON g_1.channel = 'TIKTOK' AND tc_1.business_date = g_1.business_date
)
SELECT g.business_date,
    g.channel,
    g.shop_id,
    g.orders,
    g.units,
    g.cancelled_orders,
    CASE WHEN g.orders > 0::numeric THEN round(g.cancelled_orders / g.orders, 4) ELSE NULL::numeric END AS cancellation_rate,
    g.platform_gmv,
    CASE WHEN inc.is_incomplete THEN NULL::numeric ELSE g.net_sales END AS net_sales,
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
    CASE WHEN inc.is_incomplete THEN NULL::numeric ELSE g.gm1 END AS gm1,
    CASE
        WHEN g.net_sales IS NOT NULL AND g.net_sales <> 0::numeric AND g.gm1 IS NOT NULL AND NOT inc.is_incomplete THEN round(g.gm1 / g.net_sales, 4)
        ELSE NULL::numeric
    END AS gm1_margin,
    CASE WHEN inc.is_incomplete THEN tc.completeness_status ELSE g.gm1_status END AS gm1_status,
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
        WHEN inc.is_incomplete THEN NULL::numeric
        WHEN g.cm1_raw IS NULL THEN NULL::numeric
        ELSE g.cm1_raw - COALESCE(g.hh_operational_packaging_cost, 0::numeric)
    END AS cm1,
    CASE
        WHEN g.net_sales IS NOT NULL AND g.net_sales <> 0::numeric AND g.cm1_raw IS NOT NULL AND NOT inc.is_incomplete THEN round((g.cm1_raw - COALESCE(g.hh_operational_packaging_cost, 0::numeric)) / g.net_sales, 4)
        ELSE NULL::numeric
    END AS cm1_margin,
    CASE WHEN inc.is_incomplete THEN tc.completeness_status ELSE g.cm1_status_raw END AS cm1_status,
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
    CASE WHEN inc.is_incomplete THEN 'SOURCE_LATENCY' WHEN g.net_sales IS NULL THEN 'STALE' ELSE 'FRESH' END AS data_freshness_status,
    CASE WHEN g.channel = 'TIKTOK' THEN tc.eligible_orders ELSE NULL::bigint END AS tiktok_eligible_orders,
    CASE WHEN g.channel = 'TIKTOK' THEN tc.matched_eligible_orders ELSE NULL::bigint END AS tiktok_matched_orders,
    CASE WHEN g.channel = 'TIKTOK' THEN tc.coverage_pct ELSE NULL::numeric END AS tiktok_settlement_coverage_pct,
    CASE WHEN g.channel = 'TIKTOK' THEN g.net_sales ELSE NULL::numeric END AS tiktok_partial_matched_revenue,
    g.hh_internal_packaging_cost AS packaging_item_cogs,
    g.hh_operational_packaging_cost,
    COALESCE(g.hh_operational_packaging_cost_status, 'MISSING_SOURCE') AS hh_operational_packaging_cost_status,
    COALESCE(g.hh_internal_packaging_cost, 0::numeric) + COALESCE(g.hh_operational_packaging_cost, 0::numeric) AS total_packaging_cost,
    g.cancelled_value,
    COALESCE(g.cancelled_value_status, 'MISSING_SOURCE') AS cancelled_value_status,
    g.refund_orders,
    g.refund_value,
    COALESCE(g.refund_value_status, 'MISSING_SOURCE') AS refund_value_status,
    -- P8.5 ads-attribution fix: new columns, appended at the end.
    COALESCE(g.ads_spend_status, 'MISSING_SOURCE') AS ads_spend_status,
    g.ads_impressions,
    g.ads_clicks,
    CASE WHEN g.ads_impressions IS NOT NULL AND g.ads_impressions <> 0 AND g.ads_clicks IS NOT NULL THEN round(g.ads_clicks / g.ads_impressions, 4) ELSE NULL::numeric END AS ads_ctr,
    CASE WHEN g.ads_clicks IS NOT NULL AND g.ads_clicks <> 0 AND g.ads_spend IS NOT NULL THEN round(g.ads_spend / g.ads_clicks, 2) ELSE NULL::numeric END AS ads_cpc,
    CASE WHEN g.ads_impressions IS NOT NULL AND g.ads_impressions <> 0 AND g.ads_spend IS NOT NULL THEN round(g.ads_spend / g.ads_impressions * 1000, 2) ELSE NULL::numeric END AS ads_cpm,
    g.ads_orders_broad,
    g.ads_gmv_broad,
    CASE WHEN g.ads_clicks IS NOT NULL AND g.ads_clicks <> 0 AND g.ads_orders_broad IS NOT NULL THEN round(g.ads_orders_broad / g.ads_clicks, 4) ELSE NULL::numeric END AS ads_cvr_broad,
    CASE WHEN g.ads_spend IS NOT NULL AND g.ads_spend <> 0 AND g.ads_gmv_broad IS NOT NULL THEN round(g.ads_gmv_broad / g.ads_spend, 4) ELSE NULL::numeric END AS ads_roas_broad,
    g.ads_orders_direct,
    g.ads_gmv_direct,
    CASE WHEN g.ads_clicks IS NOT NULL AND g.ads_clicks <> 0 AND g.ads_orders_direct IS NOT NULL THEN round(g.ads_orders_direct / g.ads_clicks, 4) ELSE NULL::numeric END AS ads_cvr_direct,
    CASE WHEN g.ads_spend IS NOT NULL AND g.ads_spend <> 0 AND g.ads_gmv_direct IS NOT NULL THEN round(g.ads_gmv_direct / g.ads_spend, 4) ELSE NULL::numeric END AS ads_roas_direct
FROM gold_wide g
    LEFT JOIN shopee_fee sf ON g.channel = 'SHOPEE' AND sf.business_date = g.business_date
    LEFT JOIN tiktok_fee_pop tp ON g.channel = 'TIKTOK' AND tp.business_date = g.business_date
    LEFT JOIN tiktok_completeness tc ON g.channel = 'TIKTOK' AND tc.business_date = g.business_date
    LEFT JOIN incomplete inc ON inc.business_date = g.business_date AND inc.channel = g.channel;
