-- P6C.1 Section 8-9 — permanent metric-ownership contract + freshness view.
-- Additive only: no DROP, no rewrite of existing tables/views.
-- Root-fixes the P6C Gold-overwrite bug: a DB-enforced catalog of which
-- layer (BASE_GOLD vs PNL_ENRICHMENT) owns each metric_name in
-- mart.gold_channel_daily, so a shared write-guard can refuse any script
-- that tries to write a metric it does not own.

CREATE TABLE IF NOT EXISTS mart.gold_metric_ownership (
    metric_name TEXT PRIMARY KEY,
    owner       TEXT NOT NULL CHECK (owner IN ('BASE_GOLD', 'PNL_ENRICHMENT')),
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO mart.gold_metric_ownership (metric_name, owner, notes) VALUES
    ('orders', 'BASE_GOLD', NULL),
    ('units', 'BASE_GOLD', NULL),
    ('platform_gmv', 'BASE_GOLD', NULL),
    ('cancelled_orders', 'BASE_GOLD', NULL),
    ('cancelled_value', 'BASE_GOLD', NULL),
    ('refund_orders', 'BASE_GOLD', NULL),
    ('refund_value', 'BASE_GOLD', NULL),
    ('sellable_cogs', 'BASE_GOLD', NULL),
    ('promo_gift_cost', 'BASE_GOLD', NULL),
    ('packaging_cost', 'BASE_GOLD', NULL),
    ('total_cost', 'BASE_GOLD', NULL),
    ('platform_fees', 'BASE_GOLD', NULL),
    ('payment_fees', 'BASE_GOLD', NULL),
    ('shipping_or_fulfillment_fees', 'BASE_GOLD', NULL),
    ('other_settlement_fees', 'BASE_GOLD', NULL),
    ('settlement_fee_and_tax_total', 'BASE_GOLD', NULL),
    ('settlement_amount_estimated', 'BASE_GOLD', NULL),
    ('ads_spend', 'BASE_GOLD', NULL),
    ('affiliate_commission_estimated', 'BASE_GOLD', NULL),
    ('affiliate_commission_settled', 'BASE_GOLD', NULL),
    ('inventory_units_on_hand', 'BASE_GOLD', NULL),
    ('net_sales', 'PNL_ENRICHMENT', 'P6B.1/P6C.1 — API-actual/DB-derived Net Sales'),
    ('gm1', 'PNL_ENRICHMENT', 'net_sales - sellable_cogs - promo_gift_cost'),
    ('gross_margin', 'PNL_ENRICHMENT', 'legacy P6A placeholder name, retired — never write again'),
    ('variable_platform_fees', 'PNL_ENRICHMENT', 'legacy P6A placeholder name, retired — never write again'),
    ('cm1', 'PNL_ENRICHMENT', 'gm1 - variable platform/payment fees - packaging'),
    ('cm2', 'PNL_ENRICHMENT', 'cm1 - ads/affiliate/booking-KOL-KOC-live, still MISSING_SOURCE'),
    ('profit', 'PNL_ENRICHMENT', 'cm2 - backoffice_cost, still MISSING_SOURCE'),
    ('tiktok_fixed_fee_actual', 'PNL_ENRICHMENT', 'P6B.3 API-actual SKU fee'),
    ('tiktok_payment_fee_actual', 'PNL_ENRICHMENT', 'P6B.3 API-actual SKU fee'),
    ('tiktok_vxp_fee_actual', 'PNL_ENRICHMENT', 'P6B.3 API-actual SKU fee'),
    ('tiktok_infrastructure_fee_actual', 'PNL_ENRICHMENT', 'P6B.3 API-actual SKU fee'),
    ('tiktok_affiliate_fee_actual', 'PNL_ENRICHMENT', 'P6B.3 API-actual SKU fee')
ON CONFLICT (metric_name) DO NOTHING;

-- Per (business_date, channel, shop_id): last-write timestamp for each
-- layer, so staleness of PNL-enrichment relative to a freshly-rebuilt
-- base can be detected mechanically (Section 11-12).
CREATE OR REPLACE VIEW mart.v_gold_build_freshness AS
SELECT
    g.business_date,
    g.channel,
    g.shop_id,
    max(g.updated_at) FILTER (WHERE o.owner = 'BASE_GOLD')       AS base_gold_updated_at,
    max(g.updated_at) FILTER (WHERE o.owner = 'PNL_ENRICHMENT')  AS pnl_enriched_at,
    CASE
        WHEN max(g.updated_at) FILTER (WHERE o.owner = 'PNL_ENRICHMENT') IS NULL THEN 'NOT_YET_ENRICHED'
        WHEN max(g.updated_at) FILTER (WHERE o.owner = 'BASE_GOLD')
             > max(g.updated_at) FILTER (WHERE o.owner = 'PNL_ENRICHMENT') THEN 'PNL_STALE_VS_BASE'
        ELSE 'FRESH'
    END AS pnl_freshness_status
FROM mart.gold_channel_daily g
JOIN mart.gold_metric_ownership o ON o.metric_name = g.metric_name
GROUP BY g.business_date, g.channel, g.shop_id;

GRANT SELECT ON mart.gold_metric_ownership TO hh_etl_writer;
GRANT SELECT ON mart.gold_metric_ownership TO hh_ai_reader;
GRANT SELECT ON mart.v_gold_build_freshness TO hh_etl_writer;
GRANT SELECT ON mart.v_gold_build_freshness TO hh_ai_reader;
