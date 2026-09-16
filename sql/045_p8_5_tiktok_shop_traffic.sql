-- =====================================================================
-- 045_p8_5_tiktok_shop_traffic.sql
-- P8.5 Section 9/13 — TikTok shop traffic (real visitors data, proven
-- live via GET /analytics/202510/shop/performance/{date}/performance_per_hour,
-- scope data.shop_analytics.public.read, already GRANTED).
--
-- Two grains, both real API fields, never conflated with each other or
-- with any other funnel metric:
--   fact_shop_traffic_daily  — always available: the API's own "overall"
--                              object for the business date.
--   fact_shop_traffic_hourly — available when the API returns non-empty
--                              intervals (recent dates; older dates can
--                              return overall-only with empty intervals,
--                              e.g. proven live 2026-08-15).
--
-- "visitors" is TikTok's own field name — never substituted for or
-- conflated with impressions/clicks/views/sessions. No session-level or
-- page-view metric was found in this payload.
-- =====================================================================

CREATE TABLE IF NOT EXISTS core.fact_shop_traffic_daily (
    traffic_key         BIGSERIAL PRIMARY KEY,
    channel              TEXT NOT NULL,
    shop_id              TEXT NOT NULL,
    business_date        DATE NOT NULL,
    visitors              BIGINT,
    customers            BIGINT,
    gmv_amount           NUMERIC(18,4),
    gmv_currency         TEXT,
    items_sold           BIGINT,
    value_basis          TEXT NOT NULL,   -- 'API_OVERALL' (always, direct from the endpoint's own overall object)
    source_system        TEXT NOT NULL,
    source_record_id     TEXT,
    source_endpoint      TEXT,
    source_shop_id       TEXT,
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id            UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_shop_traffic_daily UNIQUE (channel, shop_id, business_date)
);

CREATE TABLE IF NOT EXISTS core.fact_shop_traffic_hourly (
    traffic_hour_key      BIGSERIAL PRIMARY KEY,
    channel               TEXT NOT NULL,
    shop_id               TEXT NOT NULL,
    business_date         DATE NOT NULL,
    interval_index        INT NOT NULL,     -- API's own 0-23 hour index (VN local time per endpoint spec)
    visitors               BIGINT,
    customers             BIGINT,
    gmv_amount            NUMERIC(18,4),
    gmv_currency          TEXT,
    items_sold            BIGINT,
    source_system         TEXT NOT NULL,
    source_endpoint       TEXT,
    source_shop_id        TEXT,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id             UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_shop_traffic_hourly UNIQUE (channel, shop_id, business_date, interval_index)
);

CREATE INDEX IF NOT EXISTS ix_fact_shop_traffic_daily_date ON core.fact_shop_traffic_daily (business_date);
CREATE INDEX IF NOT EXISTS ix_fact_shop_traffic_hourly_date ON core.fact_shop_traffic_hourly (business_date);
