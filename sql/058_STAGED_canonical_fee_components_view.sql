-- =====================================================================
-- 058_STAGED_canonical_fee_components_view.sql
-- STATUS: STAGED / NOT APPLIED — pending human approval.
-- REVISION 3 (P8.7 final correction — partial-source semantics):
--
-- Revision 2 fixed "0 instead of NULL for a lagging source" by blanket-
-- nulling every TikTok CM1/affiliate fee whenever cm1_status =
-- 'SOURCE_LAGGING'. Live evidence proved that overcorrected: on
-- 2026-09-12, 38 of 54 eligible TikTok orders (70.4%) already have real,
-- non-zero, actually-received Finance data (observed_fixed_fee =
-- -539,443 VND) — blanket-nulling erased that already-arrived data
-- entirely, which is exactly the "erase a fee component whose actual
-- data has already arrived" failure mode.
--
-- Revision 3 introduces the observed/final split for every TikTok
-- Finance-sourced fee_code (the 5 CM1 components, TIKTOK_AFFILIATE_
-- COMMISSION, and the 3 staged CM2 candidates):
--   observed_amount = SUM of whatever has actually been received in
--     core.fact_settlement_sku_fee (finance_state='MATCHED') for that
--     date, exactly as-is — NEVER gated, NEVER erased for incompleteness.
--     NULL only when zero orders have been received at all (nothing
--     observed yet), which is different from "orders received, fee
--     happens to sum to 0" (that IS a real 0, correctly shown as 0).
--   expense_amount / credit_amount / net_cost = the FINAL, P&L-eligible
--     amount — NULL unless source_orders/eligible_orders indicates the
--     date's Finance coverage is COMPLETE. This is what any P&L
--     consumer (mart.v_ceo_ecom_daily, MCP, Agent) must read — never
--     observed_amount.
--   source_orders = distinct orders represented in the Finance source
--     for that date (core.fact_settlement_sku_fee, MATCHED).
--   eligible_orders = TikTok orders in a terminal status (DELIVERED/
--     COMPLETED/CANCELLED) for that date — the population that SHOULD
--     eventually have Finance data (core.fact_order), independently
--     re-derived here rather than trusted from mart.v_ceo_ecom_daily's
--     precomputed tiktok_eligible_orders (cross-checked equal in
--     testing, but this view computes it directly from core so it does
--     not blindly depend on that column existing/being correct).
--   coverage_ratio = source_orders / eligible_orders.
--   coverage_status = READY only when coverage_ratio >= 0.95 (matching
--     the existing COMPLETE_SETTLEMENT_COVERAGE threshold in sql/051);
--     MISSING_SOURCE when eligible_orders=0 or source_orders=0;
--     SOURCE_LAGGING otherwise (partial coverage) — derived HERE from
--     the Finance-domain order counts directly, not borrowed from the
--     generic cm1_status column (which mixes in an unrelated
--     pending-order-ratio condition that can label a date SOURCE_LAGGING
--     even when its Finance coverage is close to complete, or vice
--     versa for other CM1 dependencies in the future).
--
-- Shopee rows and TikTok rows with no per-order Finance-eligibility
-- concept (ads_spend, HH internal cost) keep the revision-2 NULL-safe
-- expense/credit/net_cost logic with observed_amount = net_cost
-- (single-source fields have no separate observed/final distinction to
-- make) and source_orders/eligible_orders/coverage_ratio = NULL (not
-- meaningful for those fee_codes).
-- =====================================================================
CREATE OR REPLACE VIEW mart.v_ai_fee_components_daily AS

