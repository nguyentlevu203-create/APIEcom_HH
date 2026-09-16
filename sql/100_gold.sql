-- =====================================================================
-- 100_gold.sql
-- HH_ECOM_AI_PILOT / hh_ecom — mart (Gold) structures ONLY.
--
-- P1 scope: create structures only. Do NOT populate or invent KPI
-- values here. Every metric row is (grain..., metric_name,
-- metric_value, coverage_status) rather than fixed wide columns, so
-- that:
--   - a metric with no computable source is written with
--     metric_value = NULL and coverage_status describing why
--     ('no_source' | 'partial' | 'complete' | 'unknown'),
--   - a missing metric is NEVER defaulted to 0,
--   - new metrics can be added later without an ALTER TABLE.
--
-- Rerun-safe: IF NOT EXISTS everywhere, never drops existing objects.
-- =====================================================================

-- ---------------------------------------------------------------------
-- mart.gold_ceo_daily — company-wide daily KPIs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_ceo_daily (
    business_date    DATE NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, metric_name)
);

-- ---------------------------------------------------------------------
-- mart.gold_channel_daily — per channel/shop daily KPIs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_channel_daily (
    business_date    DATE NOT NULL,
    channel          TEXT NOT NULL,
    shop_id          TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, channel, shop_id, metric_name)
);

-- ---------------------------------------------------------------------
-- mart.gold_channel_pnl — per channel/shop daily P&L lines
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_channel_pnl (
    business_date    DATE NOT NULL,
    channel          TEXT NOT NULL,
    shop_id          TEXT NOT NULL,
    pnl_line         TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, channel, shop_id, pnl_line)
);

-- ---------------------------------------------------------------------
-- mart.gold_sku_daily — per SKU daily KPIs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_sku_daily (
    business_date    DATE NOT NULL,
    channel          TEXT NOT NULL,
    shop_id          TEXT NOT NULL,
    sku              TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, channel, shop_id, sku, metric_name)
);

-- ---------------------------------------------------------------------
-- mart.gold_ads_daily — per campaign daily ads KPIs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_ads_daily (
    business_date    DATE NOT NULL,
    channel          TEXT NOT NULL,
    ad_account_id    TEXT NOT NULL,
    campaign_id      TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, channel, ad_account_id, campaign_id, metric_name)
);

-- ---------------------------------------------------------------------
-- mart.gold_inventory_daily — per warehouse/SKU daily inventory KPIs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_inventory_daily (
    business_date    DATE NOT NULL,
    channel          TEXT NOT NULL,
    warehouse        TEXT NOT NULL,
    sku              TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, channel, warehouse, sku, metric_name)
);

-- ---------------------------------------------------------------------
-- mart.gold_exception_daily — daily data-quality / reconciliation
-- exceptions surfaced to the CEO view. channel/shop_id/sku are
-- nullable (an exception may be company-wide), so this table uses a
-- surrogate key instead of a natural composite primary key.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.gold_exception_daily (
    id               BIGSERIAL PRIMARY KEY,
    business_date    DATE NOT NULL,
    exception_type   TEXT NOT NULL,
    channel          TEXT,
    shop_id          TEXT,
    sku              TEXT,
    description      TEXT,
    severity         TEXT NOT NULL DEFAULT 'info',
    coverage_status  TEXT NOT NULL DEFAULT 'no_source',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_gold_exception_daily_date_type
    ON mart.gold_exception_daily (business_date, exception_type);
