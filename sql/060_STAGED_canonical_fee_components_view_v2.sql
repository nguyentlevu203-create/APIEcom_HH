-- =====================================================================
-- 060_STAGED_canonical_fee_components_view_v2.sql
-- STATUS: STAGED / NOT APPLIED to production — rehearsal-branch only.
-- REVISION 4 (Phase 6C — canonical coverage semantics correction).
--
-- BUG FIXED (proven live on rehearsal this pass): revision 3
-- (058_STAGED) computed ONE shared "completeness_status" per TikTok
-- business_date from ORDER-LEVEL core.fact_settlement.settlement_type=
-- 'MATCHED', then applied that SAME status to all 8 Finance-sourced
-- fee_codes regardless of which table/field each fee_code's own value
-- actually comes from. Proven wrong two ways:
--   (a) 127 rows across 34 dates (2026-03-01..08-31) showed
--       coverage_status='READY' with observed_amount=NULL — order-level
--       settlement was MATCHED but the SKU-level fee data (a DIFFERENT
--       table/ingestion path, core.fact_settlement_sku_fee) never
--       existed for those orders at all, or (for 3 newer CM2 fee_codes)
--       the underlying RAW JSONB was never captured pre-2026-09-01.
--   (b) 184 rows across 23 dates showed coverage_ratio=1 while
--       source_orders was NULL or less than eligible_orders — same root
--       cause, coverage_ratio was inherited from the shared CTE instead
--       of being computed from each fee_code's own source population.
--
-- FIX: coverage is now computed PER fee_code, from that fee_code's own
-- authoritative source:
--   - The 5 base CM1/CM2 fee_codes (platform_commission, transaction/
--     payment_fee, vxp_fee, infrastructure_fee, affiliate_commission)
--     read core.fact_settlement_sku_fee's own structured columns
--     directly — those columns are populated at ingestion time
--     regardless of raw JSONB retention, so "source_orders" = distinct
--     orders with a MATCHED sku_fee row where that column IS NOT NULL.
--   - The 3 newer CM2 fee_codes (affiliate_ads_commission,
--     affiliate_partner_commission, tap_shop_ads_commission) are ONLY
--     ever populated FROM fee_tax_breakdown_raw (proven live this
--     session: 0 rows have these structured values populated when raw
--     is NULL) — "source_orders" = distinct orders with a MATCHED
--     sku_fee row where fee_tax_breakdown_raw IS NOT NULL AND the
--     specific key is present in fee_tax_breakdown_raw->'fee' (also
--     proven live: whenever raw is present, all 3 keys are always
--     present, explicit zero when not applicable — 0 absent-key rows
--     found across the full TIKTOK population).
--
-- Field-present-with-explicit-zero COUNTS as observed (never treated as
-- missing); field genuinely absent does NOT count. observed_amount is
-- NULL only when source_orders=0 for that fee_code on that date (truly
-- nothing observed) — never fabricated as 0 in that case, and never
-- left NULL when source_orders>0 and every observed value happens to be
-- an explicit zero (SQL SUM() over a non-empty explicit-zero set
-- returns 0, not NULL, with no special-casing needed).
--
-- RECOVERABILITY (Phase 6C Section 2/3): a live, read-only, per-order
-- probe this session (GET /finance/202501/orders/{order_id}/
-- statement_transactions) against three real historical orders --
-- including order 582847443846137818, business_date 2026-03-01, the
-- OLDEST date with any TikTok Finance data in this shop's history --
-- proved TikTok's API STILL RETURNS full fee_tax_breakdown,
-- revenue_breakdown, AND shipping_cost_breakdown for that order TODAY.
-- This is Option B (recoverable by API re-fetch), NOT Option C. The
-- current-state gap below is therefore classified MISSING_SOURCE (data
-- genuinely absent from CORE right now), never HISTORICAL_SOURCE_LIMITED
-- (which this session reserves for a proven-unrecoverable case, which
-- this is not) and never READY. A backfill PLAN (not execution) is
-- documented separately in TIKTOK_HISTORICAL_RAW_BACKFILL_PLAN.csv,
-- per the explicit "build a backfill plan only" instruction.
--
-- Threshold: RAW_CAPTURE_START_DATE = 2026-09-01 (earliest date with any
-- fee_tax_breakdown_raw IS NOT NULL row in core.fact_settlement_sku_fee,
-- confirmed live this session). Used only as descriptive metadata below
-- (basis label), not to gate coverage_status — coverage_status is
-- driven purely by each fee_code's own source_orders/eligible_orders
-- ratio, per the instruction that coverage must be fee-source-specific
-- and never a date-threshold shortcut.
-- =====================================================================
CREATE OR REPLACE VIEW mart.v_ai_fee_components_daily AS