-- ---- SHOPEE — no per-order Finance-eligibility gate exists for this
--      channel today; observed_amount mirrors the final amount. ----
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
-- P8.7 FINAL BUSINESS OWNER CONFIRMATION: Voucher Xtra/VXP and the
-- 3,000 VND infrastructure fee are both already included inside
-- Shopee's API-reported service_fee for this shop (confirmed by the
-- business owner as the approved accounting classification for HH —
-- not independently re-derived from API evidence, which never exposed
-- either subcomponent separately). service_fee itself is completely
-- unchanged: same API_ACTUAL value, same CM1 layer, same source_field.
-- This fee_name annotation exists purely so a reader (human or Agent)
-- never mistakes the absence of separate VXP/infrastructure rows for a
-- capture gap — they are accounted-for subcomponents of this line, not
-- missing data.
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
-- P8.7 FINAL BUSINESS OWNER CONFIRMATION (supersedes the prior
-- NOT_SEPARATELY_REPORTED closure): Shopee Voucher Xtra/VXP AND the
-- 3,000 VND Shopee infrastructure fee are both confirmed by the
-- business owner to already be included inside the API's service_fee
-- for this shop. This is the approved accounting classification for HH
-- — a business/accounting determination, not something independently
-- re-derived from API evidence this session (no endpoint ever exposed
-- either subcomponent separately; that investigation is documented in
-- the P8.7/P8.7S session history and is not repeated here).
--
-- Consequences enforced by this view's structure (not just documented):
--   - SHOPEE_VOUCHER_XTRA_CANONICAL_FEE_CODE = NONE,
--     SHOPEE_INFRASTRUCTURE_CANONICAL_FEE_CODE = NONE — neither has its
--     own row below. There is exactly one Shopee CM1 line for this
--     economic content: SHOPEE_SERVICE_FEE above.
--   - No estimated/derived amount for either subcomponent exists
--     anywhere in this view (no percentage-of-something formula, no
--     3,000-per-order flat multiply, no residual-based derivation).
--   - Double-count protection: nothing in this view or in
--     mart.v_ceo_ecom_daily ever computes service_fee + VXP +
--     infrastructure — service_fee is read and exposed exactly once,
--     as a single CM1 line, above.
--   - Agent-facing answer for "Shopee VXP bao nhiêu?" / "Phí hạ tầng
--     Shopee bao nhiêu?": both are included in Shopee service_fee; a
--     separate amount is not exposed by the current API.
-- (No SHOPEE_VOUCHER_XTRA or SHOPEE_INFRASTRUCTURE UNION branch below —
-- both intentionally absent, per the above.)
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

-- ---- TIKTOK — Finance-domain completeness computed once, shared by
--      every Finance-sourced fee_code below.
--
-- REVISION 4 (this pass): the completeness CASE below is a byte-for-byte
-- copy of production's tiktok_completeness CTE (sql/051_p8_5_shopee_
-- affiliate_cm2.sql), including the branch order and the exact 15%/95%
-- thresholds — not a simplified coverage_ratio>=0.95 approximation
-- (revision 3's shortcut, which happened to agree with production on
-- this window but skipped the pending-orders-ratio branch and the
-- NOT_SETTLED_YET/NO_ELIGIBLE_ORDERS distinction entirely, so it would
-- have silently diverged on a date where those branches matter).
-- eligible_orders/pending_orders/matched_eligible_orders are computed
-- from core.fact_order LEFT JOIN core.fact_settlement on the natural key
-- (channel, shop_id, order_id) — exactly as production joins them, with
-- no date filter on fs (matching production; fs's own business_date
-- represents the settlement-check date, not the order's date, so it is
-- never used for grouping — only fo.business_date is).
-- source_orders (the count actually used to size observed_amount, from
-- core.fact_settlement_sku_fee) is cross-validated equal to
-- matched_eligible_orders on all 15 audited dates (2026-09-01..09-15) —
-- both counts come from the same underlying "has_real" determination in
-- incr_worker.py's run_finance(), just against two different tables at
-- two different grains.
UNION ALL
SELECT o.business_date, 'TIKTOK', x.fee_code, x.fee_name, x.economic_layer,
       'API_ACTUAL', 'finance_order_statement_transactions', x.source_field,
       x.observed,
       CASE WHEN o.completeness_status = 'COMPLETE_SETTLEMENT_COVERAGE' THEN x.observed ELSE NULL END AS expense_amount,
       CASE WHEN o.completeness_status = 'COMPLETE_SETTLEMENT_COVERAGE' THEN 0::numeric ELSE NULL END AS credit_amount,
       CASE WHEN o.completeness_status = 'COMPLETE_SETTLEMENT_COVERAGE' THEN x.observed ELSE NULL END AS net_cost,
       s.source_orders, o.eligible_orders, o.coverage_pct / 100.0 AS coverage_ratio,
       CASE WHEN o.completeness_status = 'COMPLETE_SETTLEMENT_COVERAGE' THEN 'READY' ELSE o.completeness_status END AS coverage_status,
       'FRESH' AS freshness_status,
       CASE WHEN o.completeness_status = 'COMPLETE_SETTLEMENT_COVERAGE' THEN 'SETTLEMENT_ACTUAL_COMPLETE'
            ELSE 'SETTLEMENT_ACTUAL_' || o.completeness_status END AS basis,
       s.last_upd AS last_source_date
