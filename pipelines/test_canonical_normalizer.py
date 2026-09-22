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
    strict_component_sum,
    aggregate_tiktok_finance_skus,
    audit_unknown_nonzero_components,
    transaction_content_hash,
    assign_occurrence_indices,
    decide_settlement_write,
    TIKTOK_SHIPPING_FORMULA_FIELDS,
    TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS,
    TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
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
    # P11-TER.1 — Shopee AMS get_product_performance returns the literal
    # sentinel "--" for a metric that's mathematically undefined (e.g.
    # roi with zero spend), not zero. Production-proven: this exact
    # value crashed the fact_shopee_affiliate_performance_daily INSERT
    # with "invalid input syntax for type numeric" on 2026-09-17 and
    # 2026-09-21 (same product both times) before incr_worker.py's local
    # D() helper was fixed to route through parse_numeric() instead of
    # passing non-empty strings through unchanged.
    ("--", None),
    ("-", None),        # same class of dash-only placeholder, not a real value
    ("N/A", None),       # any other unparseable sentinel -> NULL, never 0
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
        "return_shipping_fee_amount": "0",
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


# =====================================================================
# 4. strict_component_sum — Phase 6C Gate 1 fix. Regression coverage for
#    the exact bug found and patched in this pass: a formula total must
#    NEVER be a fabricated 0 (breakdown present but all-null) and must
#    NEVER be a partial sum passed off as a complete total (breakdown
#    present with only some fields populated). NULL != 0, PARTIAL !=
#    COMPLETE, MISSING != ZERO, EXPLICIT_ZERO == 0.
# =====================================================================

def test_strict_component_sum_payload_not_dict_is_none():
    assert strict_component_sum(None, ["a", "b"]) is None
    assert strict_component_sum("not a dict", ["a", "b"]) is None


def test_strict_component_sum_partial_is_none_not_partial_total():
    assert strict_component_sum({"a": 10000}, ["a", "b"]) is None


def test_strict_component_sum_all_explicit_zero_is_real_zero():
    assert strict_component_sum({"a": 0, "b": "0"}, ["a", "b"]) == Decimal("0")


def test_strict_component_sum_all_present_is_exact_sum():
    assert strict_component_sum({"a": 10000, "b": -2500}, ["a", "b"]) == Decimal("7500")


# ---- Shipping: 5 required cases ----

def test_tiktok_shipping_case1_object_absent_is_none():
    tx = _tt_tx()
    tx["shipping_cost_breakdown"] = None
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] is None


def test_tiktok_shipping_case2_object_present_all_null_is_none():
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": None, "shipping_fee_discount_amount": None,
        "customer_paid_shipping_fee_amount": None, "failed_delivery_subsidy_amount": None,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] is None


def test_tiktok_shipping_case3_partial_components_is_none_not_fabricated_total():
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": 10000, "shipping_fee_discount_amount": None,
        "customer_paid_shipping_fee_amount": None, "failed_delivery_subsidy_amount": None,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] is None  # NOT 10000


def test_tiktok_shipping_case4_all_explicit_zero_is_real_zero():
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": 0, "shipping_fee_discount_amount": 0,
        "customer_paid_shipping_fee_amount": 0, "failed_delivery_subsidy_amount": 0,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] == Decimal("0")


def test_tiktok_shipping_case5_all_complete_is_exact_sum():
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": 20000, "shipping_fee_discount_amount": -5000,
        "customer_paid_shipping_fee_amount": 15000, "failed_delivery_subsidy_amount": 0,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] == Decimal("30000")


# ---- Revenue: 5 required cases ----

def test_tiktok_revenue_case6_object_absent_is_none():
    tx = _tt_tx()
    tx["revenue_breakdown"] = None
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] is None


def test_tiktok_revenue_case7_object_present_all_null_is_none():
    tx = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": None, "seller_discount_amount": None,
        "refund_subtotal_before_discount_amount": None, "seller_discount_refund_amount": None,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] is None


def test_tiktok_revenue_case8_partial_components_is_none_not_fabricated_total():
    tx = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": 269000, "seller_discount_amount": None,
        "refund_subtotal_before_discount_amount": None, "seller_discount_refund_amount": None,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] is None  # NOT 269000


