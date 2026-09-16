-- =====================================================================
-- 034_p6a_gold_precision.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P6A Gold foundation, precision widen.
--
-- mart.gold_channel_daily.metric_value was NUMERIC(18,4) since P1. P5B
-- widened core.dim_cogs.unit_cost to NUMERIC(24,12) to avoid rounding
-- the source workbook's up-to-11-decimal costs. Storing that
-- full-precision COGS into a NUMERIC(18,4) Gold column rounded each row
-- to 4 decimals, which then accumulated into a ~0.0005 VND reconciliation
-- gap against the P5 production total (182,187,971.5352674847... vs
-- 182,187,971.5358 — immaterial financially, but not an exact match).
-- Widening precision on an existing column is additive/safe (no data
-- loss, table only contained this session's own rows at the time of this
-- migration — applied with explicit user confirmation via neondb_owner,
-- same pattern as 032's dim_cogs widen).
-- =====================================================================

ALTER TABLE mart.gold_channel_daily
    ALTER COLUMN metric_value TYPE NUMERIC(24,12);