-- ---- SHOPEE — unchanged from revision 3 (not in scope for this fix;
--      no per-order Finance-eligibility gate exists for this channel). ----
SELECT business_date, channel, 'SHOPEE_FIXED_COMMISSION' AS fee_code,
       'Shopee fixed commission' AS fee_name, 'CM1' AS economic_layer,
       'API_ACTUAL' AS source_type, 'get_escrow_detail' AS source_name, 'commission_fee' AS source_field,
       fixed_fee AS observed_amount,
       fixed_fee AS expense_amount, CASE WHEN fixed_fee IS NULL THEN NULL ELSE 0::numeric END AS credit_amount, fixed_fee AS net_cost,
       NULL::bigint AS source_orders, NULL::bigint AS eligible_orders, NULL::numeric AS coverage_ratio,
       CASE WHEN fixed_fee IS NULL THEN 'MISSING_SOURCE' ELSE 'READY' END AS coverage_status,
       data_freshness_status AS freshness_status, net_sales_basis AS basis, source_updated_at AS last_source_date
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
UNION ALL
SELECT business_date, channel, 'SHOPEE_SERVICE_FEE', 'Shopee service fee (includes Voucher Xtra and the 3,000 VND infrastructure fee per business-owner-confirmed classification; subcomponent amounts not separately exposed by the API and are never estimated)', 'CM1',
       'API_ACTUAL', 'get_escrow_detail', 'service_fee',
       service_fee, service_fee, CASE WHEN service_fee IS NULL THEN NULL ELSE 0::numeric END, service_fee,
       NULL, NULL, NULL,
       CASE WHEN service_fee IS NULL THEN 'MISSING_SOURCE' ELSE 'READY' END,
       data_freshness_status, net_sales_basis, source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
UNION ALL
SELECT business_date, channel, 'SHOPEE_PAYMENT_TRANSACTION_FEE', 'Shopee payment/transaction fee', 'CM1',
       'API_ACTUAL', 'get_escrow_detail', 'seller_transaction_fee',
       payment_fee, payment_fee, CASE WHEN payment_fee IS NULL THEN NULL ELSE 0::numeric END, payment_fee,
       NULL, NULL, NULL,
       CASE WHEN payment_fee IS NULL THEN 'MISSING_SOURCE' ELSE 'READY' END,
       data_freshness_status, net_sales_basis, source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
UNION ALL
SELECT business_date, channel, 'SHOPEE_CANCEL_RETURN_LOGISTICS_NET', 'Shopee cancel/return logistics, netted', 'CM1',
       'API_ACTUAL', 'get_escrow_detail', 'actual_shipping_fee - shopee_shipping_rebate - buyer_paid_shipping_fee + seller_return_refund + drc_adjustable_refund + seller_lost_compensation',
       cancel_return_logistics_cost,
       CASE WHEN cancel_return_logistics_cost IS NULL THEN NULL WHEN cancel_return_logistics_cost >= 0 THEN cancel_return_logistics_cost ELSE 0 END,
       CASE WHEN cancel_return_logistics_cost IS NULL THEN NULL WHEN cancel_return_logistics_cost < 0 THEN -cancel_return_logistics_cost ELSE 0 END,
       cancel_return_logistics_cost,
       NULL, NULL, NULL,
       CASE WHEN cancel_return_logistics_cost IS NULL THEN 'MISSING_SOURCE' ELSE 'READY' END,
       data_freshness_status, net_sales_basis, source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
-- (No SHOPEE_VOUCHER_XTRA or SHOPEE_INFRASTRUCTURE branch -- both
-- intentionally absent; business-owner-confirmed included in
-- SHOPEE_SERVICE_FEE above. Unchanged from revision 3.)
UNION ALL
SELECT business_date, channel, 'SHOPEE_AFFILIATE_AMS', 'Shopee affiliate commission (AMS, order-level precedence)', 'CM2',
       'API_ACTUAL', 'ams/get_conversion_report + settlement order_ams_commission_fee', 'affiliate_commission',
       affiliate_commission, affiliate_commission, CASE WHEN affiliate_commission IS NULL THEN NULL ELSE 0::numeric END, affiliate_commission,
       NULL, NULL, NULL,
       affiliate_commission_basis, data_freshness_status, affiliate_commission_basis, source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
UNION ALL
SELECT business_date, channel, 'SHOPEE_ADS_SPEND', 'Shopee Ads spend', 'CM2',
       'API_ACTUAL', 'ads/get_all_cpc_ads_daily_performance', 'spend',
       ads_spend, ads_spend, CASE WHEN ads_spend IS NULL THEN NULL ELSE 0::numeric END, ads_spend,
       NULL, NULL, NULL,
       ads_spend_status, data_freshness_status, ads_spend_status, source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'