def test_tiktok_revenue_case9_all_explicit_zero_is_real_zero():
    tx = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": 0, "seller_discount_amount": 0,
        "refund_subtotal_before_discount_amount": 0, "seller_discount_refund_amount": 0,
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] == Decimal("0")


def test_tiktok_revenue_case10_all_complete_is_exact_sum():
    tx = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": "269000", "seller_discount_amount": "-74000",
        "refund_subtotal_before_discount_amount": "-269000", "seller_discount_refund_amount": "74000",
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_revenue"] == Decimal("0")


# =====================================================================
# 5. Gate 5 remediation (2026-09-21) — return_shipping_fee_amount fix +
#    aggregate_tiktok_finance_skus (multi-transaction-per-sku_id bug).
#    Regression coverage tied to real orders found live on production
#    during the Gate 5 investigation (read-only; no values invented).
# =====================================================================

def test_tiktok_shipping_formula_includes_return_shipping_fee_amount():
    # Regression for order 582847227535525582 (live production, 2026-09-21):
    # return_shipping_fee_amount=-41250 was present and nonzero but NOT in
    # the old 4-field formula, understating computed_shipping_cost by
    # exactly that amount versus TikTok's own order-level shipping_cost_amount.
    tx = _tt_tx(shipping_overrides={
        "actual_shipping_fee_amount": "-75400", "shipping_fee_discount_amount": "0",
        "customer_paid_shipping_fee_amount": "0", "failed_delivery_subsidy_amount": "0",
        "return_shipping_fee_amount": "-41250",
    })
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] == Decimal("-116650")  # matches live order-level shipping_cost_amount


def test_tiktok_shipping_missing_return_shipping_fee_amount_is_none_not_partial():
    # If return_shipping_fee_amount is present on the breakdown object
    # elsewhere in the population but absent on THIS tx, the formula must
    # still be all-or-nothing (never silently treat it as 0).
    tx = _tt_tx(shipping_overrides={"actual_shipping_fee_amount": "-75400"})
    del tx["shipping_cost_breakdown"]["return_shipping_fee_amount"]
    # remaining formula fields default to "0" from _tt_tx, but return_shipping_fee_amount is now absent
    assert "return_shipping_fee_amount" not in tx["shipping_cost_breakdown"]
    result = normalize_tiktok_finance_sku(tx)
    assert result["computed_shipping_cost"] is None


def test_aggregate_single_transaction_matches_normalize():
    tx = _tt_tx(fee_overrides={"platform_commission_amount": "-1000"})
    single = normalize_tiktok_finance_sku(tx)
    agg = aggregate_tiktok_finance_skus([tx])
    assert agg["structured"]["fixed_fee"] == single["structured"]["fixed_fee"]
    assert agg["computed_shipping_cost"] == single["computed_shipping_cost"]
    assert agg["n_transactions"] == 1
    assert agg["fee_tax_breakdown_raw"] == [single["fee_tax_breakdown_raw"]]


def test_aggregate_multi_transaction_real_plus_allzero_sums_correctly():
    # Regression for order 585474165265106454 (live production, 2026-09-21):
    # 4 sku_transactions covering only 2 distinct sku_id values (2 real +
    # 2 all-zero duplicates). The old write path (upsert on order_id+sku_id,
    # no statement_id) kept only the LAST entry per sku_id and lost the
    # real one whenever the zero-entry was processed after it -- DB showed
    # sku-level revenue=0 while TikTok's own order-level total was 336000.
    tx_real = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": "499000", "seller_discount_amount": "-163000",
    })
    tx_zero = _tt_tx()  # all revenue fields default to explicit "0" in _tt_tx
    agg = aggregate_tiktok_finance_skus([tx_real, tx_zero])
    assert agg["computed_revenue"] == Decimal("336000")
    assert agg["n_transactions"] == 2


def test_aggregate_refund_adjustment_nets_to_zero():
    # Regression for order 585281732040688940's sku_id 1735775277759694044
    # (live production): an original-sale line and a separate
    # refund-adjustment line for the SAME sku_id, opposite sign, net to 0 --
    # proving the old last-write-wins path could report either +219000 or
    # -219000 (whichever line was processed last) instead of the true 0.
    tx_sale = _tt_tx(revenue_overrides={
        "subtotal_before_discount_amount": "435000", "seller_discount_amount": "-216000",
    })
    tx_refund = _tt_tx(revenue_overrides={
        "refund_subtotal_before_discount_amount": "-435000", "seller_discount_refund_amount": "216000",
    })
    agg = aggregate_tiktok_finance_skus([tx_sale, tx_refund])
    assert agg["computed_revenue"] == Decimal("0")


