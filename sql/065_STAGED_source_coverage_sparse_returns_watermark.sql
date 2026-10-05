-- =====================================================================
-- 065_STAGED_source_coverage_sparse_returns_watermark.sql
-- P11 RECOVERY D — TIKTOK.returns freshness = operational (watermark/run)
-- freshness, not customer-event freshness.
--
-- Proven live (run 37262150230, 2026-10-05): TIKTOK/returns incremental
-- succeeded at 04:37 UTC (watermark current), but the shop simply had no
-- customer return after 2026-10-01 (16/30 days with any return), so the
-- date-based rule (CURRENT_DATE - latest_db_date) > 3 reported STALE and
-- the REQUIRED-source healthcheck went RED. A recent successful empty
-- poll is valid evidence of freshness for a sparse event source.
--
-- Changes vs 064 (column list and status vocabulary unchanged):
--   * new CTE sparse_sources: ('TIKTOK','returns') only
--   * sparse sources skip the latest_db_date > 3 days -> STALE rule;
--     latest_db_date stays in the output as informational event freshness
--   * sparse source with zero events all-time: NO_DATA only without a
--     watermark; a recent failed run with no data stays API_ERROR (064
--     precedence); otherwise the watermark/run rules below apply
--   * sparse source whose latest incremental run failed -> LAST_RUN_FAILED
--     (existing value; never CURRENT without a successful poll)
--   * sparse source with a watermark <= 24h and no PARTIAL_CATCHUP note
--     -> CURRENT (requires a watermark row)
--   * watermark > 72h -> STALE and > 24h -> SOURCE_LAGGING apply as in 064
--   Dense sources (orders, finance, ...) are evaluated exactly as in 064.
--   scripts/production_healthcheck.py REQUIRED/OPTIONAL map is unchanged:
--   TIKTOK.returns stays REQUIRED.
--
-- STAGED: not applied. Applying is a production DDL change and needs
-- explicit approval. Rollback = re-run sql/064 (the live definition).
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
), sparse_sources AS (
    -- P11 RECOVERY D: event sources where days without any event are
    -- normal; freshness comes from the incremental watermark/run.
    SELECT v.source_system, v.source_endpoint
    FROM (VALUES ('TIKTOK','returns')) v(source_system, source_endpoint)
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
), last_incremental AS (
    -- P11-FIX-4: most recent INCREMENTAL run only. recent_run above also
    -- sees reconciliation runs, which run after ingestion and would hide
    -- an incremental PARTIAL_CATCHUP note.
    SELECT DISTINCT ON (source_system, source_endpoint)
        source_system, source_endpoint, status, error_message, started_at
    FROM control.etl_run_log
    WHERE run_type = 'incremental'
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
        -- P11 RECOVERY D (review fix): a sparse source with zero events
        -- all-time is NO_DATA only when it has no watermark either; with a
        -- watermark it falls through to the watermark/run rules below.
        WHEN c.latest_db_date IS NULL AND (sp.source_endpoint IS NULL OR s.last_synced_at IS NULL) THEN 'NO_DATA'
        WHEN s.status IS NOT NULL AND s.status <> 'success' THEN 'LAST_RUN_FAILED'
        WHEN sp.source_endpoint IS NULL AND (CURRENT_DATE - c.latest_db_date) > 3 THEN 'STALE'
        -- P11-FIX-4: the incremental watermark must be current too, not
        -- just the newest business_date. Reconciliation fills D-1 rows
        -- without moving control.etl_sync_state, so latest_db_date alone
        -- reported TIKTOK.orders CURRENT while its incremental backlog
        -- was still ~2.4 days behind (run 35948968552). affiliate_ams is
        -- excluded: its watermark is day-grain (end of last published
        -- day) and lags by design; latest_db_date already covers it.
        WHEN dm.source_endpoint <> 'affiliate_ams' AND s.last_synced_at < now() - INTERVAL '72 hours' THEN 'STALE'
        -- P11 RECOVERY D: a sparse source has no event-date evidence of
        -- freshness, so its latest incremental run must have succeeded.
        WHEN sp.source_endpoint IS NOT NULL AND li.status = 'fail' THEN 'LAST_RUN_FAILED'
        WHEN li.status = 'success' AND li.error_message LIKE 'PARTIAL_CATCHUP%' THEN 'SOURCE_LAGGING'
        WHEN dm.source_endpoint <> 'affiliate_ams' AND s.last_synced_at < now() - INTERVAL '24 hours' THEN 'SOURCE_LAGGING'
        -- P11 RECOVERY D: sparse source, successful poll within 24h ->
        -- CURRENT even if the newest customer event is older.
        WHEN sp.source_endpoint IS NOT NULL AND s.last_synced_at IS NOT NULL THEN 'CURRENT'
        WHEN (CURRENT_DATE - c.latest_db_date) <= 1 THEN 'CURRENT'
        ELSE 'SOURCE_LAGGING'
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
LEFT JOIN last_incremental li ON li.source_system=dm.source_system AND li.source_endpoint=dm.source_endpoint
LEFT JOIN sparse_sources sp ON sp.source_system=dm.source_system AND sp.source_endpoint=dm.source_endpoint
ORDER BY dm.source_system, dm.source_endpoint;
