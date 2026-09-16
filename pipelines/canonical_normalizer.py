"""
Canonical API-response normalizer — Phase 6A.

STATUS: STAGED. Not yet wired into production writes; used by local
tests and by the refactored collectors/backfill scripts in this same
pass. No production DB write happens by importing or calling this
module — every function here is a pure transform (dict/JSON in, dict
out) with no DB I/O.

Root cause this module fixes: RAW_ENRICHMENT_BACKFILL_BYPASSED_NORMALIZER
— a prior pattern (artifacts/v0/_p8_6_backfill_shopee_raw.py,
_p8_6_backfill_raw_breakdowns.py) fetched a fresh live API response and
wrote ONLY the raw JSONB column, silently leaving normalized structured
columns stale relative to a later API state. Going forward, ANY code
that has a raw API response in hand must call the matching normalize_*
function here to get BOTH the lossless raw payload AND the structured
fields from the SAME response, and write both in the SAME statement —
never one without the other.

Explicit-zero-vs-missing contract (applies to every numeric field this
module touches):
  - key absent from the dict                    -> None
  - value is JSON null (Python None)             -> None
  - value is "" (empty string)                   -> None (API sloppiness,
    not a valid number; treated as not-supplied, matching every
    Shopee/TikTok numeric field observed live this project — none use
    "" to mean a real zero)
  - value is 0, 0.0, "0", "0.00", "-0"           -> Decimal("0") (a REAL
    observed zero, never coerced to None, never skipped)
  - value is any other numeric string/number     -> Decimal(value)
No truthiness (`if value`, `value or default`) is used anywhere in this
module for a monetary/numeric field — every check is an explicit
`is None` / key-presence test.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def parse_numeric(value: Any) -> Optional[Decimal]:
    """The one function every numeric API field must pass through.
    See module docstring for the exact contract. Never uses truthiness."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def field_present(d: dict, key: str) -> bool:
    """True only if the key exists AND its value is not JSON null —
    i.e. true 'field was supplied' per the explicit-zero-vs-missing
    contract. Distinct from parse_numeric(d.get(key)) is not None,
    because a present-but-unparseable value should still be flagged as
    present-with-a-problem by a caller that cares, not silently folded
    into 'absent'."""
    return key in d and d[key] is not None


# =====================================================================
# SHOPEE — get_escrow_detail canonical normalizer
# =====================================================================

# Every field named in P8.7S's task text plus every field already
# mapped to a named CORE column prior to this pass (core.fact_settlement).
SHOPEE_ESCROW_STRUCTURED_FIELDS = [
    "order_original_price", "seller_discount", "voucher_from_seller", "voucher_from_shopee",
    "seller_return_refund", "drc_adjustable_refund", "seller_lost_compensation",
    "commission_fee", "service_fee", "seller_transaction_fee",
    "actual_shipping_fee", "shopee_shipping_rebate", "buyer_paid_shipping_fee",
    "order_ams_commission_fee", "ads_escrow_top_up_fee_or_technical_support_fee",
    # Newly added this pass — named in the task's required field list,
    # previously only reachable via order_income_raw:
    "final_shipping_fee", "final_return_to_seller_shipping_fee", "reverse_shipping_fee",
    "escrow_amount", "escrow_amount_after_adjustment", "total_adjustment_amount",
]


def normalize_shopee_escrow(order_income: dict, buyer_payment_info: Optional[dict] = None) -> dict:
    """Single source of truth for turning a get_escrow_detail response
    into CORE's structured fields. Returns a dict with:
      - 'structured': {field_name: Decimal|None, ...} for every field in
        SHOPEE_ESCROW_STRUCTURED_FIELDS
      - 'settlement_amount': Decimal|None (escrow_amount_after_adjustment,
        falling back to escrow_amount — the payout-reconciliation value;
        this fallback is a documented precedence choice, not a
        truthiness bug, since both are legitimate views of the same
        payout and the "after_adjustment" one is preferred when present)
      - 'order_income_raw': the input dict, unmodified (for lossless
        storage in the same statement)
      - 'buyer_payment_info_raw': ditto

    CRITICAL SEMANTIC (per task): buyer_payment_info is a checkout
    snapshot. This function NEVER reads from buyer_payment_info to
    derive any of the structured monetary fields above — every one of
    them comes from order_income, the dynamic/final accounting source.
    buyer_payment_info is returned only for lossless raw storage.
    """
    order_income = order_income or {}
    structured = {
        field: parse_numeric(order_income.get(field))
        for field in SHOPEE_ESCROW_STRUCTURED_FIELDS
    }
    settlement_amount = structured["escrow_amount_after_adjustment"]
    if settlement_amount is None:
        settlement_amount = structured["escrow_amount"]
    return {
        "structured": structured,
        "settlement_amount": settlement_amount,
        "order_income_raw": order_income,
        "buyer_payment_info_raw": buyer_payment_info,
    }