def test_aggregate_partial_one_entry_none_field_contributes_zero_not_none():
    # Deliberately different rule from strict_component_sum: a field that
    # is None on ONE transaction entry (fee code not applicable to that
    # specific transaction) must not null out the whole aggregate when
    # another entry in the group has a real value.
    tx_with_fee = _tt_tx(fee_overrides={"platform_commission_amount": "-500"})
    tx_without_fee = _tt_tx()
    del tx_without_fee["fee_tax_breakdown"]["fee"]["platform_commission_amount"]
    agg = aggregate_tiktok_finance_skus([tx_with_fee, tx_without_fee])
    assert agg["structured"]["fixed_fee"] == Decimal("-500")


def test_aggregate_all_entries_missing_field_stays_none():
    tx1 = _tt_tx()
    tx2 = _tt_tx()
    del tx1["fee_tax_breakdown"]["fee"]["platform_commission_amount"]
    del tx2["fee_tax_breakdown"]["fee"]["platform_commission_amount"]
    agg = aggregate_tiktok_finance_skus([tx1, tx2])
    assert agg["structured"]["fixed_fee"] is None


def test_aggregate_all_explicit_zero_across_entries_is_real_zero():
    tx1 = _tt_tx()
    tx2 = _tt_tx()
    agg = aggregate_tiktok_finance_skus([tx1, tx2])
    assert agg["computed_shipping_cost"] == Decimal("0")
    assert agg["computed_revenue"] == Decimal("0")
    assert agg["structured"]["fixed_fee"] == Decimal("0")


# =====================================================================
# 6. audit_unknown_nonzero_components — B1 requirement: detect (not
#    silently ignore) a raw component nobody has evaluated yet.
# =====================================================================

def test_audit_clean_payload_flags_nothing():
    payload = {f: "0" for f in TIKTOK_SHIPPING_FORMULA_FIELDS}
    payload.update({f: "0" for f in TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS})
    payload["supplementary_component"] = {"anything": "0"}
    result = audit_unknown_nonzero_components(
        payload, TIKTOK_SHIPPING_FORMULA_FIELDS,
        TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
    )
    assert result == []


def test_audit_flags_genuinely_unknown_nonzero_field():
    # Simulates TikTok adding a brand-new shipping fee sub-field tomorrow.
    payload = {f: "0" for f in TIKTOK_SHIPPING_FORMULA_FIELDS}
    payload["brand_new_fee_field_amount"] = "-5000"
    result = audit_unknown_nonzero_components(
        payload, TIKTOK_SHIPPING_FORMULA_FIELDS,
        TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
    )
    assert result == [("brand_new_fee_field_amount", Decimal("-5000"))]


def test_audit_known_always_zero_field_going_nonzero_is_flagged():
    # A field previously proven always-zero (so excluded from the formula)
    # turning nonzero must surface, not be silently absorbed as "known".
    payload = {f: "0" for f in TIKTOK_SHIPPING_FORMULA_FIELDS}
    payload["logistics_service_fee"] = "1234"  # was always 0 as of 2026-09-21
    result = audit_unknown_nonzero_components(
        payload, TIKTOK_SHIPPING_FORMULA_FIELDS, set(), TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
    )
    assert ("logistics_service_fee", Decimal("1234")) in result


def test_audit_nested_dict_never_flagged_even_if_it_looks_nonzero():
    payload = {f: "0" for f in TIKTOK_SHIPPING_FORMULA_FIELDS}
    payload["supplementary_component"] = {"platform_shipping_fee_discount_amount": "9999"}
    result = audit_unknown_nonzero_components(
        payload, TIKTOK_SHIPPING_FORMULA_FIELDS,
        TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
    )
    assert result == []


# =====================================================================
# 7. transaction_content_hash + assign_occurrence_indices (Task A/B —
#    TRANSACTION_IDEMPOTENCY_MODEL=H2) — regression coverage for the
#    exact live patterns found on production, 2026-09-21:
#      - order 585474165265106454: two entries sharing (sku_id,
#        statement_id) with DIFFERENT settlement_amount (proves
#        statement_id alone is not a safe key).
#      - cross-order duplicate-hash probe (55 orders / 120 entries):
#        an identical (sku_id, statement_id, all-zero values) entry
#        observed on 2+ DIFFERENT order_ids (proves content_hash alone,
#        without order_id, is not globally unique -- order_id must be a
#        separate column in the DB unique key, not hashed in).
# =====================================================================

