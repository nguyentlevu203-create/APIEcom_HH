-- =====================================================================
-- 056_p8_6_shopee_raw_order_income_capture.sql
-- P8.6 — same structural fix as sql/055, for Shopee. get_escrow_detail's
-- order_income has 80 fields (25-order live sample this pass); only 15
-- are persisted as named columns. Most of the rest are proven either
-- market-not-applicable (Brazil/Thailand/Mexico tax/cross-border fields,
-- always 0 for this VN shop) or duplicates/components of already-mapped
-- fields (proven via live per-order equality checks this pass), but a
-- handful (buyer_total_amount, coins, estimated_shipping_fee,
-- order_seller_discount, original_price, plus the entire
-- buyer_payment_info block) are real, distinct, currently-uncaptured
-- fields whose accounting role is not yet certain.
--
-- Persist order_income and buyer_payment_info verbatim as JSONB so no
-- current or future Shopee escrow field is silently lost, without a
-- migration each time a new one is found live (the same lesson TikTok's
-- statement-level reconciliation just proved: small live samples miss
-- rare fields).
--
-- Additive only. No existing column touched, no existing value changed.
-- =====================================================================
ALTER TABLE core.fact_settlement
    ADD COLUMN IF NOT EXISTS order_income_raw JSONB,
    ADD COLUMN IF NOT EXISTS buyer_payment_info_raw JSONB;