# =====================================================================
# TIKTOK — finance_order_statement_transactions (sku_transaction grain)
# canonical normalizer
# =====================================================================

# fee_tax_breakdown.fee sub-fields already proven live (P8.6/P8.7) to be
# the ONLY ones ever nonzero for this shop across the full Sep 1-15
# population, plus the 2 discovered-but-not-yet-named-column fields
# (affiliate_partner_commission_amount, tap_shop_ads_commission) which
# sql/057 (staged) would promote to named columns.
TIKTOK_FEE_STRUCTURED_MAP = {
    "fixed_fee": "platform_commission_amount",
    "payment_fee": "transaction_fee_amount",
    "vxp_fee": "voucher_xtra_service_fee_amount",
    "infrastructure_fee": "vn_fix_infrastructure_fee",
    "affiliate_fee": "affiliate_commission_amount",
    "affiliate_ads_commission_amount": "affiliate_ads_commission_amount",
    "affiliate_partner_commission_amount": "affiliate_partner_commission_amount",
    "tap_shop_ads_commission_amount": "tap_shop_ads_commission",
}

# fee_tax_breakdown.fee.affiliate_commission_amount_before_pit is a
# proven ALIAS_DUPLICATE of affiliate_commission_amount (identical on
# every nonzero row observed this project, VN PIT withholding always 0)
# — intentionally not a separate structured column; not double-counted.
TIKTOK_FEE_ALIAS_DUPLICATES = {"affiliate_commission_amount_before_pit": "affiliate_commission_amount"}

# Shipping formula proven exact (1103/1105, Phase-6A revalidation) —
# failed_delivery_subsidy_amount is a real, documented field, included
# unconditionally (0 when absent, per parse_numeric contract); NOT a
# balancing/invented term.
TIKTOK_SHIPPING_FORMULA_FIELDS = [
    "actual_shipping_fee_amount", "shipping_fee_discount_amount",
    "customer_paid_shipping_fee_amount", "failed_delivery_subsidy_amount",
]

# Revenue formula proven exact (1806/1806, P8.6/P8.7 full population).
TIKTOK_REVENUE_FORMULA_FIELDS = [
    "subtotal_before_discount_amount", "seller_discount_amount",
    "refund_subtotal_before_discount_amount", "seller_discount_refund_amount",
]


def normalize_tiktok_finance_sku(tx: dict) -> dict:
    """Single source of truth for turning one sku_transaction object
    (from finance_order_statement_transactions) into CORE's structured
    fields. Returns:
      - 'structured': {core_column_name: Decimal|None, ...} for every
        column in TIKTOK_FEE_STRUCTURED_MAP
      - 'computed_shipping_cost': Decimal|None — sum of
        TIKTOK_SHIPPING_FORMULA_FIELDS, None only if fee_tax/shipping
        data itself is entirely absent (never a fabricated 0)
      - 'computed_revenue': Decimal|None — sum of
        TIKTOK_REVENUE_FORMULA_FIELDS
      - 'fee_tax_breakdown_raw', 'revenue_breakdown_raw',
        'shipping_cost_breakdown_raw': the raw sub-objects, unmodified
    """
    fee_tax = tx.get("fee_tax_breakdown") or {}
    fee = fee_tax.get("fee") or {}
    revenue_breakdown = tx.get("revenue_breakdown") or {}
    shipping_breakdown = tx.get("shipping_cost_breakdown") or {}

    structured = {
        core_col: parse_numeric(fee.get(raw_field))
        for core_col, raw_field in TIKTOK_FEE_STRUCTURED_MAP.items()
    }

    if shipping_breakdown:
        parts = [parse_numeric(shipping_breakdown.get(f)) for f in TIKTOK_SHIPPING_FORMULA_FIELDS]
        computed_shipping = sum((p for p in parts if p is not None), Decimal(0))
    else:
        computed_shipping = None

    if revenue_breakdown:
        parts = [parse_numeric(revenue_breakdown.get(f)) for f in TIKTOK_REVENUE_FORMULA_FIELDS]
        computed_revenue = sum((p for p in parts if p is not None), Decimal(0))
    else:
        computed_revenue = None

    return {
        "structured": structured,
        "computed_shipping_cost": computed_shipping,
        "computed_revenue": computed_revenue,
        "fee_tax_breakdown_raw": tx.get("fee_tax_breakdown"),
        "revenue_breakdown_raw": tx.get("revenue_breakdown"),
        "shipping_cost_breakdown_raw": tx.get("shipping_cost_breakdown"),
    }
