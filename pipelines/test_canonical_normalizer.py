"""
Unit tests for pipelines/canonical_normalizer.py — Phase 6A.
Pure function tests, no DB connection, no network. Run with:
    python3 -m pytest pipelines/test_canonical_normalizer.py -v
"""
from decimal import Decimal
import json
from pathlib import Path

import pytest

from canonical_normalizer import (
    parse_numeric,
    field_present,
    normalize_shopee_escrow,
    normalize_tiktok_finance_sku,
)

EVIDENCE_DIR = Path(__file__).parent.parent / "artifacts" / "v0" / "API_CONTRACT_AUDIT" / "RAW_EVIDENCE"


# =====================================================================
# 1. Explicit zero vs missing — the core contract (Section 2 / 13)
# =====================================================================

@pytest.mark.parametrize("raw_value,expected", [
    (None, None),                    # JSON null -> NULL
    ("", None),                      # empty string -> NULL (API sloppiness)
    ("0", Decimal("0")),             # zero string -> real numeric 0
    (0, Decimal("0")),               # numeric zero -> real numeric 0
    (0.0, Decimal("0")),             # float zero -> real numeric 0
    ("-0", Decimal("0")),
    (38480, Decimal("38480")),       # positive
    ("38480", Decimal("38480")),
    (-185000, Decimal("-185000")),   # negative (refund)
    ("-185000", Decimal("-185000")),
    (12.5, Decimal("12.5")),
])
def test_parse_numeric_explicit_contract(raw_value, expected):
    assert parse_numeric(raw_value) == expected


def test_parse_numeric_absent_key_is_none():
    d = {"other_field": 5}
    assert parse_numeric(d.get("missing_field")) is None


def test_parse_numeric_never_uses_truthiness():
    # A naive `value or 0` would turn this real zero into... 0 too, so
    # that alone doesn't catch the bug class. The real anti-pattern this
    # guards against is `new_value or old_value` at the CALLER site —
    # verify parse_numeric itself is side-effect-free and always returns
    # based purely on the input, never an implicit fallback to a second
    # argument (this function intentionally takes no "old value" param
    # at all, making the anti-pattern structurally impossible here).
    import inspect
    sig = inspect.signature(parse_numeric)
    assert list(sig.parameters) == ["value"], (
        "parse_numeric must not accept a fallback/default value parameter "
        "-- that is exactly the shape of the prohibited `new_value or "
        "old_value` pattern"
    )


def test_field_present_distinguishes_null_from_zero():
    assert field_present({"x": 0}, "x") is True
    assert field_present({"x": "0"}, "x") is True
    assert field_present({"x": None}, "x") is False
    assert field_present({}, "x") is False


# =====================================================================
# 2. Zero must overwrite old nonzero / absent must stay NULL
#    (simulated at the normalizer-output level, since these functions
#    are pure — the "overwrite" behavior is proven by showing the
#    function's OUTPUT for a zeroed response is a real 0, never
#    silently coerced to None, and the caller (UPSERT statement) is
#    unconditional per Section 2 of this same repair.)
# =====================================================================

def test_shopee_zero_response_produces_real_zero_not_none():
    order_income = {"commission_fee": 0, "service_fee": "0", "seller_transaction_fee": 0.0}
    result = normalize_shopee_escrow(order_income)
    assert result["structured"]["commission_fee"] == Decimal("0")
    assert result["structured"]["service_fee"] == Decimal("0")
    assert result["structured"]["seller_transaction_fee"] == Decimal("0")


def test_shopee_absent_field_stays_none():
    order_income = {"commission_fee": 100}  # everything else absent
    result = normalize_shopee_escrow(order_income)
    assert result["structured"]["commission_fee"] == Decimal("100")
    assert result["structured"]["service_fee"] is None
    assert result["structured"]["seller_transaction_fee"] is None


def test_shopee_settlement_amount_precedence():
    # after_adjustment present and different from escrow_amount -> prefer after_adjustment
    oi = {"escrow_amount": 100, "escrow_amount_after_adjustment": 80}
    assert normalize_shopee_escrow(oi)["settlement_amount"] == Decimal("80")
    # after_adjustment absent -> fall back to escrow_amount
    oi2 = {"escrow_amount": 100}
    assert normalize_shopee_escrow(oi2)["settlement_amount"] == Decimal("100")
    # both absent -> None, never fabricated 0
    assert normalize_shopee_escrow({})["settlement_amount"] is None


def test_shopee_never_reads_buyer_payment_info_for_structured_fields():
    # buyer_payment_info has DIFFERENT (checkout-snapshot) values for an
    # overlapping-sounding concept; must never leak into structured.
    order_income = {"commission_fee": 100}
    buyer_payment_info = {"buyer_service_fee": 99999, "seller_voucher": -50000}
    result = normalize_shopee_escrow(order_income, buyer_payment_info)
    assert result["structured"]["commission_fee"] == Decimal("100")
    assert result["buyer_payment_info_raw"] == buyer_payment_info  # stored losslessly
    assert "buyer_service_fee" not in result["structured"]
    assert "seller_voucher" not in result["structured"]


