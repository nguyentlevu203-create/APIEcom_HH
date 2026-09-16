-- =============================================================================
-- P7.1 — Platform KPI API Coverage + Production Ingestion
-- All additive: new tables + one additive ALTER on core.fact_live_daily.
-- No existing column dropped/retyped, no existing row's meaning changed.
-- Run as neondb_owner (hh_etl_writer has no CREATE/ALTER on core by design).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. TikTok Video content-performance (NEW table).
-- Grain: channel + shop_id + video_id + account_type + business_date.
-- business_date = the analytics query window's report date (metrics reflect
-- activity within that window, NOT the video's post date — video_post_time
-- is kept separately as evidence). account_type in {ALL, AFFILIATE_ACCOUNTS}
-- per real API probe (/analytics/202605/shop_videos/performance).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_tiktok_video_daily (
    video_key             BIGSERIAL PRIMARY KEY,
    channel               TEXT NOT NULL,
    shop_id               TEXT NOT NULL,
    video_id              TEXT NOT NULL,
    business_date         DATE NOT NULL,
    account_type          TEXT NOT NULL,
    creator_open_id       TEXT,
    creator_username      TEXT,
    creator_nick_name     TEXT,
    creator_author_type   TEXT,
    title                 TEXT,
    video_post_time       TIMESTAMPTZ,
    duration_seconds      INTEGER,
    views                 BIGINT,
    avg_customers         BIGINT,
    click_through_rate    NUMERIC(10,6),
    sku_orders            BIGINT,
    items_sold            BIGINT,
    gmv_amount            NUMERIC(24,12),
    gmv_currency          TEXT,
    gpm_amount            NUMERIC(24,12),
    gpm_currency          TEXT,
    latest_available_date DATE,
    source_system         TEXT NOT NULL,
    source_record_id      TEXT NOT NULL,
    source_endpoint       TEXT NOT NULL,
    source_api_version    TEXT NOT NULL,
    value_basis           TEXT NOT NULL DEFAULT 'API_ACTUAL',
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id            UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_tiktok_video_daily UNIQUE (channel, shop_id, video_id, account_type, business_date)
);
CREATE INDEX IF NOT EXISTS ix_fact_tiktok_video_daily_business_date ON core.fact_tiktok_video_daily (business_date);

-- -----------------------------------------------------------------------------
-- 2. TikTok LIVE — additive extension of the EXISTING core.fact_live_daily
-- (reused per task instruction, grain already matches: live_id+business_date).
-- Adds account_type to the grain (both ALL and AFFILIATE_ACCOUNTS breakdowns
-- exist for the same live_id) plus the real engagement/conversion fields
-- proven by the live_analytics (202509/shop_lives/performance) probe.
-- Existing 20 rows get account_type='ALL' (their original collection never
-- distinguished account_type, so 'ALL' is the correct, non-fabricated label
-- for what was actually queried).
-- -----------------------------------------------------------------------------
ALTER TABLE core.fact_live_daily
    ADD COLUMN IF NOT EXISTS account_type            TEXT NOT NULL DEFAULT 'ALL',
    ADD COLUMN IF NOT EXISTS duration_seconds         INTEGER,
    ADD COLUMN IF NOT EXISTS customers                BIGINT,
    ADD COLUMN IF NOT EXISTS items_sold               BIGINT,
    ADD COLUMN IF NOT EXISTS likes                    BIGINT,
    ADD COLUMN IF NOT EXISTS comments                 BIGINT,
    ADD COLUMN IF NOT EXISTS shares                   BIGINT,
    ADD COLUMN IF NOT EXISTS new_followers             BIGINT,
    ADD COLUMN IF NOT EXISTS avg_viewing_duration_secs BIGINT,
    ADD COLUMN IF NOT EXISTS click_through_rate        NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS click_to_order_rate        NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS latest_available_date      DATE,
    ADD COLUMN IF NOT EXISTS source_api_version         TEXT,
    ADD COLUMN IF NOT EXISTS value_basis                TEXT NOT NULL DEFAULT 'API_ACTUAL';

ALTER TABLE core.fact_live_daily DROP CONSTRAINT IF EXISTS uq_fact_live_daily;
ALTER TABLE core.fact_live_daily
    ADD CONSTRAINT uq_fact_live_daily UNIQUE (channel, shop_id, live_id, account_type);

