-- =====================================================================
-- 049_p8_5_source_coverage_semantics_fix.sql
-- P8.5 Sections 1-2 — fix real semantic defects in mart.v_ai_source_coverage,
-- found by direct query against the live view:
--
-- BUG 1: product_inventory (both channels) showed latest_db_date=NULL,
-- coverage_pct=0%, missing_days=30, yet coverage_status=CURRENT. Root
-- cause: product_inventory was never added to domain_dates (it's a
-- snapshot source, not an order-history source) — this migration adds
-- it, sourced from core.fact_inventory_snapshot.business_date, so real
-- freshness is now actually measured, not special-cased blind.
--
-- BUG 2: SHOPEE returns showed NO_DATA (implying "never attempted")
-- despite control.etl_run_log showing repeated real, recent API
-- failures ("error_data - Inner error, please try later. [5]"). This
-- migration adds a real API_ERROR detection: a source with a FAILED
-- run in the last 3 days and zero real data is now API_ERROR, not
-- NO_DATA (which is reserved for a source never attempted / genuinely
-- empty with no failure evidence).
--
-- BUG 3 (30-day denominator): a newly-onboarded source (e.g. Shopee
-- orders, first seen 2026-08-23, only 23 days ago) was scored against
-- a flat 30-day denominator, understating true coverage. Now uses
-- eligible_days = days between GREATEST(date_from, CURRENT_DATE-29)
-- and CURRENT_DATE inclusive — i.e. never penalizes a source for days
-- before it existed. The raw 30-calendar-day figure is kept alongside
-- as coverage_pct_30d_calendar for reference, per the instruction to
-- preserve it, but health classification now uses the eligible-window
-- figure.
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
    -- BUG 1 fix: product_inventory now measured for real, both channels
    UNION ALL SELECT 'SHOPEE','product_inventory', business_date FROM core.fact_inventory_snapshot WHERE channel='SHOPEE'
    UNION ALL SELECT 'TIKTOK','product_inventory', business_date FROM core.fact_inventory_snapshot WHERE channel='TIKTOK'
), coverage AS (
    SELECT source_system, source_endpoint,
           min(business_date) AS date_from_all_time,
           max(business_date) AS latest_db_date,
           count(DISTINCT business_date) FILTER (WHERE business_date >= ((CURRENT_DATE - 29))) AS days_present_30d
    FROM domain_dates GROUP BY source_system, source_endpoint
), recent_run AS (
    -- BUG 2 fix: real API-error evidence, last 3 days, most recent run per domain
    SELECT DISTINCT ON (source_system, source_endpoint)
        source_system, source_endpoint, status, error_message, started_at
    FROM control.etl_run_log
    WHERE started_at >= (now() - INTERVAL '3 days')
    ORDER BY source_system, source_endpoint, started_at DESC
), last_success AS (
    SELECT source_system, source_endpoint, max(started_at) AS last_success_started_at
    FROM control.etl_run_log WHERE status='success' GROUP BY 1,2
)
SELECT dm.source_system AS platform,
    dm.source_endpoint AS source_name,
    dm.source_endpoint AS metric_name,
    c.date_from_all_time AS date_from, CURRENT_DATE AS date_to,
    CURRENT_DATE AS latest_source_date_expected,
    c.latest_db_date,
    -- eligible-window coverage (BUG 3 fix): denominator = days since the
    -- LATER of (source first seen) or (30 days ago), never penalizing a
    -- source for days before it was onboarded.
    round(100.0 * COALESCE(c.days_present_30d,0)::numeric /
          GREATEST(1, CURRENT_DATE - GREATEST(COALESCE(c.date_from_all_time, (CURRENT_DATE - 29))::date, (CURRENT_DATE - 29)) + 1)::numeric, 1) AS coverage_pct_30d,
    CASE
        WHEN s.source_endpoint IS NULL AND dm.source_endpoint='affiliate_ams' THEN 'NOT_SCHEDULED'
        -- BUG 2 fix: real, recent, failed run with zero data -> API_ERROR, never NO_DATA
        WHEN c.latest_db_date IS NULL AND rr.status = 'fail' THEN 'API_ERROR'
        WHEN dm.source_endpoint='product_inventory' AND c.latest_db_date IS NOT NULL AND (CURRENT_DATE - c.latest_db_date) <= 1 THEN 'CURRENT'
        WHEN dm.source_endpoint='product_inventory' AND c.latest_db_date IS NOT NULL THEN 'SOURCE_LAGGING'
        WHEN dm.source_endpoint='product_inventory' THEN 'NO_DATA'
        WHEN c.latest_db_date IS NULL THEN 'NO_DATA'
        WHEN s.status IS NOT NULL AND s.status <> 'success' THEN 'LAST_RUN_FAILED'
        WHEN (CURRENT_DATE - c.latest_db_date) <= 1 THEN 'CURRENT'
        WHEN (CURRENT_DATE - c.latest_db_date) <= 3 THEN 'SOURCE_LAGGING'
        ELSE 'STALE'
    END AS coverage_status,
    s.last_synced_at AS last_success_at,
    30 - COALESCE(c.days_present_30d,0) AS missing_days_30d,
    -- new columns appended at the end (CREATE OR REPLACE VIEW cannot
    -- reorder/insert columns mid-list)
    GREATEST(1, CURRENT_DATE - GREATEST(COALESCE(c.date_from_all_time, (CURRENT_DATE - 29))::date, (CURRENT_DATE - 29)) + 1) AS eligible_days_30d,
    round(100.0 * COALESCE(c.days_present_30d,0)::numeric/30.0,1) AS coverage_pct_30d_calendar,
    rr.started_at AS last_attempt_at,
    rr.error_message AS last_error_message
FROM domains dm
LEFT JOIN coverage c ON c.source_system=dm.source_system AND c.source_endpoint=dm.source_endpoint
LEFT JOIN control.etl_sync_state s ON s.source_system=dm.source_system AND s.source_endpoint=dm.source_endpoint
LEFT JOIN recent_run rr ON rr.source_system=dm.source_system AND rr.source_endpoint=dm.source_endpoint
ORDER BY dm.source_system, dm.source_endpoint;
