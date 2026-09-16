-- =====================================================================
-- 031_ecom_extended_facts.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P2D data-model gap closure (Shopee +
-- TikTok only; Nhanh untouched).
--
-- NOTE ON FILE NUMBER: P2D's instructions suggested "030_ecom_extended_
-- facts.sql", but 030 was already used by P2B's additive migration
-- (sql/030_p2b_tiktok_additive.sql, applied 2026-09-11). Using 031 here
-- instead of overwriting/renumbering that file — renumbering an applied
-- migration would rewrite project history for no benefit.
--
-- Additive only: 3 new fact tables (affiliate, product analytics, LIVE
-- analytics) + 4 new nullable columns on the existing core.fact_ads_daily
-- (already shared by Shopee and TikTok). No DROP, no rewrite of any
-- existing column or table, no existing row deleted.
--
-- Rerun-safe: IF NOT EXISTS everywhere.
-- =====================================================================

-- ---------------------------------------------------------------------
-- core.fact_ads_daily — additive columns only.
-- ctr/roas/conversion are derivable (clicks/impressions, gmv/spend,
-- orders) but stored explicitly so a reporting query never needs to
-- recompute them inconsistently across channels. attribution_basis
-- makes the DIRECT/BROAD/OTHER_PLATFORM_DEFINED distinction an explicit
-- column instead of overloading campaign_id/campaign_name (Shopee's
-- current P2A rows use campaign_id='SHOP_TOTAL_DIRECT'/'SHOP_TOTAL_BROAD'
-- — those stay as-is; attribution_basis is backfilled from them below).
-- ---------------------------------------------------------------------
ALTER TABLE core.fact_ads_daily
    ADD COLUMN IF NOT EXISTS ctr               NUMERIC(9,4),
    ADD COLUMN IF NOT EXISTS roas               NUMERIC(9,4),
    ADD COLUMN IF NOT EXISTS conversion          BIGINT,
    ADD COLUMN IF NOT EXISTS attribution_basis   TEXT;

-- Backfill attribution_basis for the two existing Shopee P2A rows from
-- their existing campaign_id — populating a brand-new column on already
-- valid rows, not rewriting any existing value.
UPDATE core.fact_ads_daily
SET attribution_basis = 'DIRECT'
WHERE channel = 'SHOPEE' AND campaign_id = 'SHOP_TOTAL_DIRECT' AND attribution_basis IS NULL;

UPDATE core.fact_ads_daily
SET attribution_basis = 'BROAD'
WHERE channel = 'SHOPEE' AND campaign_id = 'SHOP_TOTAL_BROAD' AND attribution_basis IS NULL;

