-- =====================================================================
-- 047_p8_5_source_coverage_shop_traffic.sql
-- P8.5 Section 2 — add TIKTOK shop_traffic to mart.v_ai_source_coverage.
-- Additive only: one more domain tuple + one more UNION ALL branch
-- reading core.fact_shop_traffic_daily. No change to existing domains'
-- logic or status thresholds.
-- =====================================================================

CREATE OR REPLACE VIEW mart.v_ai_source_coverage AS
WITH domains AS (
    SELECT d.source_system, d.source_endpoint
    FROM (VALUES
        ('SHOPEE','orders'), ('SHOPEE','returns'), ('SHOPEE','finance'), ('SHOPEE','ads'),
        ('SHOPEE','product_inventory'), ('SHOPEE','affiliate_ams'),
        ('TIKTOK','orders'), ('TIKTOK','returns'), ('TIKTOK','finance'), ('TIKTOK','affiliate'),
        ('TIKTOK','product_analytics'), ('TIKTOK','live'), ('TIKTOK','product_inventory'),
        ('TIKTOK','shop_traffic')
    ) d(source_system, source_endpoint)
), domain_dates AS (
    SELECT 'SHOPEE' AS source_system, 'orders' AS source_endpoint, business_date FROM core.fact_order WHERE channel='SHOPEE'
    UNION ALL SELECT 'TIKTOK','orders', business_date FROM core.fact_order WHERE channel='TIKTOK'
    UNION ALL SELECT 'SHOPEE','returns', business_date FROM core.fact_return_refund WHERE channel='SHOPEE'
    UNION ALL SELECT 'TIKTOK','returns', business_date FROM core.fact_return_refund WHERE channel='TIKTOK'
    UNION ALL SELECT 'SHOPEE','finance', business_date FROM core.fact_settlement WHERE channel='SHOPEE'
    UNION ALL SELECT 'TIKTOK','finance', business_date FROM core.fact_settlement WHERE channel='TIKTOK'
    UNION ALL SELECT 'SHOPEE','ads', business_date FROM core.fact_ads_daily WHERE channel='SHOPEE'
    UNION ALL SELECT 'TIKTOK','affiliate', business_date FROM core.fact_affiliate_daily WHERE channel='TIKTOK'
    UNION ALL SELECT 'TIKTOK','product_analytics', business_date FROM core.fact_product_analytics_daily WHERE channel='TIKTOK'
    UNION ALL SELECT 'TIKTOK','live', business_date FROM core.fact_live_daily WHERE channel='TIKTOK'
    UNION ALL SELECT 'SHOPEE','affiliate_ams', business_date FROM core.fact_shopee_affiliate_conversion
    UNION ALL SELECT 'TIKTOK','shop_traffic', business_date FROM core.fact_shop_traffic_daily WHERE channel='TIKTOK'
), coverage AS (
    SELECT source_system, source_endpoint,
           min(business_date) AS date_from_all_time,
           max(business_date) AS latest_db_date,
           count(DISTINCT business_date) FILTER (WHERE business_date >= (CURRENT_DATE - INTERVAL '29 days')) AS days_present_30d
    FROM domain_dates GROUP BY source_system, source_endpoint
)
SELECT dm.source_system AS platform,
    dm.source_endpoint AS source_name,
    dm.source_endpoint AS metric_name,
    c.date_from_all_time AS date_from, CURRENT_DATE AS date_to,
    CURRENT_DATE AS latest_source_date_expected,
    c.latest_db_date,
    round(100.0 * COALESCE(c.days_present_30d,0)::numeric/30.0,1) AS coverage_pct_30d,
    CASE
        WHEN s.source_endpoint IS NULL AND dm.source_endpoint='affiliate_ams' THEN 'NOT_SCHEDULED'
        WHEN dm.source_endpoint='product_inventory' AND s.status='success' AND (CURRENT_DATE - s.last_synced_at::date) <= 1 THEN 'CURRENT'
        WHEN dm.source_endpoint='product_inventory' AND s.status='success' THEN 'SOURCE_LAGGING'
        WHEN dm.source_endpoint='product_inventory' THEN 'NO_DATA'
        WHEN c.latest_db_date IS NULL THEN 'NO_DATA'
        WHEN s.status IS NOT NULL AND s.status <> 'success' THEN 'LAST_RUN_FAILED'
        WHEN (CURRENT_DATE - c.latest_db_date) <= 1 THEN 'CURRENT'
        WHEN (CURRENT_DATE - c.latest_db_date) <= 3 THEN 'SOURCE_LAGGING'
        ELSE 'STALE'
    END AS coverage_status,
    s.last_synced_at AS last_success_at,
    30 - COALESCE(c.days_present_30d,0) AS missing_days_30d
FROM domains dm
LEFT JOIN coverage c ON c.source_system=dm.source_system AND c.source_endpoint=dm.source_endpoint
LEFT JOIN control.etl_sync_state s ON s.source_system=dm.source_system AND s.source_endpoint=dm.source_endpoint
ORDER BY dm.source_system, dm.source_endpoint;
