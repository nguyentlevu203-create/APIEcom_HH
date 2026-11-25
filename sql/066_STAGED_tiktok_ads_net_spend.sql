-- =====================================================================
-- 066_STAGED_tiktok_ads_net_spend.sql
-- P15 — TikTok ads cost collector (TikTok API for Business).
--
-- STAGED: not applied. Requires owner approval before running against
-- production (additive only — one nullable column, no data rewrite).
--
-- core.fact_ads_daily gains `net_spend`: GMV Max reports both `cost`
-- (gross ad cost, stored in the existing `spend` column and used for
-- Gold ads_spend / CM2) and `net_cost` (cost after TikTok's ROI-protection
-- compensation credit). Both are kept so HH can choose the basis later
-- without re-pulling history. NULL for Shopee and for TikTok AUCTION rows.
--
-- New TikTok row kinds (attribution_basis), all channel='TIKTOK',
-- source_system='TIKTOK_BUSINESS':
--   GMV_MAX  — per advertiser x GMV Max campaign x day, shop_id = store_id
--   AUCTION  — per advertiser x auction campaign x day, shop_id NULL
--              (not store-linked; excluded from Gold ads_spend)
--   COVERAGE — one marker per store x day (ad_account_id='STORE:<id>',
--              campaign_id='__COVERAGE__', spend 0) = "collector covered
--              this day"; lets Gold report a real 0 instead of MISSING.
-- =====================================================================

ALTER TABLE core.fact_ads_daily
    ADD COLUMN IF NOT EXISTS net_spend NUMERIC(18,4);

COMMENT ON COLUMN core.fact_ads_daily.net_spend IS
    'P15 TikTok GMV Max net_cost (cost after ROI-protection compensation). spend = gross cost.';