-- ---------------------------------------------------------------------
-- core.fact_affiliate_daily
-- Grain: one row per (channel, shop_id, order_id, sku_id, content_id) —
-- the natural commission-line grain returned by TikTok's affiliate
-- orders/search (one entry per creator/content promoting one SKU within
-- one order). Named "_daily" for consistency with fact_ads_daily /
-- fact_product_analytics_daily (each row belongs to one business_date),
-- not because rows are pre-aggregated to one-per-day — daily rollups
-- (affiliate_attributed_orders = COUNT DISTINCT order_id,
-- affiliate_attributed_gmv = SUM(affiliate_attributed_gmv)) are computed
-- from this table at query time, preserving full traceability to the
-- source commission line.
--
-- estimated_commission / validated_commission / settled_commission are
-- kept as separate columns per Section 4, but TikTok's API exposes only
-- one commission amount field (estimated_paid_shop_ads_commission_amount
-- — literally named "estimated" regardless of lifecycle state).
-- validated_commission has no source field and is always NULL for
-- TikTok. settled_commission is populated with that same amount ONLY
-- when settlement_status = 'SETTLED' (never a separately fabricated
-- number) — see p2d_tiktok_extended_to_postgres.py.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_affiliate_daily (
    affiliate_key             BIGSERIAL PRIMARY KEY,
    channel                   TEXT NOT NULL,
    shop_id                   TEXT NOT NULL,
    order_id                  TEXT NOT NULL,
    sku_id                    TEXT NOT NULL,
    content_id                TEXT NOT NULL,
    product_id                TEXT,
    creator_username          TEXT,
    content_type              TEXT,
    commission_model          TEXT,
    quantity                  NUMERIC(18,4),
    currency                  TEXT,
    affiliate_attributed_gmv  NUMERIC(18,4),   -- = estimated_commission_base_amount (the sales value the commission is computed on)
    estimated_commission      NUMERIC(18,4),   -- = estimated_paid_shop_ads_commission_amount, always present when a commission applies
    validated_commission      NUMERIC(18,4),   -- no TikTok source field observed; NULL until a source proves otherwise
    settled_commission        NUMERIC(18,4),   -- = estimated_commission WHEN settlement_status = 'SETTLED', else NULL (never fabricated)
    settlement_status         TEXT,            -- verbatim source value (e.g. SETTLED / AWAITING PAYMENT / To-SETTLE / INELIGIBLE)
    business_date             DATE NOT NULL,
    source_system             TEXT NOT NULL,
    source_record_id          TEXT NOT NULL,
    source_created_at         TIMESTAMPTZ,
    source_updated_at         TIMESTAMPTZ,
    source_endpoint           TEXT,
    source_shop_id            TEXT,
    ingested_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id                UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_affiliate_daily UNIQUE (channel, shop_id, order_id, sku_id, content_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_affiliate_daily_business_date ON core.fact_affiliate_daily (business_date);

-- ---------------------------------------------------------------------
-- core.fact_product_analytics_daily
-- Grain: one row per (channel, shop_id, product_id, business_date) —
-- matches TikTok's native per-product-per-day analytics response
-- exactly (no SKU-level breakdown is exposed by this endpoint; sku_id
-- stays NULL on every TikTok row and is included only so a future
-- channel/endpoint that IS SKU-level can populate it without another
-- migration).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_product_analytics_daily (
    pa_key              BIGSERIAL PRIMARY KEY,
    channel             TEXT NOT NULL,
    shop_id             TEXT NOT NULL,
    product_id          TEXT NOT NULL,
    sku_id              TEXT,
    business_date       DATE NOT NULL,
    impressions         BIGINT,
    clicks              BIGINT,
    ctr                 NUMERIC(9,4),
    attributed_orders   BIGINT,   -- Product-Analytics-attributed orders — NEVER the platform order count (see fact_order)
    click_to_order_rate NUMERIC(9,4),
    gmv_amount          NUMERIC(18,4),
    gmv_currency        TEXT,
    items_sold          BIGINT,
    source_system       TEXT NOT NULL,
    source_record_id    TEXT NOT NULL,
    source_created_at   TIMESTAMPTZ,
    source_updated_at   TIMESTAMPTZ,
    source_endpoint     TEXT,
    source_shop_id      TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id          UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_product_analytics_daily UNIQUE (channel, shop_id, product_id, business_date)
);

CREATE INDEX IF NOT EXISTS ix_fact_product_analytics_daily_business_date ON core.fact_product_analytics_daily (business_date);

-- ---------------------------------------------------------------------
-- core.fact_live_daily
-- Grain: one row per (channel, shop_id, live_id) — one LIVE session.
-- business_date is derived from start_time (Asia/Ho_Chi_Minh), matching
-- every other fact table's convention. available_data_date +
-- latency_status preserve TikTok LIVE's known data-latency behavior
-- (Section 6): when the requested date isn't ready yet, the row (if any)
-- must carry NULL metrics + latency_status='LATENCY_DATA_NOT_YET_AVAILABLE',
-- never fabricated zeros.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fact_live_daily (
    live_key              BIGSERIAL PRIMARY KEY,
    channel               TEXT NOT NULL,
    shop_id               TEXT NOT NULL,
    live_id               TEXT NOT NULL,
    business_date         DATE NOT NULL,
    available_data_date   DATE,
    title                 TEXT,
    username              TEXT,
    start_time            TIMESTAMPTZ,
    end_time              TIMESTAMPTZ,
    viewers               BIGINT,
    views                 BIGINT,
    product_impressions   BIGINT,
    product_clicks        BIGINT,
    sku_orders            BIGINT,
    gmv_amount            NUMERIC(18,4),
    gmv_currency          TEXT,
    latency_status        TEXT NOT NULL DEFAULT 'READY',
    source_system         TEXT NOT NULL,
    source_record_id      TEXT NOT NULL,
    source_created_at     TIMESTAMPTZ,
    source_updated_at     TIMESTAMPTZ,
    source_endpoint       TEXT,
    source_shop_id        TEXT,
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id            UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_fact_live_daily UNIQUE (channel, shop_id, live_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_live_daily_business_date ON core.fact_live_daily (business_date);