UNION ALL
SELECT business_date, channel, 'HH_OPERATIONAL_PACKAGING', 'HH internal operational packaging cost (policy-derived, not a ledger read)', 'HH_INTERNAL',
       'INTERNAL_POLICY_DERIVED', 'HH cost policy (no ledger system connected)', 'hh_operational_packaging_cost',
       hh_operational_packaging_cost, hh_operational_packaging_cost, CASE WHEN hh_operational_packaging_cost IS NULL THEN NULL ELSE 0::numeric END, hh_operational_packaging_cost,
       NULL, NULL, NULL,
       CASE WHEN hh_operational_packaging_cost IS NULL THEN 'MISSING_SOURCE' ELSE 'INTERNAL_POLICY_DERIVED' END,
       data_freshness_status, 'INTERNAL_POLICY_DERIVED', source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'SHOPEE'

-- ---- TIKTOK — Finance-domain fee_codes, coverage computed PER
--      fee_code from that fee_code's OWN source population. ----
UNION ALL
SELECT e.business_date, 'TIKTOK', x.fee_code, x.fee_name, x.economic_layer,
       'API_ACTUAL', 'finance_order_statement_transactions', x.source_field,
       x.observed_amount,
       CASE WHEN x.coverage_status = 'READY' THEN x.observed_amount ELSE NULL END AS expense_amount,
       CASE WHEN x.coverage_status = 'READY' THEN 0::numeric ELSE NULL END AS credit_amount,
       CASE WHEN x.coverage_status = 'READY' THEN x.observed_amount ELSE NULL END AS net_cost,
       x.source_orders, e.eligible_orders,
       CASE WHEN e.eligible_orders > 0 THEN round(x.source_orders::numeric / e.eligible_orders::numeric, 4) ELSE NULL END AS coverage_ratio,
       x.coverage_status,
       'FRESH' AS freshness_status,
       CASE WHEN x.coverage_status = 'READY' THEN 'SETTLEMENT_ACTUAL_COMPLETE'
            ELSE 'SETTLEMENT_ACTUAL_' || x.coverage_status END AS basis,
       x.last_source_date
FROM (
    -- eligible_orders: TikTok orders in a terminal status per business_date
    -- (the population that SHOULD eventually have Finance data). Kept as
    -- the shared denominator -- this part was never the bug; the bug was
    -- reusing an order-level "matched" NUMERATOR across all fee_codes.
    SELECT fo.business_date,
        count(*) FILTER (WHERE fo.order_status = ANY (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AS eligible_orders
    FROM core.fact_order fo
    WHERE fo.channel = 'TIKTOK'
    GROUP BY fo.business_date
) e
CROSS JOIN LATERAL (
    VALUES
    -- ---- Base 5: authoritative source = the structured column itself
    --      (populated at ingestion regardless of raw JSONB retention). ----
    ('TIKTOK_PLATFORM_COMMISSION', 'TikTok platform commission', 'CM1', 'fee_tax_breakdown.fee.platform_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fixed_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(fixed_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fixed_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fixed_fee IS NOT NULL AND business_date=e.business_date)),
    ('TIKTOK_TRANSACTION_PAYMENT_FEE', 'TikTok transaction/payment fee', 'CM1', 'fee_tax_breakdown.fee.transaction_fee_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND payment_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(payment_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND payment_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND payment_fee IS NOT NULL AND business_date=e.business_date)),
    ('TIKTOK_VOUCHER_XTRA_SERVICE_FEE', 'TikTok Voucher Xtra Program service fee', 'CM1', 'fee_tax_breakdown.fee.voucher_xtra_service_fee_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND vxp_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(vxp_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND vxp_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND vxp_fee IS NOT NULL AND business_date=e.business_date)),
    ('TIKTOK_INFRASTRUCTURE_FEE', 'TikTok VN fixed infrastructure fee', 'CM1', 'fee_tax_breakdown.fee.vn_fix_infrastructure_fee',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND infrastructure_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(infrastructure_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND infrastructure_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND infrastructure_fee IS NOT NULL AND business_date=e.business_date)),
    ('TIKTOK_AFFILIATE_COMMISSION', 'TikTok base affiliate commission (organic)', 'CM2', 'fee_tax_breakdown.fee.affiliate_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(affiliate_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_fee IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_fee IS NOT NULL AND business_date=e.business_date)),
    -- ---- 3 newer CM2 components: authoritative source = raw JSONB ONLY
    --      (proven live: structured column never populated without raw;
    --      key always present-with-explicit-zero when raw is present). ----
    ('TIKTOK_AFFILIATE_ADS_COMMISSION', 'TikTok affiliate ads commission (paid boost)', 'CM2', 'fee_tax_breakdown.fee.affiliate_ads_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_ads_commission_amount') AND business_date=e.business_date),
     (SELECT sum((fee_tax_breakdown_raw->'fee'->>'affiliate_ads_commission_amount')::numeric) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_ads_commission_amount') AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_ads_commission_amount') AND business_date=e.business_date)),
    ('TIKTOK_AFFILIATE_PARTNER_COMMISSION', 'TikTok affiliate partner-program commission', 'CM2', 'fee_tax_breakdown.fee.affiliate_partner_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_partner_commission_amount') AND business_date=e.business_date),
     (SELECT sum((fee_tax_breakdown_raw->'fee'->>'affiliate_partner_commission_amount')::numeric) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_partner_commission_amount') AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'affiliate_partner_commission_amount') AND business_date=e.business_date)),
    ('TIKTOK_TAP_SHOP_ADS_COMMISSION', 'TikTok Shop Ads commission', 'CM2', 'fee_tax_breakdown.fee.tap_shop_ads_commission',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'tap_shop_ads_commission') AND business_date=e.business_date),
     (SELECT sum((fee_tax_breakdown_raw->'fee'->>'tap_shop_ads_commission')::numeric) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'tap_shop_ads_commission') AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND (fee_tax_breakdown_raw->'fee' ? 'tap_shop_ads_commission') AND business_date=e.business_date))
) AS raw_x(fee_code, fee_name, economic_layer, source_field, source_orders, observed_amount, last_source_date)
CROSS JOIN LATERAL (
    SELECT raw_x.fee_code, raw_x.fee_name, raw_x.economic_layer, raw_x.source_field,
           raw_x.source_orders, raw_x.observed_amount, raw_x.last_source_date,
           CASE
               WHEN e.eligible_orders = 0 THEN 'NO_ELIGIBLE_ORDERS'
               WHEN raw_x.source_orders IS NULL OR raw_x.source_orders = 0 THEN 'MISSING_SOURCE'
               WHEN raw_x.source_orders::numeric / e.eligible_orders::numeric < 0.95 THEN 'SOURCE_LAGGING'
               ELSE 'READY'
           END AS coverage_status
) x

