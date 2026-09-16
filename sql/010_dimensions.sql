-- =====================================================================
-- 010_dimensions.sql
-- HH_ECOM_AI_PILOT / hh_ecom — core dimension tables.
-- Rerun-safe: IF NOT EXISTS everywhere, never drops existing objects.
-- =====================================================================

-- ---------------------------------------------------------------------
-- core.dim_product
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_product (
    product_key       BIGSERIAL PRIMARY KEY,
    sku               TEXT NOT NULL,
    product_name      TEXT,
    brand             TEXT,
    category_l1       TEXT,
    category_l2       TEXT,
    category_l3       TEXT,
    barcode           TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT true,
    source_system     TEXT,
    source_record_id  TEXT,
    source_created_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id        UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_dim_product_sku UNIQUE (sku)
);

-- ---------------------------------------------------------------------
-- core.dim_channel
-- One row per (channel, shop_id) — the sales-channel/shop grain shared
-- by every fact table's channel+shop_id columns.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_channel (
    channel_key   SERIAL PRIMARY KEY,
    channel       TEXT NOT NULL,
    shop_id       TEXT NOT NULL,
    shop_name     TEXT,
    market        TEXT NOT NULL DEFAULT 'VN',
    is_active     BOOLEAN NOT NULL DEFAULT true,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_channel UNIQUE (channel, shop_id)
);

-- ---------------------------------------------------------------------
-- core.dim_cogs
-- Effective-dated cost of goods sold per SKU. No overlap enforcement in
-- P1 beyond the unique key below — overlap checks belong to the ETL
-- layer / audit.data_quality_result.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_cogs (
    id              BIGSERIAL PRIMARY KEY,
    sku             TEXT NOT NULL,
    effective_from  DATE NOT NULL,
    effective_to    DATE,
    unit_cost       NUMERIC(18,4) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'VND',
    source_system   TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_cogs UNIQUE (sku, effective_from)
);

-- ---------------------------------------------------------------------
-- core.dim_target
-- KPI targets by scope (company/channel/sku) and period. metric_value
-- may be NULL if a target has not been set yet — never defaulted to 0.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_target (
    id            BIGSERIAL PRIMARY KEY,
    target_scope  TEXT NOT NULL,
    channel       TEXT,
    shop_id       TEXT,
    sku           TEXT,
    metric_name   TEXT NOT NULL,
    period_type   TEXT NOT NULL,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    target_value  NUMERIC(18,4),
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_target UNIQUE (target_scope, channel, shop_id, sku, metric_name, period_type, period_start)
);