-- -----------------------------------------------------------------------------
-- 3. TikTok LIVE per-minute interval (NEW table).
-- Grain: live_id + interval_start. Proven by
-- /analytics/202510/shop_lives/{live_id}/performance_per_minutes — this
-- endpoint uniquely exposes room-level `impressions` and `enter_room_rate`,
-- which are NOT the same as product_impressions (see report). Rates
-- (comment_rate/like_rate/share_rate/follow_rate) are returned directly by
-- the API, not derived here.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_tiktok_live_minute (
    live_minute_key        BIGSERIAL PRIMARY KEY,
    channel                TEXT NOT NULL,
    shop_id                TEXT NOT NULL,
    live_id                TEXT NOT NULL,
    business_date          DATE NOT NULL,
    interval_start         TIMESTAMPTZ NOT NULL,
    interval_end           TIMESTAMPTZ,
    room_impressions       BIGINT,
    enter_room_rate        NUMERIC(10,6),
    viewers                BIGINT,
    views                  BIGINT,
    ctr                    NUMERIC(10,6),
    product_clicks         BIGINT,
    product_impressions    BIGINT,
    comments               BIGINT,
    comment_rate           NUMERIC(10,6),
    likes                  BIGINT,
    like_rate              NUMERIC(10,6),
    shares                 BIGINT,
    share_rate             NUMERIC(10,6),
    new_followers          BIGINT,
    follow_rate            NUMERIC(10,6),
    customers              BIGINT,
    sku_orders             BIGINT,
    main_orders            BIGINT,
    items_sold             BIGINT,
    gmv_amount             NUMERIC(24,12),
    gmv_currency           TEXT,
    created_sku_orders     BIGINT,
    main_order_ctor        NUMERIC(10,6),
    sku_order_ctor         NUMERIC(10,6),
    avg_price_amount       NUMERIC(24,12),
    show_gpm               NUMERIC(24,12),
    watch_gpm              NUMERIC(24,12),
    source_system          TEXT NOT NULL,
    source_endpoint        TEXT NOT NULL,
    source_api_version     TEXT NOT NULL,
    value_basis            TEXT NOT NULL DEFAULT 'API_ACTUAL',
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id             UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_tiktok_live_minute UNIQUE (channel, shop_id, live_id, interval_start)
);
CREATE INDEX IF NOT EXISTS ix_fact_tiktok_live_minute_business_date ON core.fact_tiktok_live_minute (business_date);

-- -----------------------------------------------------------------------------
-- 4. TikTok LIVE per-product (NEW table). Grain: live_id + product_id.
-- Proven by /analytics/202512/shop/{live_id}/products_performance.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_tiktok_live_product (
    live_product_key       BIGSERIAL PRIMARY KEY,
    channel                TEXT NOT NULL,
    shop_id                TEXT NOT NULL,
    live_id                TEXT NOT NULL,
    product_id             TEXT NOT NULL,
    business_date          DATE NOT NULL,
    product_name           TEXT,
    avg_price_amount       NUMERIC(24,12),
    avg_price_currency     TEXT,
    created_sku_orders     BIGINT,
    customers              BIGINT,
    direct_gmv_amount      NUMERIC(24,12),
    direct_gmv_currency    TEXT,
    items_sold             BIGINT,
    main_orders            BIGINT,
    payment_rate           NUMERIC(10,6),
    sku_orders             BIGINT,
    add_to_cart_count      BIGINT,
    main_order_ctor        NUMERIC(10,6),
    sku_order_ctor         NUMERIC(10,6),
    ctr                    NUMERIC(10,6),
    watch_gpm              NUMERIC(24,12),
    product_impressions    BIGINT,
    product_clicks         BIGINT,
    source_system          TEXT NOT NULL,
    source_endpoint        TEXT NOT NULL,
    source_api_version     TEXT NOT NULL,
    value_basis            TEXT NOT NULL DEFAULT 'API_ACTUAL',
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id             UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_tiktok_live_product UNIQUE (channel, shop_id, live_id, product_id)
);
CREATE INDEX IF NOT EXISTS ix_fact_tiktok_live_product_business_date ON core.fact_tiktok_live_product (business_date);