-- ---- TIKTOK — no per-order Finance-eligibility gate applies. ----
UNION ALL
SELECT business_date, channel, 'TIKTOK_SHIPPING_FULFILLMENT_NET', 'TikTok shipping/fulfillment fee, netted', 'CM1',
       'API_ACTUAL', 'core.fact_settlement (shipping_or_fulfillment_fees)', 'shipping_or_fulfillment_fees',
       platform_packaging_or_fulfillment_fee,
       CASE WHEN platform_packaging_or_fulfillment_fee IS NULL THEN NULL WHEN platform_packaging_or_fulfillment_fee >= 0 THEN platform_packaging_or_fulfillment_fee ELSE 0 END,
       CASE WHEN platform_packaging_or_fulfillment_fee IS NULL THEN NULL WHEN platform_packaging_or_fulfillment_fee < 0 THEN -platform_packaging_or_fulfillment_fee ELSE 0 END,
       platform_packaging_or_fulfillment_fee,
       NULL, NULL, NULL,
       CASE WHEN platform_packaging_or_fulfillment_fee IS NULL THEN 'SOURCE_LAGGING' ELSE 'READY' END,
       data_freshness_status, 'SETTLEMENT_ACTUAL_WHEN_READY', source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'TIKTOK'
UNION ALL
SELECT business_date, channel, 'TIKTOK_ADS_SPEND', 'TikTok Ads spend — Marketing API not connected', 'CM2',
       'SEPARATE_API_REQUIRED', NULL, NULL, NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric,
       NULL, NULL, NULL, 'SEPARATE_API_REQUIRED', 'FRESH', 'SEPARATE_API_REQUIRED', NULL
FROM mart.v_ceo_ecom_daily WHERE channel = 'TIKTOK'
UNION ALL
SELECT business_date, channel, 'HH_OPERATIONAL_PACKAGING', 'HH internal operational packaging cost (policy-derived, not a ledger read)', 'HH_INTERNAL',
       'INTERNAL_POLICY_DERIVED', 'HH cost policy (no ledger system connected)', 'hh_operational_packaging_cost',
       hh_operational_packaging_cost, hh_operational_packaging_cost, CASE WHEN hh_operational_packaging_cost IS NULL THEN NULL ELSE 0::numeric END, hh_operational_packaging_cost,
       NULL, NULL, NULL,
       CASE WHEN hh_operational_packaging_cost IS NULL THEN 'MISSING_SOURCE' ELSE 'INTERNAL_POLICY_DERIVED' END,
       data_freshness_status, 'INTERNAL_POLICY_DERIVED', source_updated_at
FROM mart.v_ceo_ecom_daily WHERE channel = 'TIKTOK';