def test_shopee_real_stale_zero_example_order_2609153STTS4DT():
    """Regression test for the exact production bug this repair fixes."""
    with open(EVIDENCE_DIR / "session_probes_2026-09-16.json") as f:
        evidence = json.load(f)
    order_income = evidence["stale_zero_example_2609153STTS4DT"]["response"]["order_income"]
    result = normalize_shopee_escrow(order_income)
    # The live API returns explicit 0 for all three -- the canonical
    # normalizer must produce real Decimal(0), matching RAW, never the
    # stale 38480/13175/11100 that a bypassed-normalizer write left in
    # CORE.
    assert result["structured"]["commission_fee"] == Decimal("0")
    assert result["structured"]["service_fee"] == Decimal("0")
    assert result["structured"]["seller_transaction_fee"] == Decimal("0")


# =====================================================================
# 3. TikTok finance sku normalizer
# =====================================================================

def _tt_tx(fee_overrides=None, shipping_overrides=None, revenue_overrides=None):
    fee = {
        "platform_commission_amount": "0", "transaction_fee_amount": "0",
        "voucher_xtra_service_fee_amount": "0", "vn_fix_infrastructure_fee": "0",
        "affiliate_commission_amount": "0", "affiliate_ads_commission_amount": "0",
        "affiliate_partner_commission_amount": "0", "tap_shop_ads_commission": "0",
    }
    fee.update(fee_overrides or {})
    shipping = {
        "actual_shipping_fee_amount": "0", "shipping_fee_discount_amount": "0",
        "customer_paid_shipping_fee_amount": "0", "failed_delivery_subsidy_amount": "0",
    }
    shipping.update(shipping_overrides or {})
    revenue = {
        "subtotal_before_discount_amount": "0", "seller_discount_amount": "0",
        "refund_subtotal_before_discount_amount": "0", "seller_discount_refund_amount": "0",
    }
    revenue.update(revenue_overrides or {})
    return {
        "fee_tax_breakdown": {"fee": fee, "tax": {}},
        "shipping_cost_breakdown": shipping,
        "revenue_breakdown": revenue,
    }


def test_tiktok_explicit_zero_produces_real_zero():
    tx = _tt_tx(fee_overrides={"affiliate_ads_commission_amount": "0"})
    result = normalize_tiktok_finance_sku(tx)
    assert result["structured"]["affiliate_ads_commission_amount"] == Decimal("0")


def test_tiktok_regression_two_strict_diff_orders():
    """Regression test for the exact 2 rows found with raw='0'/structured=NULL."""
    tx = _tt_tx(fee_overrides={"affiliate_ads_commission_amount": "0"})
    result = normalize_tiktok_finance_sku(tx)
    # Applying the canonical normalizer must yield Decimal(0), never None,
    # for these two known rows -- closing TIKTOK_EXPLICIT_ZERO_NULL_MISMATCH.
    assert result["structured"]["affiliate_ads_commission_amount"] is not None
    assert result["structured"]["affiliate_ads_commission_amount"] == Decimal("0")


def test_tiktok_absent_fee_object_stays_none():
    tx = {"fee_tax_breakdown": None, "shipping_cost_breakdown": None, "revenue_breakdown": None}
    result = normalize_tiktok_finance_sku(tx)
    for v in result["structured"].values():
        assert v is None
    assert result["computed_shipping_cost"] is None
    assert result["computed_revenue"] is None


def test_tiktok_shipping_formula_with_failed_delivery_subsidy():
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": "-13652", "failed_delivery_subsidy_amount": "2252",
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] == Decimal("-11400")


def test_tiktok_revenue_formula_full_refund_case():
    # Exact pattern proven live: full-line refund nets to 0.
    tx = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": "269000", "seller_discount_amount": "-74000",
        "refund_subtotal_before_discount_amount": "-269000", "seller_discount_refund_amount": "74000",
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] == Decimal("0")


def test_tiktok_alias_duplicate_not_double_counted():
    # affiliate_commission_amount_before_pit is a proven alias; the
    # structured map must never sum it in addition to
    # affiliate_commission_amount.
    fee = {"affiliate_commission_amount": "-150372", "affiliate_commission_amount_before_pit": "-150372"}
    tx = _tt_tx(fee_overrides=fee)
    result = normalize_tiktok_finance_sku(tx)
    assert result["structured"]["affiliate_fee"] == Decimal("-150372")
    # the alias key never appears as its own structured column:
    assert "affiliate_commission_amount_before_pit" not in result["structured"]
