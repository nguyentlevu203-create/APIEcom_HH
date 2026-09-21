-- =====================================================================
-- 063_STAGED_canonical_fee_components_view_v3.sql
-- STATUS: STAGED / NOT APPLIED to production — rehearsal-branch only.
-- REVISION 5 (Phase 6C, 2026-09-21) — supersedes sql/060 (revision 4).
--   060 = SUPERSEDED_BY_063
--   062 = INCORPORATED_INTO_063 (062 was a diff-only addendum; this file
--         is the full, directly-executable view it described)
--
-- CHANGE FROM REVISION 4 (sql/060): exactly 2 of 8 TikTok Finance-domain
-- fee_codes — TIKTOK_AFFILIATE_PARTNER_COMMISSION and
-- TIKTOK_TAP_SHOP_ADS_COMMISSION — now read from
-- core.fact_settlement_sku_transaction (sql/061, the lossless
-- transaction-grain table) instead of fee_tax_breakdown_raw on
-- core.fact_settlement_sku_fee. Root cause: TikTok can return multiple
-- sku_transaction entries sharing one (order_id, sku_id) (proven live,
-- order 585474165265106454); fact_settlement_sku_fee's raw column can
-- only hold ONE representative entry post-aggregation, so summing raw
-- off that table for these 2 fee_codes would silently undercount the
-- exact same way the pre-fix revenue/shipping totals did. The other 6
-- fee_codes read fact_settlement_sku_fee's STRUCTURED columns, which
-- are now written by summing every row in fact_settlement_sku_transaction
-- for that (order_id,sku_id) — no SQL text change needed for those 6,
-- they inherit the fix once the write path (incr_worker.py) is deployed.
--
-- DEPENDS ON: sql/061 existing AND being populated by the updated write
-- path. Applying this before either exists just makes these 2 fee_codes
-- report 0/NULL (MISSING_SOURCE), not wrong-but-nonzero data — fails
-- safe, but sequence sql/061 + write-path deploy BEFORE this file.
--
-- Everything below revision 4's original header/body is otherwise
-- UNCHANGED — see sql/060 for the full original rationale (058→060 bug
-- fix history, coverage-status semantics, RAW_CAPTURE_START_DATE note).
-- =====================================================================
CREATE OR REPLACE VIEW mart.v_ai_fee_components_daily AS

-- ---- SHOPEE — unchanged from revision 4 (not in scope for this fix;
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
-- SHOPEE_SERVICE_FEE above. Unchanged from revision 4.)
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
    -- ---- 3 newer CM2 components: TIKTOK_AFFILIATE_ADS_COMMISSION keeps
    --      reading fact_settlement_sku_fee's raw column (unaffected --
    --      affiliate_ads_commission_amount has been a named STRUCTURED
    --      column since sql/054, not raw-only, so it already inherits
    --      the write-path fix like the base 5 above). The other 2 below
    --      are the ones changed in this revision. ----
    ('TIKTOK_AFFILIATE_ADS_COMMISSION', 'TikTok affiliate ads commission (paid boost)', 'CM2', 'fee_tax_breakdown.fee.affiliate_ads_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_ads_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(affiliate_ads_commission_amount) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_ads_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_fee WHERE channel='TIKTOK' AND finance_state='MATCHED' AND affiliate_ads_commission_amount IS NOT NULL AND business_date=e.business_date)),
    -- REVISION 5 CHANGE: read from the lossless transaction-grain table
    -- (sql/061), not fee_tax_breakdown_raw on fact_settlement_sku_fee --
    -- see this file's header. source_orders/observed_amount now sum
    -- across every source transaction, never just the last-surviving one.
    ('TIKTOK_AFFILIATE_PARTNER_COMMISSION', 'TikTok affiliate partner-program commission', 'CM2', 'fee_tax_breakdown.fee.affiliate_partner_commission_amount',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND affiliate_partner_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(affiliate_partner_commission_amount) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND affiliate_partner_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND affiliate_partner_commission_amount IS NOT NULL AND business_date=e.business_date)),
    ('TIKTOK_TAP_SHOP_ADS_COMMISSION', 'TikTok Shop Ads commission', 'CM2', 'fee_tax_breakdown.fee.tap_shop_ads_commission',
     (SELECT count(DISTINCT order_id) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND tap_shop_ads_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT sum(tap_shop_ads_commission_amount) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND tap_shop_ads_commission_amount IS NOT NULL AND business_date=e.business_date),
     (SELECT max(source_updated_at) FROM core.fact_settlement_sku_transaction WHERE channel='TIKTOK' AND tap_shop_ads_commission_amount IS NOT NULL AND business_date=e.business_date))
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