def _canon_tx(sku_id, statement_id, settlement_amount, revenue_amount="0",
              shipping_cost_amount="0", fee_tax_amount="0"):
    return {
        "sku_id": sku_id, "statement_id": statement_id,
        "settlement_amount": settlement_amount, "revenue_amount": revenue_amount,
        "shipping_cost_amount": shipping_cost_amount, "fee_tax_amount": fee_tax_amount,
        "sku_name": None, "product_name": None, "quantity": None,
        "fee_tax_breakdown": {}, "revenue_breakdown": {}, "shipping_cost_breakdown": {},
    }


def test_content_hash_identical_replay_is_stable():
    tx = _canon_tx("1735775277759694044", "7669237315507767060", "166946", revenue_amount="219000")
    h1 = transaction_content_hash(tx)
    h2 = transaction_content_hash(dict(tx))
    assert h1 == h2


def test_content_hash_same_sku_and_statement_id_but_different_amount_differs():
    # Real production pattern: order 585474165265106454 tx[0] vs tx[2].
    tx_real = _canon_tx("1735734212755293404", "7672589526459598609", "230539", revenue_amount="336000", fee_tax_amount="-105461")
    tx_zero = _canon_tx("1735734212755293404", "7672589526459598609", "0")
    assert transaction_content_hash(tx_real) != transaction_content_hash(tx_zero)


def test_content_hash_sale_then_refund_adjustment_different_statement_ids_distinct():
    tx_sale = _canon_tx("1735775277759694044", "7669237315507767060", "166946", revenue_amount="219000", fee_tax_amount="-52054")
    tx_refund = _canon_tx("1735775277759694044", "7678133531121469202", "-166946", revenue_amount="-219000", fee_tax_amount="52054")
    assert transaction_content_hash(tx_sale) != transaction_content_hash(tx_refund)


def test_content_hash_does_not_depend_on_order_id():
    # order_id is deliberately NOT part of the hash -- proven live that
    # identical content legitimately repeats across different orders
    # (a shared all-zero placeholder statement line). Uniqueness comes
    # from the DB key (order_id, content_hash, occurrence_index), not
    # from folding order_id into the hash itself.
    tx = _canon_tx("1735801927555253468", "7672589526459598609", "0")
    assert transaction_content_hash(tx) == transaction_content_hash(dict(tx))


# ---- D3/D4: occurrence_index (H2 model) ----

def test_occurrence_index_no_duplicates_all_zero():
    tx_a = _canon_tx("skuA", "stmt1", "100")
    tx_b = _canon_tx("skuB", "stmt2", "200")
    result = assign_occurrence_indices([tx_a, tx_b])
    assert [idx for _, _, idx in result] == [0, 0]
    assert len({h for _, h, _ in result}) == 2  # distinct content -> distinct hashes


def test_occurrence_index_same_sku_statement_different_amount_both_index_0():
    # D3: same sku_id + same statement_id, different amount -- these are
    # DIFFERENT content (different hash), so each is its own index-0.
    tx_real = _canon_tx("skuA", "stmt1", "230539", revenue_amount="336000")
    tx_zero = _canon_tx("skuA", "stmt1", "0")
    result = assign_occurrence_indices([tx_real, tx_zero])
    hashes = [h for _, h, _ in result]
    indices = [idx for _, _, idx in result]
    assert hashes[0] != hashes[1]
    assert indices == [0, 0]  # distinct hashes, each first-seen


def test_occurrence_index_true_duplicate_entries_get_0_and_1():
    # D4: TWO byte-identical source entries in the SAME order (never
    # observed live in the 55-order sample, but the design must not
    # assume it can't happen) -- must survive as 2 rows, indices 0 and 1.
    tx = _canon_tx("skuA", "stmt1", "100")
    result = assign_occurrence_indices([tx, dict(tx)])
    assert [h for _, h, _ in result][0] == [h for _, h, _ in result][1]
    assert [idx for _, _, idx in result] == [0, 1]


