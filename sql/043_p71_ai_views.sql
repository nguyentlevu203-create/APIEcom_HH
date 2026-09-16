-- =============================================================================
-- P7.1 — AI-facing views for the new Platform KPI tables.
-- All additive: CREATE VIEW + GRANT SELECT to hh_ai_reader on the views
-- only (P7 security model unchanged — zero grants on core.* for the AI
-- role). Not applied yet; drafted for review alongside the crosswalk.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- mart.v_ai_video_daily — TikTok video content performance.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_video_daily AS
SELECT
    channel, shop_id, video_id, business_date, account_type,
    creator_open_id, creator_username, creator_nick_name, creator_author_type,
    title, video_post_time, duration_seconds, views, avg_customers,
    click_through_rate, sku_orders, items_sold, gmv_amount, gmv_currency,
    gpm_amount, gpm_currency, value_basis, ingested_at AS last_updated_at
FROM core.fact_tiktok_video_daily;
-- account_type: 'ALL' = every video; 'AFFILIATE_ACCOUNTS' = affiliate-posted
-- videos only, a SUBSET of 'ALL' for the same video_id — never sum both.

-- -----------------------------------------------------------------------------
-- mart.v_ai_live_daily — TikTok LIVE session performance.
-- account_type is ALWAYS a visible column. 'ALL' is the authoritative total
-- LIVE reporting slice; 'AFFILIATE_ACCOUNTS' is a drilldown SUBSET of the
-- same live_id, never additive. This view performs NO aggregation across
-- account_type — every consumer must filter explicitly
-- (WHERE account_type = 'ALL' for totals).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_live_daily AS
SELECT
    channel, shop_id, live_id, business_date, account_type,
    title, username, start_time, end_time, duration_seconds,
    viewers, views, product_impressions, product_clicks, sku_orders,
    customers, items_sold, likes, comments, shares, new_followers,
    avg_viewing_duration_secs, click_through_rate, click_to_order_rate,
    gmv_amount, gmv_currency, latency_status, value_basis, ingested_at AS last_updated_at
FROM core.fact_live_daily;

-- -----------------------------------------------------------------------------
-- mart.v_ai_live_product_daily — TikTok LIVE per-product performance.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_live_product_daily AS
SELECT
    channel, shop_id, live_id, product_id, business_date, product_name,
    avg_price_amount, avg_price_currency, created_sku_orders, customers,
    direct_gmv_amount, direct_gmv_currency, items_sold, main_orders, payment_rate,
    sku_orders, add_to_cart_count, main_order_ctor, sku_order_ctor, ctr, watch_gpm,
    product_impressions, product_clicks, value_basis, ingested_at AS last_updated_at
FROM core.fact_tiktok_live_product;

-- -----------------------------------------------------------------------------
-- mart.v_ai_live_minute_daily — TikTok LIVE per-minute interval performance.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_live_minute_daily AS
SELECT
    channel, shop_id, live_id, business_date, interval_start, interval_end,
    room_impressions, enter_room_rate, viewers, views, ctr, product_clicks, product_impressions,
    comments, comment_rate, likes, like_rate, shares, share_rate, new_followers, follow_rate,
    customers, sku_orders, main_orders, items_sold, gmv_amount, gmv_currency,
    created_sku_orders, main_order_ctor, sku_order_ctor, show_gpm, watch_gpm,
    value_basis, ingested_at AS last_updated_at
FROM core.fact_tiktok_live_minute;

-- -----------------------------------------------------------------------------
-- mart.v_ai_affiliate_content_daily — Shopee AMS content-level affiliate
-- performance (ShopeeVideo / LiveStreaming). Additive to, not a replacement
-- for, mart.v_ai_affiliate_daily (order-item/financial grain, unchanged).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_affiliate_content_daily AS
SELECT
    channel, shop_id, content_id, content_channel, business_date, content_title, post_time,
    affiliate_name, affiliate_username, products_count, views, likes, comments,
    sales_amount, sales_currency, orders, items_sold, value_basis, ingested_at AS last_updated_at
FROM core.fact_shopee_affiliate_content_daily;

-- -----------------------------------------------------------------------------
-- mart.v_ai_operations_daily — Shopee Account Health prospective snapshot.
-- Current-state-only source (proven, no historical backfill) — snapshot_date
-- is the date this row was actually observed, not a business_date proxy.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_ai_operations_daily AS
SELECT
    shop_id, snapshot_date, metric_id, metric_name, metric_type, parent_metric_id,
    current_period, last_period, unit, target_value, target_comparator,
    exemption_end_date, overall_shop_rating, value_basis, ingested_at AS last_updated_at
FROM core.fact_shopee_account_health_snapshot;

GRANT SELECT ON
    mart.v_ai_video_daily,
    mart.v_ai_live_daily,
    mart.v_ai_live_product_daily,
    mart.v_ai_live_minute_daily,
    mart.v_ai_affiliate_content_daily,
    mart.v_ai_operations_daily
TO hh_ai_reader;
