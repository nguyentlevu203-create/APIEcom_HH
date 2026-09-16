-- =====================================================================
-- 038_p6c_ceo_ai_views.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P6C CEO daily production view + AI
-- read-only layer.
--
-- Views only (per Section 13's "prefer views over copied snapshot
-- tables"). No new tables, no data copied/duplicated. Reuses:
--   - mart.gold_channel_daily (orders/units/GMV/net_sales/gm1/cm1/COGS —
--     already-proven Gold metrics, P6A/P6B/P6B.1/P6B.3)
--   - core.fact_settlement (Shopee escrow fee breakdown, P6B.1)
--   - core.fact_settlement_sku_fee (TikTok per-SKU actual fees, P6B.3)
--   - core.fact_order / core.fact_order_item / core.dim_product (product
--     day view, inventory)
-- No P&L subtotal is computed here that wasn't already proven in a
-- prior phase — this is a read-surface, not a new calculation.
-- =====================================================================

-- ---------------------------------------------------------------------
-- mart.v_ceo_ecom_daily — grain: business_date, channel, shop_id
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ceo_ecom_daily AS
WITH gold_wide AS (
    SELECT
        business_date, channel, shop_id,
        MAX(metric_value) FILTER (WHERE metric_name='orders') AS orders,
        MAX(metric_value) FILTER (WHERE metric_name='units') AS units,
        MAX(metric_value) FILTER (WHERE metric_name='cancelled_orders') AS cancelled_orders,
        MAX(metric_value) FILTER (WHERE metric_name='platform_gmv') AS platform_gmv,
        MAX(metric_value) FILTER (WHERE metric_name='net_sales') AS net_sales,
        MAX(coverage_status) FILTER (WHERE metric_name='net_sales') AS net_sales_status_raw,
        MAX(metric_value) FILTER (WHERE metric_name='sellable_cogs') AS sellable_cogs,
        MAX(metric_value) FILTER (WHERE metric_name='promo_gift_cost') AS promo_gift_cost,
        MAX(metric_value) FILTER (WHERE metric_name='gm1') AS gm1,
        MAX(coverage_status) FILTER (WHERE metric_name='gm1') AS gm1_status,
        MAX(metric_value) FILTER (WHERE metric_name='cm1') AS cm1,
        MAX(coverage_status) FILTER (WHERE metric_name='cm1') AS cm1_status,
        MAX(metric_value) FILTER (WHERE metric_name='cm2') AS cm2,
        MAX(coverage_status) FILTER (WHERE metric_name='cm2') AS cm2_status,
        MAX(metric_value) FILTER (WHERE metric_name='ads_spend') AS ads_spend,
        MAX(coverage_status) FILTER (WHERE metric_name='ads_spend') AS ads_spend_status,
        MAX(metric_value) FILTER (WHERE metric_name='packaging_cost') AS hh_internal_packaging_cost,
        MAX(metric_value) FILTER (WHERE metric_name='tiktok_fixed_fee_actual') AS tt_fixed_fee,
        MAX(metric_value) FILTER (WHERE metric_name='tiktok_payment_fee_actual') AS tt_payment_fee,
        MAX(metric_value) FILTER (WHERE metric_name='tiktok_vxp_fee_actual') AS tt_vxp_fee,
        MAX(metric_value) FILTER (WHERE metric_name='tiktok_infrastructure_fee_actual') AS tt_infra_fee,
        MAX(metric_value) FILTER (WHERE metric_name='tiktok_affiliate_fee_actual') AS tt_affiliate_fee,
        MAX(metric_value) FILTER (WHERE metric_name='affiliate_commission_estimated') AS tt_affiliate_seller_api,
        MAX(updated_at) AS gold_updated_at
    FROM mart.gold_channel_daily
    GROUP BY 1,2,3
),
shopee_fee AS (
    -- Order-level escrow fee breakdown, order-date joined (same proven
    -- formula as _p6b1_finalize.py's shopee_net_sales_from_db) — kept
    -- live here rather than duplicated into Gold as individual metrics.
    SELECT
        fo.business_date,
        sum(fs.commission_fee) FILTER (WHERE fo.order_status != 'CANCELLED') AS fixed_fee,
        sum(fs.service_fee) FILTER (WHERE fo.order_status != 'CANCELLED') AS service_fee,
        sum(fs.seller_transaction_fee) FILTER (WHERE fo.order_status != 'CANCELLED') AS payment_fee,
        sum(fs.actual_shipping_fee - fs.shopee_shipping_rebate - fs.buyer_paid_shipping_fee
            + fs.seller_return_refund + fs.drc_adjustable_refund + fs.seller_lost_compensation)
            FILTER (WHERE fo.order_status != 'CANCELLED') AS cancel_return_logistics_cost,
        count(*) FILTER (WHERE fo.order_status != 'CANCELLED') AS non_cancelled_orders,
        count(*) FILTER (WHERE fs.order_id IS NOT NULL) AS orders_with_escrow,
        count(*) AS total_orders,
        MAX(fs.ingested_at) AS source_updated_at
    FROM core.fact_order fo
    LEFT JOIN core.fact_settlement fs
      ON fs.channel=fo.channel AND fs.shop_id=fo.shop_id AND fs.order_id=fo.order_id AND fs.settlement_type='escrow_estimate'
    WHERE fo.channel='SHOPEE'
    GROUP BY 1
),
tiktok_fee_pop AS (
    -- population/finality signal for TikTok settlement (proven finality
    -- contract: FINAL only when zero NOT_SETTLED_YET orders that date)
    SELECT
        fo.business_date,
        count(*) AS total_orders,
        count(*) FILTER (WHERE fs.settlement_type='MATCHED') AS matched_orders,
        count(*) FILTER (WHERE fs.settlement_type='NOT_SETTLED_YET') AS not_settled_orders,
        MAX(fs.ingested_at) AS source_updated_at
    FROM core.fact_order fo
    LEFT JOIN core.fact_settlement fs ON fs.channel=fo.channel AND fs.shop_id=fo.shop_id AND fs.order_id=fo.order_id
    WHERE fo.channel='TIKTOK'
    GROUP BY 1
)
SELECT
    g.business_date, g.channel, g.shop_id,
    g.orders, g.units, g.cancelled_orders,
    CASE WHEN g.orders > 0 THEN ROUND((g.cancelled_orders / g.orders)::numeric, 4) ELSE NULL END AS cancellation_rate,
    g.platform_gmv,
    g.net_sales,
    CASE
        WHEN g.channel = 'SHOPEE' THEN g.net_sales_status_raw
        WHEN g.channel = 'TIKTOK' AND tp.not_settled_orders = 0 AND g.net_sales IS NOT NULL THEN 'SETTLEMENT_ACTUAL/FINAL'
        WHEN g.channel = 'TIKTOK' AND g.net_sales IS NOT NULL THEN 'SETTLEMENT_ACTUAL/PARTIAL'
        WHEN g.channel = 'TIKTOK' THEN 'SETTLEMENT_ACTUAL/NOT_SETTLED_YET'
        ELSE g.net_sales_status_raw
    END AS net_sales_status,
    CASE WHEN g.channel='SHOPEE' THEN 'API_ACTUAL/ESTIMATE (Shopee escrow is always "escrow_estimate", never a confirmed final payout in current evidence)'
         ELSE 'SETTLEMENT_ACTUAL' END AS net_sales_basis,
    g.sellable_cogs, g.promo_gift_cost,
    (COALESCE(g.sellable_cogs,0) + COALESCE(g.promo_gift_cost,0)) AS total_cogs,
    g.gm1,
    CASE WHEN g.net_sales IS NOT NULL AND g.net_sales != 0 AND g.gm1 IS NOT NULL
         THEN ROUND((g.gm1 / g.net_sales)::numeric, 4) ELSE NULL END AS gm1_margin,
    g.gm1_status,
    CASE WHEN g.channel='SHOPEE' THEN sf.fixed_fee ELSE g.tt_fixed_fee END AS fixed_fee,
    CASE WHEN g.channel='SHOPEE' THEN sf.service_fee ELSE NULL END AS service_fee,
    CASE WHEN g.channel='SHOPEE' THEN sf.payment_fee ELSE g.tt_payment_fee END AS payment_fee,
    CASE WHEN g.channel='TIKTOK' THEN g.tt_vxp_fee ELSE NULL END AS vxp_fee,
    CASE WHEN g.channel='TIKTOK' THEN g.tt_infra_fee ELSE NULL END AS infrastructure_fee,
    CASE WHEN g.channel='TIKTOK' THEN g.tt_affiliate_fee ELSE NULL END AS affiliate_fee,
    g.ads_spend,
    g.hh_internal_packaging_cost,
    CAST(NULL AS NUMERIC) AS platform_packaging_or_fulfillment_fee,  -- NOT_APPLICABLE both channels — no such API field ever found
    CASE WHEN g.channel='SHOPEE' THEN sf.cancel_return_logistics_cost ELSE NULL END AS cancel_return_logistics_cost,
    g.cm1,
    CASE WHEN g.net_sales IS NOT NULL AND g.net_sales != 0 AND g.cm1 IS NOT NULL
         THEN ROUND((g.cm1 / g.net_sales)::numeric, 4) ELSE NULL END AS cm1_margin,
    g.cm1_status,
    CAST(NULL AS NUMERIC) AS booking_kol_koc,     -- MISSING_SOURCE both channels — no book_rate/creator-mapping master in this project
    CAST(NULL AS NUMERIC) AS live_inhouse_cost,   -- MISSING_SOURCE both channels — no API/approved source identified
    g.cm2,
    CASE WHEN g.net_sales IS NOT NULL AND g.net_sales != 0 AND g.cm2 IS NOT NULL
         THEN ROUND((g.cm2 / g.net_sales)::numeric, 4) ELSE NULL END AS cm2_margin,
    COALESCE(g.cm2_status, 'MISSING_SOURCE') AS cm2_status,
    CAST(NULL AS NUMERIC) AS backoffice_cost,     -- RULE_NOT_APPROVED — no current V0 HH-approved backoffice rate on either channel
    CAST(NULL AS NUMERIC) AS profit,
    CAST(NULL AS NUMERIC) AS profit_margin,
    CASE WHEN g.cm2 IS NULL THEN COALESCE(g.cm2_status, 'MISSING_SOURCE') ELSE 'RULE_NOT_APPROVED' END AS profit_status,
    GREATEST(g.gold_updated_at,
             CASE WHEN g.channel='SHOPEE' THEN sf.source_updated_at ELSE tp.source_updated_at END) AS source_updated_at,
    CASE
        WHEN g.channel='TIKTOK' AND tp.not_settled_orders > 0 THEN 'SOURCE_LATENCY'
        WHEN g.net_sales IS NULL THEN 'STALE'
        ELSE 'FRESH'
    END AS data_freshness_status
FROM gold_wide g
LEFT JOIN shopee_fee sf ON g.channel='SHOPEE' AND sf.business_date=g.business_date
LEFT JOIN tiktok_fee_pop tp ON g.channel='TIKTOK' AND tp.business_date=g.business_date;

COMMENT ON VIEW mart.v_ceo_ecom_daily IS
'P6C CEO daily production view — Shopee+TikTok only. Every downstream subtotal (gm1/cm1/cm2/profit) is NULL, never a fabricated 0, whenever a required upstream input is missing — see the paired _status column.';

-- ---------------------------------------------------------------------
-- AI READ LAYER — hh_ai_reader gets SELECT on these views ONLY, never
-- on the underlying core.* tables directly (views run with the owner's
-- privileges by default in Postgres, so hh_ai_reader needs no grant on
-- core.* at all to use them).
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW mart.v_ai_channel_daily AS
SELECT business_date, channel, orders, units, cancelled_orders, cancellation_rate,
       platform_gmv, data_freshness_status, source_updated_at
FROM mart.v_ceo_ecom_daily;

CREATE OR REPLACE VIEW mart.v_ai_pnl_daily AS
SELECT business_date, channel,
       net_sales, net_sales_status, net_sales_basis,
       sellable_cogs, promo_gift_cost, total_cogs,
       gm1, gm1_margin, gm1_status,
       fixed_fee, service_fee, payment_fee, vxp_fee, infrastructure_fee, affiliate_fee, ads_spend,
       hh_internal_packaging_cost, platform_packaging_or_fulfillment_fee, cancel_return_logistics_cost,
       cm1, cm1_margin, cm1_status,
       booking_kol_koc, live_inhouse_cost,
       cm2, cm2_margin, cm2_status,
       backoffice_cost, profit, profit_margin, profit_status,
       source_updated_at, data_freshness_status
FROM mart.v_ceo_ecom_daily;

-- NOTE: run 039_p6c_product_role.sql (adds core.dim_product.default_transaction_role,
-- backfilled from the same approved_master().default_role already used everywhere
-- else — see artifacts/v0's one-time UPDATE) BEFORE this view, or PACKAGING_AUXILIARY-
-- typed items and PROMO_GIFT-by-default items like 'BONG TAM HQ' (master_product_type
-- ='PRODUCT', but default_role='PROMO_GIFT' per the 3-layer P5A.3 model) will be
-- misclassified as SALE.
CREATE OR REPLACE VIEW mart.v_ai_product_daily AS
WITH role AS (
    -- The ONE known channel-dependent role override in the entire
    -- HH-approved master (P5B): '1 BÁNH-XP' is PROMO_GIFT on Shopee,
    -- SALE on TikTok (its own default_role). Every other SKU's role
    -- comes straight from dim_product.default_transaction_role.
    SELECT
        foi.channel, foi.business_date, foi.sku,
        CASE
            WHEN foi.sku = '1 BÁNH-XP' AND foi.channel = 'SHOPEE' THEN 'PROMO_GIFT'
            ELSE COALESCE(dp2.default_transaction_role, 'SALE')
        END AS transaction_role,
        foi.qty, foi.order_id
    FROM core.fact_order_item foi
    LEFT JOIN core.map_platform_product mpp ON mpp.channel=foi.channel AND mpp.shop_id=foi.shop_id AND mpp.platform_identifier=foi.sku
    LEFT JOIN core.dim_product dp2 ON dp2.sku = COALESCE(mpp.hh_sku, foi.sku)
    WHERE foi.sku IS NOT NULL AND foi.sku != ''
)
SELECT
    r.business_date, r.channel,
    COALESCE(mpp.hh_sku, r.sku) AS hh_product_id,
    COALESCE(mpp.hh_sku, r.sku) AS hh_sku,
    dp.barcode AS ean,
    dp.product_name,
    SUM(r.qty) FILTER (WHERE r.transaction_role='SALE') AS sold_units,
    SUM(r.qty) FILTER (WHERE r.transaction_role IN ('PROMO_GIFT','PACKAGING')) AS gift_units,
    SUM(r.qty) AS total_units,
    CAST(NULL AS NUMERIC) AS platform_gmv,  -- not attributable per-SKU without an approved order-level-fee allocation contract (none exists) — left NULL, never estimated
    CAST(NULL AS NUMERIC) AS sellable_cogs, -- per-SKU COGS requires the full P5 compute engine (combo BOM resolution) — not re-derivable in a plain SQL view without duplicating that logic; see note in P6C report
    CAST(NULL AS NUMERIC) AS promo_gift_cost,
    COUNT(DISTINCT r.order_id) AS order_count
FROM role r
LEFT JOIN core.map_platform_product mpp ON mpp.channel=r.channel AND mpp.platform_identifier=r.sku
LEFT JOIN core.dim_product dp ON dp.sku = COALESCE(mpp.hh_sku, r.sku)
GROUP BY 1,2,3,4,5,6;

COMMENT ON VIEW mart.v_ai_product_daily IS
'sellable_cogs/promo_gift_cost/platform_gmv are intentionally NULL here — combo BOM cost resolution and fee allocation both require the full P5/P6 Python compute engine, not reproducible as a plain SQL aggregate without risking a silently-wrong number. Use sold_units/gift_units/order_count for now; ask for a per-SKU COGS export via the P5 engine if $ granularity is needed.';

CREATE OR REPLACE VIEW mart.v_ai_inventory AS
SELECT
    fis.business_date AS snapshot_date, fis.warehouse,
    COALESCE(mpp.hh_sku, fis.sku) AS hh_product_id,
    COALESCE(mpp.hh_sku, fis.sku) AS hh_sku,
    dp.barcode AS ean, dp.product_name,
    CAST(NULL AS TEXT) AS lot,             -- not tracked anywhere in current production (no lot-level source)
    CAST(NULL AS DATE) AS expiry_date,     -- not tracked anywhere in current production
    fis.qty_available AS stock_qty,
    'SINGLE_SNAPSHOT_ONLY_NOT_DAILY_PRODUCTION_READY' AS data_status
FROM core.fact_inventory_snapshot fis
LEFT JOIN core.map_platform_product mpp ON mpp.channel=fis.channel AND mpp.platform_identifier=fis.sku
LEFT JOIN core.dim_product dp ON dp.sku = COALESCE(mpp.hh_sku, fis.sku);

COMMENT ON VIEW mart.v_ai_inventory IS
'core.fact_inventory_snapshot has only ONE snapshot date (2026-09-11) as of P6C — this is a point-in-time view, not a daily-refreshed production feed. data_status flags this on every row. Do not treat absence of a SKU here as zero stock.';

CREATE OR REPLACE VIEW mart.v_ai_metric_status AS
SELECT
    g.metric_name, g.channel, g.business_date,
    g.coverage_status AS availability_status,
    COALESCE(c.value_basis, 'UNKNOWN') AS value_basis,
    g.updated_at AS last_source_update,
    c.approved_rule_source AS note
FROM mart.gold_channel_daily g
LEFT JOIN mart.gold_metric_contract c ON c.metric_name = g.metric_name AND c.channel = g.channel AND c.is_active;

COMMENT ON VIEW mart.v_ai_metric_status IS
'value_basis/note are UNKNOWN/NULL for Gold metrics that have no matching row in mart.gold_metric_contract yet (naming has not been fully unified across P6A/P6B/P6B.3) — availability_status (from gold_channel_daily.coverage_status) is always populated and authoritative.';

