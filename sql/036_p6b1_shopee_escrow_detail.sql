-- =====================================================================
-- 036_p6b1_shopee_escrow_detail.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P6B.1 Shopee finance/escrow breakdown.
--
-- Root cause (P6B): Shopee's get_escrow_detail response already returns
-- every field HH_SHOPEE_PNL_FIELD_MAPPING_V1.md proved necessary for the
-- Net Sales/CM1 formula (order_original_price, seller_discount,
-- voucher_from_seller, seller_return_refund, commission_fee,
-- service_fee, seller_transaction_fee, actual_shipping_fee,
-- shopee_shipping_rebate, order_ams_commission_fee, ...) — but
-- incr_worker.py's run_finance (Shopee) only ever persisted the single
-- blended settlement_amount into core.fact_settlement. This migration
-- adds ONLY the Shopee-specific fields actually used by the proven
-- formula (mirrors the existing TikTok-specific columns
-- revenue_amount/fee_and_tax_amount/shipping_cost_amount added in P2B —
-- same table, same additive pattern, no duplicate structure).
--
-- Additive only, all nullable, no existing column touched.
-- =====================================================================

ALTER TABLE core.fact_settlement
    ADD COLUMN IF NOT EXISTS order_original_price                       NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS seller_discount                            NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS voucher_from_seller                        NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS voucher_from_shopee                        NUMERIC(18,4),  -- platform-funded, informational only, never subtracted from HH Net Sales
    ADD COLUMN IF NOT EXISTS seller_return_refund                       NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS drc_adjustable_refund                      NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS seller_lost_compensation                   NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS commission_fee                             NUMERIC(18,4),  -- Shopee "Fixed Fee"
    ADD COLUMN IF NOT EXISTS service_fee                                NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS seller_transaction_fee                     NUMERIC(18,4),  -- Shopee "Payment Fee"
    ADD COLUMN IF NOT EXISTS actual_shipping_fee                        NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS shopee_shipping_rebate                     NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS buyer_paid_shipping_fee                    NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS order_ams_commission_fee                   NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS ads_escrow_top_up_fee_or_technical_support_fee NUMERIC(18,4);