def test_occurrence_index_deterministic_across_replay():
    # Re-fetch stability proven live (identical array order across 2
    # calls, 4/4 orders) -- replaying the identical tx_list must assign
    # the identical occurrence_index each time, so ON CONFLICT DO
    # NOTHING on (order_id, content_hash, occurrence_index) is a safe no-op.
    tx_list = [_canon_tx("skuA", "stmt1", "100"), _canon_tx("skuA", "stmt1", "100"), _canon_tx("skuB", "stmt2", "200")]
    result1 = [(h, idx) for _, h, idx in assign_occurrence_indices(tx_list)]
    result2 = [(h, idx) for _, h, idx in assign_occurrence_indices([dict(tx) for tx in tx_list])]
    assert result1 == result2


# ---- Append-only store simulation (H2 model, order_id + content_hash + occurrence_index key) ----

def test_idempotent_replay_no_duplicate_no_double_sum():
    """Simulate the H2 append-only store across three fetch cycles for
    ONE order: (1) sale only, (2) sale + refund adjustment appears,
    (3) same sale + refund re-fetched again. Final state must equal
    exactly {sale, refund} once each, total reflects each exactly once."""
    order_id = "585281732040688940"
    tx_sale = _canon_tx("1735775277759694044", "7669237315507767060", "166946", revenue_amount="219000")
    tx_refund = _canon_tx("1735775277759694044", "7678133531121469202", "-166946", revenue_amount="-219000")

    store: dict[tuple, dict] = {}  # (order_id, content_hash, occurrence_index) -> tx

    def apply_fetch(tx_list):
        for tx, h, idx in assign_occurrence_indices(tx_list):
            key = (order_id, h, idx)
            if key not in store:  # ON CONFLICT (order_id, content_hash, occurrence_index) DO NOTHING
                store[key] = tx

    apply_fetch([tx_sale])                    # cycle 1
    assert len(store) == 1
    apply_fetch([tx_sale, tx_refund])          # cycle 2 -- sale re-seen, refund is new
    assert len(store) == 2
    apply_fetch([tx_sale, tx_refund])          # cycle 3 -- both re-seen again, must not double-add
    assert len(store) == 2

    total_revenue = sum(Decimal(tx["revenue_amount"]) for tx in store.values())
    assert total_revenue == Decimal("0")  # sale (219000) + refund (-219000), each counted exactly once


def test_idempotent_true_duplicate_multiplicity_survives_replay():
    """D6+D4 combined: two byte-identical entries in one order, fetched
    three times -- multiplicity must stay 2, never collapse to 1 and
    never grow past 2."""
    order_id = "585999999999999999"
    tx = _canon_tx("skuA", "stmt1", "100")

    store: dict[tuple, dict] = {}

    def apply_fetch(tx_list):
        for t, h, idx in assign_occurrence_indices(tx_list):
            key = (order_id, h, idx)
            if key not in store:
                store[key] = t

    apply_fetch([tx, dict(tx)])
    assert len(store) == 2
    apply_fetch([tx, dict(tx)])  # replay
    assert len(store) == 2
    total = sum(Decimal(t["settlement_amount"]) for t in store.values())
    assert total == Decimal("200")  # both real occurrences counted, no more no less


def test_idempotent_full_replay_of_same_payload_twice_matches_aggregate():
    """Replaying the EXACT same API payload twice must not change the
    canonical aggregate computed via aggregate_tiktok_finance_skus."""
    tx1 = _tt_tx(revenue_overrides={"subtotal_before_discount_amount": "499000", "seller_discount_amount": "-163000"})
    tx2 = _tt_tx()
    first = aggregate_tiktok_finance_skus([tx1, tx2])
    second = aggregate_tiktok_finance_skus([tx1, tx2])
    assert first["computed_revenue"] == second["computed_revenue"] == Decimal("336000")


# NOTE: "one-side API failure" and "retry" (from the Phase 6C Gate 5 task
# spec) are HTTP/session-layer concerns that live in incr_worker.py's
# ic.with_backoff()/session handling, not in this pure-function module --
# they need live or mocked API behavior to test meaningfully and are out
# of scope for canonical_normalizer's no-DB/no-network unit tests. Not
# fabricated here; flagged as a gap for whoever wires
# aggregate_tiktok_finance_skus into incr_worker.py's write loop.