FROM (
    -- Exact copy of production's tiktok_fee_pop + tiktok_completeness
    -- (sql/051), branch order and thresholds unchanged.
    SELECT tiktok_fee_pop.business_date,
        tiktok_fee_pop.total_orders,
        tiktok_fee_pop.eligible_orders,
        tiktok_fee_pop.pending_orders,
        tiktok_fee_pop.matched_eligible_orders,
        CASE WHEN tiktok_fee_pop.eligible_orders > 0
             THEN round(100.0 * tiktok_fee_pop.matched_eligible_orders::numeric / tiktok_fee_pop.eligible_orders::numeric, 1)
             ELSE NULL::numeric END AS coverage_pct,
        CASE
            WHEN tiktok_fee_pop.total_orders = 0 THEN 'NO_ELIGIBLE_ORDERS'
            WHEN tiktok_fee_pop.eligible_orders = 0 THEN 'SOURCE_LAGGING'
            WHEN (tiktok_fee_pop.pending_orders::numeric / tiktok_fee_pop.total_orders::numeric) > 0.15 THEN 'SOURCE_LAGGING'
            WHEN tiktok_fee_pop.matched_eligible_orders = 0 THEN 'NOT_SETTLED_YET'
            WHEN (tiktok_fee_pop.matched_eligible_orders::numeric / tiktok_fee_pop.eligible_orders::numeric) < 0.95 THEN 'PARTIAL_SETTLEMENT_COVERAGE'
            ELSE 'COMPLETE_SETTLEMENT_COVERAGE'
        END AS completeness_status
    FROM (
        SELECT fo.business_date,
            count(*) AS total_orders,
            count(*) FILTER (WHERE fo.order_status = ANY (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AS eligible_orders,
            count(*) FILTER (WHERE fo.order_status <> ALL (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AS pending_orders,
            count(*) FILTER (WHERE (fo.order_status = ANY (ARRAY['DELIVERED','COMPLETED','CANCELLED'])) AND fs.settlement_type = 'MATCHED') AS matched_eligible_orders
        FROM core.fact_order fo
        LEFT JOIN core.fact_settlement fs ON fs.channel = fo.channel AND fs.shop_id = fo.shop_id AND fs.order_id = fo.order_id
        WHERE fo.channel = 'TIKTOK'
        GROUP BY fo.business_date
    ) tiktok_fee_pop
) o
LEFT JOIN (
    SELECT business_date, count(DISTINCT order_id) AS source_orders, max(source_updated_at) AS last_upd
    FROM core.fact_settlement_sku_fee
    WHERE channel = 'TIKTOK' AND finance_state = 'MATCHED'
    GROUP BY business_date
) s ON s.business_date = o.business_date
CROSS JOIN LATERAL (
    VALUES
    ('TIKTOK_PLATFORM_COMMISSION', 'TikTok platform commission', 'CM1', 'fee_tax_breakdown.fee.platform_commission_amount',
     (SELECT sum(fixed_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_TRANSACTION_PAYMENT_FEE', 'TikTok transaction/payment fee', 'CM1', 'fee_tax_breakdown.fee.transaction_fee_amount',
     (SELECT sum(payment_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_VOUCHER_XTRA_SERVICE_FEE', 'TikTok Voucher Xtra Program service fee', 'CM1', 'fee_tax_breakdown.fee.voucher_xtra_service_fee_amount',
     (SELECT sum(vxp_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_INFRASTRUCTURE_FEE', 'TikTok VN fixed infrastructure fee', 'CM1', 'fee_tax_breakdown.fee.vn_fix_infrastructure_fee',
     (SELECT sum(infrastructure_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_AFFILIATE_COMMISSION', 'TikTok base affiliate commission (organic)', 'CM2', 'fee_tax_breakdown.fee.affiliate_commission_amount',
     (SELECT sum(affiliate_fee) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_AFFILIATE_ADS_COMMISSION', 'TikTok affiliate ads commission (paid boost)', 'CM2', 'fee_tax_breakdown.fee.affiliate_ads_commission_amount',
     (SELECT sum(affiliate_ads_commission_amount) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND business_date=o.business_date)),
    ('TIKTOK_AFFILIATE_PARTNER_COMMISSION', 'TikTok affiliate partner-program commission', 'CM2', 'fee_tax_breakdown.fee.affiliate_partner_commission_amount',
     (SELECT sum((fee_tax_breakdown_raw->'fee'->>'affiliate_partner_commission_amount')::numeric) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND business_date=o.business_date)),
    ('TIKTOK_TAP_SHOP_ADS_COMMISSION', 'TikTok Shop Ads commission', 'CM2', 'fee_tax_breakdown.fee.tap_shop_ads_commission',
     (SELECT sum((fee_tax_breakdown_raw->'fee'->>'tap_shop_ads_commission')::numeric) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND fee_tax_breakdown_raw IS NOT NULL AND business_date=o.business_date))
) AS x(fee_code, fee_name, economic_layer, source_field, observed)

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
