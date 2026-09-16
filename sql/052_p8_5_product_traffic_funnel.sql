-- =====================================================================
-- 052_p8_5_product_traffic_funnel.sql
-- P8.5 — TikTok product-funnel MART exposure (approved 2026-09-15,
-- "P8.5 FINAL FINISH" Section 1).
--
-- Source: core.fact_product_analytics_daily (TikTok only, 555 real
-- rows, 5 real dates: 2026-09-04/08/10/11/14 — sparse, non-contiguous,
-- never fabricated as continuous). Natural grain is already
-- (channel, shop_id, product_id, business_date) — verified unique live,
-- 0 duplicate keys, no aggregation needed in this view.
--
-- Product identity: core.map_platform_product is keyed for this
-- channel overwhelmingly by seller_sku/ean (order-item COGS mapping),
-- NOT by the product-analytics API's platform product_id — live-tested
-- join via identifier_source='platform_product_id' matches only 1 of
-- 121 distinct products. This is a genuine absence of exact mapping,
-- not a bug: per instruction, hh_sku/ean stay NULL rather than
-- fuzzy-matched, and the funnel metric remains usable keyed on the
-- platform's own product_id.
--
-- Additive only — does NOT touch mart.v_ai_product_daily.
-- =====================================================================

CREATE OR REPLACE VIEW mart.v_ai_product_traffic_daily AS
SELECT
    pa.business_date,
    pa.channel,
    pa.shop_id,
    pa.product_id,
    pa.sku_id,
    m.hh_sku,
    m.ean,
    pa.impressions,
    pa.clicks,
    pa.attributed_orders,
    pa.items_sold,
    pa.gmv_amount,
    pa.gmv_currency,
    -- row-level ratios (grain is already 1 product x 1 day; SUM/SUM at
    -- this row-level trivially equals the row's own clicks/impressions)
    CASE WHEN pa.impressions IS NOT NULL AND pa.impressions <> 0
         THEN round(pa.clicks::numeric / pa.impressions, 4) ELSE NULL END AS ctr,
    CASE WHEN pa.clicks IS NOT NULL AND pa.clicks <> 0
         THEN round(pa.attributed_orders::numeric / pa.clicks, 4) ELSE NULL END AS click_to_order,
    'API_ACTUAL' AS status,
    pa.ingested_at AS last_updated_at
FROM core.fact_product_analytics_daily pa
LEFT JOIN core.map_platform_product m
    ON m.channel = pa.channel
   AND m.identifier_source = 'platform_product_id'
   AND m.platform_identifier = pa.product_id;
