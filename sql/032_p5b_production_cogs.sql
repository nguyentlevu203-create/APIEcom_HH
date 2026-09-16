-- =====================================================================
-- 032_p5b_production_cogs.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P5B production Product/COGS master,
-- platform mapping, and combo BOM.
--
-- Additive only. No DROP, no destructive rewrite, no existing row
-- deleted. dim_product/dim_cogs are enriched with new nullable columns;
-- dim_cogs also needs two narrow, deliberate widen/relax changes (both
-- justified below) because the table is empty (0 rows) and the HH-
-- approved COGS_DATE_001/unit-cost-precision requirements cannot be met
-- under its original P1 constraints. Two new tables are created for
-- platform-SKU mapping and combo BOM (neither existed before — Section 4
-- confirmed no equivalent structure exists to reuse).
--
-- Rerun-safe: IF NOT EXISTS everywhere.
-- =====================================================================

-- ---------------------------------------------------------------------
-- core.dim_product — additive columns for the P5A-approved master model
-- (3-layer product/role/cost-treatment design from P5A.3 §1).
-- ---------------------------------------------------------------------
ALTER TABLE core.dim_product
    ADD COLUMN IF NOT EXISTS master_product_type  TEXT,
    ADD COLUMN IF NOT EXISTS invoice_name          TEXT,
    ADD COLUMN IF NOT EXISTS volume                TEXT,
    ADD COLUMN IF NOT EXISTS unit                  TEXT,
    ADD COLUMN IF NOT EXISTS case_pack              NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS pallet_pack             NUMERIC(18,4),
    -- Original source text (e.g. "nhập lô air" for DT18054) preserved for
    -- traceability per ITEM_NAME_001's explicit instruction — product_name
    -- itself carries the new HH-approved canonical name.
    ADD COLUMN IF NOT EXISTS source_product_name      TEXT;

-- ---------------------------------------------------------------------
-- core.dim_cogs — two deliberate changes, both justified and both safe
-- only because the table is currently EMPTY (0 rows, reconfirmed
-- immediately before this migration):
--
-- 1) unit_cost NUMERIC(18,4) -> NUMERIC(24,12): the P1-era scale (4
--    decimal places) cannot hold the source workbook's actual precision
--    (e.g. 54799.51681159621 — 11 decimal places) without truncation,
--    which would violate COGS Section 6's explicit "do not round stored
--    cost". Widening precision/scale on an empty column loses nothing.
-- 2) effective_from DATE NOT NULL -> nullable: HH's COGS_DATE_001 =
--    TIME_INDEPENDENT decision requires storing NULL, never an invented
--    date. The original NOT NULL assumed every cost row would carry a
--    real effective date; that assumption is what changed, not a
--    workaround.
-- ---------------------------------------------------------------------
ALTER TABLE core.dim_cogs
    ALTER COLUMN unit_cost TYPE NUMERIC(24,12),
    ALTER COLUMN effective_from DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS cost_basis        TEXT,
    ADD COLUMN IF NOT EXISTS cost_date_rule     TEXT NOT NULL DEFAULT 'TIME_INDEPENDENT',
    ADD COLUMN IF NOT EXISTS is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS source_record_id    TEXT;

-- The existing UNIQUE(sku, effective_from) constraint does NOT prevent
-- duplicate (sku, NULL) rows — standard SQL treats NULL <> NULL even for
-- uniqueness, so a second load of the same TIME_INDEPENDENT sku would
-- insert a duplicate row instead of conflicting. This partial unique
-- index closes that gap additively, without altering or dropping the
-- original constraint (which still correctly governs any future
-- non-NULL, date-sliced cost rows).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_cogs_time_independent
    ON core.dim_cogs (sku) WHERE effective_from IS NULL;

-- ---------------------------------------------------------------------
-- core.map_platform_product — NEW. No equivalent table existed (Section
-- 4 preflight: fact_order_item.sku is the only existing home for a
-- platform identifier, and it cannot carry the full identifier set —
-- item_id/model_id/mapping method/confidence — needed to answer "why
-- was this line mapped this way" without re-deriving it every time).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.map_platform_product (
    id                    BIGSERIAL PRIMARY KEY,
    channel               TEXT NOT NULL,
    shop_id               TEXT NOT NULL,
    -- The identifier string exactly as it appears in core.fact_order_item.sku
    -- (or, for Shopee no-variation lines where that column is blank, the
    -- item_sku recovered in P5A.1 — Section 7's explicit "do not revert to
    -- model_sku-only logic" requirement).
    platform_identifier   TEXT NOT NULL,
    identifier_source     TEXT NOT NULL,   -- seller_sku | item_sku | model_sku
    item_id               TEXT,
    model_id              TEXT,
    hh_sku                TEXT NOT NULL,
    ean                    TEXT,
    item_key               TEXT NOT NULL,
    mapping_method          TEXT NOT NULL,
    mapping_confidence       TEXT NOT NULL DEFAULT 'HIGH',
    is_combo                 BOOLEAN NOT NULL DEFAULT FALSE,
    bom_pattern_id            TEXT,
    source_system              TEXT,
    ingested_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id                  UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_map_platform_product UNIQUE (channel, shop_id, platform_identifier)
);

CREATE INDEX IF NOT EXISTS ix_map_platform_product_hh_sku ON core.map_platform_product (hh_sku);

-- ---------------------------------------------------------------------
-- core.dim_combo_bom — NEW. No equivalent table existed.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_combo_bom (
    id                   BIGSERIAL PRIMARY KEY,
    bom_pattern_id       TEXT NOT NULL,
    bom_description       TEXT,
    component_hh_sku       TEXT NOT NULL,
    component_ean            TEXT,
    component_item_key        TEXT,
    component_qty              NUMERIC(18,4) NOT NULL,
    component_role               TEXT NOT NULL DEFAULT 'PRODUCT',
    approval_status                TEXT NOT NULL,
    evidence_source                 TEXT,
    source_system                    TEXT,
    ingested_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id                        UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_dim_combo_bom UNIQUE (bom_pattern_id, component_hh_sku)
);
