-- =====================================================================
-- 030_p2b_tiktok_additive.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P2B TikTok Shop additive migration.
--
-- Additive only: adds nullable columns to existing tables so TikTok's
-- finance/return fields (not present in the Shopee-only P1 schema) can
-- be preserved without inventing values. No DROP, no rewrite of any
-- existing column, no existing row touched by this file.
--
-- Rerun-safe: IF NOT EXISTS everywhere.
-- =====================================================================

-- core.fact_settlement: TikTok's Order Statement Transactions endpoint
-- returns a revenue/fee/shipping breakdown (and a currency) that Shopee's
-- escrow endpoint does not expose in the same shape. settlement_amount
-- alone cannot distinguish "not yet settled" from "settled at 0" once a
-- currency-qualified breakdown exists, so these are additive, not a
-- reuse of settlement_amount.
ALTER TABLE core.fact_settlement
    ADD COLUMN IF NOT EXISTS currency             TEXT,
    ADD COLUMN IF NOT EXISTS revenue_amount        NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS fee_and_tax_amount     NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS shipping_cost_amount   NUMERIC(18,4);

-- core.fact_return_refund: P1 had no currency column (Shopee return
-- evidence didn't require one); TikTok's return_refund/returns/search
-- response carries refund_amount.currency explicitly.
ALTER TABLE core.fact_return_refund
    ADD COLUMN IF NOT EXISTS currency TEXT;
