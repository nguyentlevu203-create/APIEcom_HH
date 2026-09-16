-- =====================================================================
-- 037_p6b3_tiktok_sku_fee_detail.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P6B.3 TikTok SKU-level fee detail.
--
-- P6B.2 proved (real evidence, 112 SKU transactions) that TikTok's
-- finance_order_statement_transactions response — the exact endpoint
-- core.fact_settlement already calls — carries a sku_transactions[]
-- array with fully itemized fee_tax_breakdown.fee.{platform_commission_
-- amount, transaction_fee_amount, voucher_xtra_service_fee_amount,
-- vn_fix_infrastructure_fee, affiliate_commission_amount}. This has
-- never been persisted — core.fact_settlement only ever stored the
-- order-level blended fee_and_tax_amount.
--
-- New table, not new columns on core.fact_settlement: an order can have
-- multiple sku_transactions (one per SKU line within that order's
-- statement), a genuinely different grain from fact_settlement's one-
-- row-per-order. Storing at this grain is lossless (Section 3) and
-- lets CM1 be rebuilt as SUM(sku-level fee) per order/date without ever
-- double-counting against the existing order-level fee_and_tax_amount
-- (which is superseded for CM1 purposes, not deleted — kept for
-- SETTLEMENT_ACTUAL / cross-check use).
--
-- Additive only. No existing table/column touched.
-- =====================================================================
CREATE TABLE IF NOT EXISTS core.fact_settlement_sku_fee (
    id                      BIGSERIAL PRIMARY KEY,
    channel                 TEXT NOT NULL,
    shop_id                 TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    sku_id                  TEXT NOT NULL,
    statement_id            TEXT,                 -- TikTok's own statement_id for this SKU transaction, when present
    business_date           DATE,                  -- from the parent order's business_date (create_time-derived), not settlement check-date
    revenue_amount          NUMERIC(18,4),         -- this SKU transaction's own net revenue (cross-check denominator)
    fixed_fee               NUMERIC(18,4),         -- fee_tax_breakdown.fee.platform_commission_amount, AS RETURNED (sign preserved)
    payment_fee             NUMERIC(18,4),         -- fee_tax_breakdown.fee.transaction_fee_amount
    vxp_fee                 NUMERIC(18,4),         -- fee_tax_breakdown.fee.voucher_xtra_service_fee_amount
    infrastructure_fee      NUMERIC(18,4),         -- fee_tax_breakdown.fee.vn_fix_infrastructure_fee
    affiliate_fee           NUMERIC(18,4),         -- fee_tax_breakdown.fee.affiliate_commission_amount
    currency                TEXT,
    finance_state           TEXT NOT NULL,         -- MATCHED (has real sku_transactions) — this table only ever holds MATCHED rows
    value_basis             TEXT NOT NULL DEFAULT 'API_ACTUAL',
    source_system           TEXT NOT NULL DEFAULT 'TIKTOK',
    source_record_id        TEXT,                  -- order_id:sku_id, for traceability
    source_endpoint         TEXT NOT NULL DEFAULT '/finance/202501/orders/{order_id}/statement_transactions',
    source_updated_at       TIMESTAMPTZ,           -- when this row's evidence was fetched
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id              UUID,
    CONSTRAINT uq_fact_settlement_sku_fee UNIQUE (channel, shop_id, order_id, sku_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_settlement_sku_fee_business_date
    ON core.fact_settlement_sku_fee (channel, business_date);
