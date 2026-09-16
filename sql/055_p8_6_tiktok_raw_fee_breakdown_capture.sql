-- =====================================================================
-- 055_p8_6_tiktok_raw_fee_breakdown_capture.sql
-- P8.6 — structural fix for "không bỏ sót field nào" on TikTok's
-- finance_order_statement_transactions response.
--
-- Evidence this pass: fee_tax_breakdown.fee has 48 sub-fields; only 5
-- were ever persisted as named columns (fixed_fee/payment_fee/vxp_fee/
-- infrastructure_fee/affiliate_fee). Statement-level reconciliation
-- (Get Statements .fee_amount vs SUM of mapped components) found a real,
-- live, unreconciled gap on every one of 3 statements checked. Adding
-- affiliate_ads_commission_amount (sql/054) reduced it; a full-population
-- re-check found a SECOND previously-unknown live field,
-- tap_shop_ads_commission (1/98 sku rows, -15,600 VND), and a residual
-- gap still remained after that too — proving a small live sample will
-- always miss some rare field, and adding one named column at a time
-- cannot converge.
--
-- Structural fix: persist the ENTIRE fee_tax_breakdown, revenue_breakdown,
-- and shipping_cost_breakdown objects verbatim as JSONB alongside the
-- existing named columns. This guarantees zero future field loss for
-- this endpoint without another migration every time a new sub-field is
-- found — the named columns remain the P&L-facing summary, the JSONB is
-- the lossless audit/reconciliation source of truth.
--
-- Additive only. No existing column touched, no existing value changed.
-- =====================================================================
ALTER TABLE core.fact_settlement_sku_fee
    ADD COLUMN IF NOT EXISTS fee_tax_breakdown_raw JSONB,
    ADD COLUMN IF NOT EXISTS revenue_breakdown_raw JSONB,
    ADD COLUMN IF NOT EXISTS shipping_cost_breakdown_raw JSONB;
