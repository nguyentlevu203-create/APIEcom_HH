-- =====================================================================
-- 020_facts.sql
-- HH_ECOM_AI_PILOT / hh_ecom — core fact tables.
-- Rerun-safe: IF NOT EXISTS everywhere, never drops existing objects.
--
-- Every fact table below carries the required traceability columns:
--   source_system, source_record_id, source_created_at,
--   source_updated_at, business_date, ingested_at, etl_run_id,
--   source_endpoint, source_shop_id
--
-- No customer PII is stored (no name/phone/address/email columns).
-- business_date is a DATE computed by the ETL layer in
-- Asia/Ho_Chi_Minh, not Asia/Bangkok, from the relevant source
-- timestamp.
-- =====================================================================

-- ---------------------------------------------------------------------
-- core.fact_order
-- Unique key: channel + shop_id + order_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_order (
    order_key          BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    shop_id            TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    order_status       TEXT,
    order_create_time  TIMESTAMPTZ,
    order_update_time  TIMESTAMPTZ,
    currency           TEXT,
    total_amount       NUMERIC(18,4),
    business_date      DATE NOT NULL,
    source_system      TEXT NOT NULL,
    source_record_id   TEXT NOT NULL,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_order UNIQUE (channel, shop_id, order_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_order_business_date ON core.fact_order (business_date);

-- ---------------------------------------------------------------------
-- core.fact_order_item
-- Unique key: channel + shop_id + order_id + order_item_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_order_item (
    order_item_key     BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    shop_id            TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    order_item_id      TEXT NOT NULL,
    sku                TEXT,
    product_name       TEXT,
    qty                NUMERIC(18,4),
    unit_price         NUMERIC(18,4),
    item_amount        NUMERIC(18,4),
    business_date      DATE NOT NULL,
    source_system      TEXT NOT NULL,
    source_record_id   TEXT NOT NULL,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_order_item UNIQUE (channel, shop_id, order_id, order_item_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_order_item_business_date ON core.fact_order_item (business_date);
CREATE INDEX IF NOT EXISTS ix_fact_order_item_sku ON core.fact_order_item (sku);

-- ---------------------------------------------------------------------
-- core.fact_settlement
-- Unique key: channel + shop_id + settlement_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_settlement (
    settlement_key     BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    shop_id            TEXT NOT NULL,
    order_id           TEXT,
    settlement_id      TEXT NOT NULL,
    settlement_amount  NUMERIC(18,4),
    settlement_type    TEXT,
    settlement_time    TIMESTAMPTZ,
    business_date      DATE NOT NULL,
    source_system      TEXT NOT NULL,
    source_record_id   TEXT NOT NULL,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_settlement UNIQUE (channel, shop_id, settlement_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_settlement_business_date ON core.fact_settlement (business_date);
CREATE INDEX IF NOT EXISTS ix_fact_settlement_order_id ON core.fact_settlement (channel, shop_id, order_id);

-- ---------------------------------------------------------------------
-- core.fact_ads_daily
-- Unique key: channel + ad_account_id + business_date + campaign_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_ads_daily (
    ads_key            BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    ad_account_id      TEXT NOT NULL,
    shop_id            TEXT,
    campaign_id        TEXT NOT NULL,
    campaign_name      TEXT,
    business_date      DATE NOT NULL,
    impressions        BIGINT,
    clicks             BIGINT,
    spend              NUMERIC(18,4),
    orders             BIGINT,
    gmv                NUMERIC(18,4),
    source_system      TEXT NOT NULL,
    source_record_id   TEXT,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_ads_daily UNIQUE (channel, ad_account_id, business_date, campaign_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_ads_daily_business_date ON core.fact_ads_daily (business_date);

-- ---------------------------------------------------------------------
-- core.fact_return_refund
-- Unique key: channel + shop_id + return_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_return_refund (
    return_key         BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    shop_id            TEXT NOT NULL,
    order_id           TEXT,
    return_id          TEXT NOT NULL,
    return_status      TEXT,
    refund_amount      NUMERIC(18,4),
    return_reason      TEXT,
    business_date      DATE NOT NULL,
    source_system      TEXT NOT NULL,
    source_record_id   TEXT NOT NULL,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_return_refund UNIQUE (channel, shop_id, return_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_return_refund_business_date ON core.fact_return_refund (business_date);

-- ---------------------------------------------------------------------
-- core.fact_inventory_snapshot
-- Unique key: channel + warehouse + sku + snapshot_at
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_inventory_snapshot (
    inventory_key      BIGSERIAL PRIMARY KEY,
    channel            TEXT NOT NULL,
    warehouse          TEXT NOT NULL,
    sku                TEXT NOT NULL,
    snapshot_at        TIMESTAMPTZ NOT NULL,
    business_date      DATE NOT NULL,
    qty_on_hand        NUMERIC(18,4),
    qty_reserved       NUMERIC(18,4),
    qty_available      NUMERIC(18,4),
    source_system      TEXT NOT NULL,
    source_record_id   TEXT,
    source_created_at  TIMESTAMPTZ,
    source_updated_at  TIMESTAMPTZ,
    source_endpoint    TEXT,
    source_shop_id     TEXT,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id         UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_inventory_snapshot UNIQUE (channel, warehouse, sku, snapshot_at)
);

CREATE INDEX IF NOT EXISTS ix_fact_inventory_snapshot_business_date ON core.fact_inventory_snapshot (business_date);