-- -----------------------------------------------------------------------------
-- 5. Shopee Affiliate CONTENT performance (NEW, additive table — does NOT
-- touch core.fact_affiliate_daily, which is order-item grain and stays the
-- financial/commission SSOT). Grain: business_date + content_id + affiliate
-- + channel. Proven by GET /api/v2/ams/get_content_performance
-- (channel=ShopeeVideo, LiveStreaming).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_shopee_affiliate_content_daily (
    content_key            BIGSERIAL PRIMARY KEY,
    channel                TEXT NOT NULL DEFAULT 'SHOPEE',
    shop_id                TEXT NOT NULL,
    content_id             TEXT NOT NULL,
    content_channel        TEXT NOT NULL,   -- ShopeeVideo | LiveStreaming
    business_date          DATE NOT NULL,
    content_title          TEXT,
    post_time              TIMESTAMPTZ,
    affiliate_name         TEXT,
    affiliate_username     TEXT,
    products_count         INTEGER,
    views                  BIGINT,
    likes                  BIGINT,
    comments               BIGINT,
    sales_amount           NUMERIC(24,12),
    sales_currency         TEXT DEFAULT 'VND',
    orders                 BIGINT,
    items_sold             BIGINT,
    source_system          TEXT NOT NULL DEFAULT 'SHOPEE_AMS',
    source_endpoint        TEXT NOT NULL,
    source_api_version     TEXT,
    value_basis            TEXT NOT NULL DEFAULT 'API_ACTUAL',
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id             UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_shopee_affiliate_content_daily UNIQUE (channel, shop_id, content_id, content_channel, business_date)
);
CREATE INDEX IF NOT EXISTS ix_fact_shopee_affiliate_content_daily_date ON core.fact_shopee_affiliate_content_daily (business_date);

-- -----------------------------------------------------------------------------
-- 6. Shopee Account Health (NEW, prospective daily snapshot table). Grain:
-- snapshot_date + metric_id. Proven CURRENT-STATE-ONLY (identical response
-- with/without a historical date-range query param) — no fake backfill;
-- snapshots begin from the date this phase first ran.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_shopee_account_health_snapshot (
    snapshot_key            BIGSERIAL PRIMARY KEY,
    shop_id                 TEXT NOT NULL,
    snapshot_date           DATE NOT NULL,
    metric_id               INTEGER NOT NULL,
    metric_name             TEXT NOT NULL,
    metric_type             INTEGER,
    parent_metric_id        INTEGER,
    current_period          NUMERIC(24,6),
    last_period             NUMERIC(24,6),
    unit                    INTEGER,
    target_value            NUMERIC(24,6),
    target_comparator       TEXT,
    exemption_end_date      DATE,
    overall_shop_rating     NUMERIC(6,2),
    source_system           TEXT NOT NULL DEFAULT 'SHOPEE',
    source_endpoint         TEXT NOT NULL DEFAULT '/api/v2/account_health/get_shop_performance',
    value_basis             TEXT NOT NULL DEFAULT 'API_ACTUAL',
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id              UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_shopee_account_health_snapshot UNIQUE (shop_id, snapshot_date, metric_id)
);
CREATE INDEX IF NOT EXISTS ix_fact_shopee_account_health_snapshot_date ON core.fact_shopee_account_health_snapshot (snapshot_date);

-- -----------------------------------------------------------------------------
-- Grants: hh_etl_writer needs INSERT/SELECT/UPDATE on all 6 new/extended
-- objects (P1 default was per-table, not schema-wide — same pattern as
-- every prior additive migration).
-- -----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON
    core.fact_tiktok_video_daily,
    core.fact_tiktok_live_minute,
    core.fact_tiktok_live_product,
    core.fact_shopee_affiliate_content_daily,
    core.fact_shopee_account_health_snapshot
TO hh_etl_writer;

GRANT USAGE, SELECT ON
    core.fact_tiktok_video_daily_video_key_seq,
    core.fact_tiktok_live_minute_live_minute_key_seq,
    core.fact_tiktok_live_product_live_product_key_seq,
    core.fact_shopee_affiliate_content_daily_content_key_seq,
    core.fact_shopee_account_health_snapshot_snapshot_key_seq
TO hh_etl_writer;

-- hh_ai_reader gets nothing here directly — it only ever reads via mart.v_ai_*
-- views (P7 security model: zero grants on core.* for the AI role). New
-- mart views for these tables are added in a separate migration/step, with
-- the corresponding hh_ai_reader GRANT on those views only.
