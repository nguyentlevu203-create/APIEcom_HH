-- P6C4 — Shopee AMS Affiliate ingestion schema. Additive only: no DROP,
-- no rewrite of any existing table/view. Three new fact tables (raw
-- source evidence, traceable per Section 5) + ownership-catalog rows for
-- the new Gold metric names + 3 new AI read views.

-- 1. Order-item level, "order-level-actual" commission (get_conversion_report)
CREATE TABLE IF NOT EXISTS core.fact_shopee_affiliate_conversion (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'SHOPEE',
    shop_id TEXT NOT NULL,
    order_id TEXT NOT NULL,               -- = AMS order_sn, joins core.fact_order.order_id
    item_id TEXT,
    model_id TEXT,
    order_status TEXT,
    verified_status TEXT,
    affiliate_id TEXT,
    affiliate_name TEXT,
    affiliate_username TEXT,
    ams_channel TEXT,                     -- AMS's own "channel" field (e.g. Shopeevideo-Shopee) -- NOT platform channel
    order_type TEXT,
    buyer_status TEXT,
    campaign_id TEXT,                     -- items[].attr_campaign_id
    seller_campaign_type TEXT,
    promotion_id TEXT,
    item_name TEXT,
    price NUMERIC(24,6),
    qty NUMERIC(18,4),
    purchase_value NUMERIC(24,6),
    refund_amount NUMERIC(24,6),
    order_brand_commission NUMERIC(24,6),
    item_brand_commission NUMERIC(24,6),
    item_brand_commission_rate_to_affiliate TEXT,
    item_brand_commission_to_affiliate NUMERIC(24,6),
    item_brand_commission_rate_to_mcn TEXT,
    item_brand_commission_to_mcn NUMERIC(24,6),
    seller_service_fee_rate TEXT,
    seller_service_fee NUMERIC(24,6),
    place_order_time TIMESTAMPTZ,
    order_completed_time TIMESTAMPTZ,
    conversion_completed_time TIMESTAMPTZ,
    commission_value_basis TEXT NOT NULL DEFAULT 'ORDER_LEVEL_ACTUAL',
    source_updated_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id UUID
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fsac_natural_key
    ON core.fact_shopee_affiliate_conversion (channel, shop_id, order_id, COALESCE(item_id,''), COALESCE(model_id,''));

-- 2. Same grain, FINAL_BILLED (get_validation_report) — kept as a SEPARATE
-- table (not overwriting #1) so a provisional ORDER_LEVEL_ACTUAL row is
-- never destructively replaced when the final bill lands.
CREATE TABLE IF NOT EXISTS core.fact_shopee_affiliate_validation (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'SHOPEE',
    shop_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    item_id TEXT,
    model_id TEXT,
    order_status TEXT,
    verified_status TEXT,
    affiliate_id TEXT,
    affiliate_name TEXT,
    affiliate_username TEXT,
    ams_channel TEXT,
    order_type TEXT,
    buyer_status TEXT,
    campaign_id TEXT,
    seller_campaign_type TEXT,
    promotion_id TEXT,
    item_name TEXT,
    price NUMERIC(24,6),
    qty NUMERIC(18,4),
    purchase_value NUMERIC(24,6),
    refund_amount NUMERIC(24,6),
    order_brand_commission NUMERIC(24,6),
    item_brand_commission NUMERIC(24,6),
    item_brand_commission_rate_to_affiliate TEXT,
    item_brand_commission_to_affiliate NUMERIC(24,6),
    item_brand_commission_rate_to_mcn TEXT,
    item_brand_commission_to_mcn NUMERIC(24,6),
    seller_service_fee_rate TEXT,
    seller_service_fee NUMERIC(24,6),
    place_order_time TIMESTAMPTZ,
    order_completed_time TIMESTAMPTZ,
    conversion_completed_time TIMESTAMPTZ,
    validation_id TEXT,
    validation_month INTEGER,
    ams_deduction_time TIMESTAMPTZ,
    commission_value_basis TEXT NOT NULL DEFAULT 'FINAL_BILLED',
    source_updated_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id UUID
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fsav_natural_key
    ON core.fact_shopee_affiliate_validation (channel, shop_id, order_id, COALESCE(item_id,''), COALESCE(model_id,''), validation_id);

-- 3. Daily performance-tier aggregates (get_shop_performance /
-- get_affiliate_performance / get_product_performance) — ESTIMATED_PERFORMANCE
-- basis only, never used to override order-level/final-billed values.
-- One table, 3 grains (grain_type discriminator) to avoid 3 near-identical tables.
CREATE TABLE IF NOT EXISTS core.fact_shopee_affiliate_performance_daily (
    id BIGSERIAL PRIMARY KEY,
    grain_type TEXT NOT NULL CHECK (grain_type IN ('SHOP','AFFILIATE','PRODUCT')),
    business_date DATE NOT NULL,
    channel TEXT NOT NULL DEFAULT 'SHOPEE',
    shop_id TEXT NOT NULL,
    affiliate_id TEXT,
    affiliate_name TEXT,
    affiliate_username TEXT,
    item_id TEXT,
    item_name TEXT,
    sales NUMERIC(24,6),
    items_sold NUMERIC(18,4),
    orders INTEGER,
    clicks INTEGER,
    est_commission NUMERIC(24,6),
    roi NUMERIC(10,4),
    total_buyers INTEGER,
    new_buyers INTEGER,
    value_basis TEXT NOT NULL DEFAULT 'ESTIMATED_PERFORMANCE',
    source_updated_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id UUID
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fsapd_natural_key
    ON core.fact_shopee_affiliate_performance_daily (grain_type, business_date, channel, shop_id, COALESCE(affiliate_id,''), COALESCE(item_id,''));

GRANT SELECT, INSERT, UPDATE ON core.fact_shopee_affiliate_conversion TO hh_etl_writer;
GRANT SELECT, INSERT, UPDATE ON core.fact_shopee_affiliate_validation TO hh_etl_writer;
GRANT SELECT, INSERT, UPDATE ON core.fact_shopee_affiliate_performance_daily TO hh_etl_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO hh_etl_writer;

-- 4. Gold metric ownership: affiliate volume/traffic facts are BASE_GOLD;
-- affiliate commission (feeds CM2) is PNL_ENRICHMENT.
INSERT INTO mart.gold_metric_ownership (metric_name, owner, notes) VALUES
    ('affiliate_gmv', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance sales'),
    ('affiliate_orders', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance orders'),
    ('affiliate_units', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance gross_item_sold'),
    ('affiliate_clicks', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance clicks'),
    ('affiliate_creator_count', 'BASE_GOLD', 'P6C4 — distinct affiliate_id count, AMS affiliate-performance'),
    ('affiliate_new_buyers', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance new_buyers'),
    ('affiliate_total_buyers', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance total_buyers'),
    ('affiliate_roi', 'BASE_GOLD', 'P6C4 — Shopee AMS shop-performance roi (Shopee-computed)'),
    ('affiliate_commission', 'PNL_ENRICHMENT', 'P6C4 — feeds Shopee CM2; value_basis precedence FINAL_BILLED > ORDER_LEVEL_ACTUAL > ESTIMATED_PERFORMANCE'),
    ('affiliate_commission_basis', 'PNL_ENRICHMENT', 'P6C4 — text label of which value_basis is currently in affiliate_commission')
ON CONFLICT (metric_name) DO NOTHING;

-- 5. AI read views (SELECT-only, hh_ai_reader).
CREATE OR REPLACE VIEW mart.v_ai_affiliate_daily AS
SELECT
    g.business_date, g.channel, g.shop_id,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_gmv')            AS affiliate_gmv,
    max(g.coverage_status) FILTER (WHERE g.metric_name='affiliate_gmv')         AS affiliate_gmv_status,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_orders')         AS affiliate_orders,
    max(g.coverage_status) FILTER (WHERE g.metric_name='affiliate_orders')      AS affiliate_orders_status,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_units')          AS affiliate_units,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_clicks')         AS affiliate_clicks,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_commission')     AS affiliate_commission,
    max(g.coverage_status) FILTER (WHERE g.metric_name='affiliate_commission')  AS affiliate_commission_status,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_commission_basis') AS affiliate_commission_basis_code,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_roi')            AS affiliate_roi,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_creator_count')  AS affiliate_creator_count,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_new_buyers')     AS affiliate_new_buyers,
    max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_total_buyers')   AS affiliate_total_buyers,
    CASE
        WHEN max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_gmv') IS NOT NULL
             AND max(g.metric_value) FILTER (WHERE g.metric_name='platform_gmv') IS NOT NULL
        THEN round(max(g.metric_value) FILTER (WHERE g.metric_name='affiliate_gmv')
                   / NULLIF(max(g.metric_value) FILTER (WHERE g.metric_name='platform_gmv'), 0), 4)
        ELSE NULL
    END AS affiliate_gmv_pct_of_platform_gmv
FROM mart.gold_channel_daily g
WHERE g.channel = 'SHOPEE'
GROUP BY g.business_date, g.channel, g.shop_id;

CREATE OR REPLACE VIEW mart.v_ai_affiliate_creator_daily AS
SELECT business_date, channel, shop_id, affiliate_id, affiliate_name, affiliate_username,
       sales, items_sold, orders, clicks, est_commission, roi, total_buyers, new_buyers,
       value_basis, source_updated_at
FROM core.fact_shopee_affiliate_performance_daily
WHERE grain_type = 'AFFILIATE';

CREATE OR REPLACE VIEW mart.v_ai_affiliate_product_daily AS
SELECT business_date, channel, shop_id, item_id, item_name,
       sales, items_sold, orders, clicks, est_commission, roi, total_buyers, new_buyers,
       value_basis, source_updated_at
FROM core.fact_shopee_affiliate_performance_daily
WHERE grain_type = 'PRODUCT';

GRANT SELECT ON mart.v_ai_affiliate_daily TO hh_ai_reader;
GRANT SELECT ON mart.v_ai_affiliate_creator_daily TO hh_ai_reader;
GRANT SELECT ON mart.v_ai_affiliate_product_daily TO hh_ai_reader;
GRANT SELECT ON mart.v_ai_affiliate_daily TO hh_etl_writer;
GRANT SELECT ON mart.v_ai_affiliate_creator_daily TO hh_etl_writer;
GRANT SELECT ON mart.v_ai_affiliate_product_daily TO hh_etl_writer;