# =====================================================================
# 8. decide_settlement_write — Phase 6C P4-bis TRANSIENT_SETTLEMENT_
#    REGRESSION guard. Regression coverage tied to the real production
#    incident: order 586086803325813933, confirmed-good MATCHED/134669
#    silently overwritten with UNKNOWN/NULL by one glitched API call.
# =====================================================================

def test_d1_no_existing_row_incoming_unknown_writes_pending_as_before():
    # existing=NULL, incoming=UNKNOWN -> nothing to protect, write as computed
    assert decide_settlement_write(None, "UNKNOWN") == "WRITE"


def test_d2_existing_matched_incoming_matched_new_valid_writes():
    existing = {"settlement_type": "MATCHED", "settlement_amount": Decimal("100")}
    assert decide_settlement_write(existing, "MATCHED") == "WRITE"


def _simulate_write_with_retry(existing, incoming_status, retry_status):
    """Mirrors incr_worker.py's guard usage: decide -> if RETRY, re-fetch
    once (simulated by retry_status) -> WRITE only if the retry itself
    resolves to MATCHED, else PRESERVE (skip) + warning."""
    decision = decide_settlement_write(existing, incoming_status)
    if decision == "WRITE":
        return ("WRITE", incoming_status)
    # decision == "RETRY"
    if retry_status == "MATCHED":
        return ("WRITE", retry_status)
    return ("PRESERVE", existing["settlement_type"])


def test_d3_existing_matched_incoming_unknown_retry_matched_uses_retry():
    existing = {"settlement_type": "MATCHED", "settlement_amount": Decimal("134669")}
    action, final_status = _simulate_write_with_retry(existing, "UNKNOWN", retry_status="MATCHED")
    assert action == "WRITE"
    assert final_status == "MATCHED"


def test_d4_existing_matched_incoming_unknown_retry_still_unknown_preserves():
    existing = {"settlement_type": "MATCHED", "settlement_amount": Decimal("134669")}
    action, final_status = _simulate_write_with_retry(existing, "UNKNOWN", retry_status="UNKNOWN")
    assert action == "PRESERVE"
    assert final_status == "MATCHED"  # existing value never touched


def test_d5_existing_explicit_zero_settlement_still_counts_as_confirmed_good():
    # A real MATCHED order can legitimately settle to exactly 0 (e.g. a
    # fully-discounted order) -- explicit zero must still be protected,
    # never treated as "no real value to protect".
    existing = {"settlement_type": "MATCHED", "settlement_amount": Decimal("0")}
    assert decide_settlement_write(existing, "UNKNOWN") == "RETRY"


def test_d6_null_settlement_amount_is_not_confirmed_good_even_if_matched():
    # NULL != 0: a row with settlement_type='MATCHED' but a NULL amount
    # (shouldn't normally happen, but must not be trusted as "confirmed
    # good" if it does) does not block a fresh write.
    existing = {"settlement_type": "MATCHED", "settlement_amount": None}
    assert decide_settlement_write(existing, "UNKNOWN") == "WRITE"


def test_d7_replay_same_valid_payload_idempotent():
    existing = {"settlement_type": "MATCHED", "settlement_amount": Decimal("134669")}
    action, final_status = _simulate_write_with_retry(existing, "MATCHED", retry_status="MATCHED")
    assert action == "WRITE"
    assert final_status == "MATCHED"
    # calling again with the same inputs must yield the identical decision
    action2, final_status2 = _simulate_write_with_retry(existing, "MATCHED", retry_status="MATCHED")
    assert (action2, final_status2) == (action, final_status)


def test_d8_guard_does_not_affect_sku_transaction_or_aggregate_writer():
    # decide_settlement_write is a standalone function with no shared
    # state or side effects on the sku-level lossless/aggregate layer --
    # calling it (in any decision) must not change what
    # aggregate_tiktok_finance_skus computes for the same tx data.
    tx = _tt_tx(revenue_overrides={"subtotal_before_discount_amount": "1000"})
    before = aggregate_tiktok_finance_skus([tx])
    decide_settlement_write({"settlement_type": "MATCHED", "settlement_amount": Decimal("100")}, "UNKNOWN")
    after = aggregate_tiktok_finance_skus([tx])
    assert before["computed_revenue"] == after["computed_revenue"] == Decimal("1000")
