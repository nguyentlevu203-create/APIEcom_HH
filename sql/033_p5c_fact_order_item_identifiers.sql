-- =====================================================================
-- 033_p5c_fact_order_item_identifiers.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P5C fact_order_item platform-identifier
-- closure.
--
-- Root cause (confirmed in P5B's production recompute, 2026-09-12):
-- core.fact_order_item.sku is the ONLY platform identifier ever
-- persisted for an order-item line. For Shopee, the ingest path
-- (integrations/shopee/pilot_reporting/incr_worker.py::run_orders) only
-- ever read item_list[].model_sku, so any line where Shopee's own
-- get_order_detail response has model_id=0 (no real variation) and
-- model_sku empty was written with sku='' — even though Shopee's
-- response separately carries item_sku, item_id, and model_id for that
-- same line. core.map_platform_product already has 8 rows correctly
-- sourced from item_sku (loaded in P5B from P5A.1 evidence), but they
-- were unreachable because fact_order_item never stored a joinable key
-- for those lines and had no bridge column to recover one after the
-- fact.
--
-- This migration is purely additive: 6 new nullable TEXT columns on
-- core.fact_order_item, no existing column altered, no row touched, no
-- DROP. Two are Shopee-specific raw source fields (item_sku, model_sku)
-- kept alongside item_id/model_id so the *resolved* value written to the
-- existing `sku` column can always be traced back to which field it came
-- from; two are TikTok-specific (platform_product_id, platform_sku_id —
-- named generically, not item_id/model_id again, since TikTok's own
-- catalog identifiers are a distinct concept from Shopee's and TikTok's
-- equivalent of "seller_sku" is already captured correctly in the
-- existing `sku` column, so no TikTok-side seller_sku column is added
-- here — would be a redundant duplicate).
--
-- Rerun-safe: IF NOT EXISTS everywhere.
-- =====================================================================

ALTER TABLE core.fact_order_item
    -- Shopee raw source identifiers (item_list[] fields from
    -- GET /api/v2/order/get_order_detail), preserved alongside the
    -- resolved `sku` so the resolution can always be audited.
    ADD COLUMN IF NOT EXISTS item_id               TEXT,
    ADD COLUMN IF NOT EXISTS model_id              TEXT,
    ADD COLUMN IF NOT EXISTS item_sku              TEXT,
    ADD COLUMN IF NOT EXISTS model_sku             TEXT,
    -- TikTok raw source identifiers (line_items[] fields from
    -- GET /order/202309/orders/{order_id}), not previously persisted.
    ADD COLUMN IF NOT EXISTS platform_product_id   TEXT,
    ADD COLUMN IF NOT EXISTS platform_sku_id       TEXT;
