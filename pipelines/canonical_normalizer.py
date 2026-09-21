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

import hashlib
import json
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

# Shipping formula — Phase 6C Gate 5 correction (2026-09-21): the prior
# 4-field list was INCOMPLETE relative to TikTok's real
# shipping_cost_breakdown contract, which carries ~23 sub-fields. Live
# read-only re-probe of finance_order_statement_transactions plus a
# full-population scan of shipping_cost_breakdown_raw (7,097 rows)
# proved return_shipping_fee_amount is the ONLY additional flat-numeric
# field ever nonzero in this shop's population (7/7,097 rows, and those
# 7 rows are an exact 1:1 match with every live order-vs-sku shipping
# residual found — see PRODUCTION_REPAIR_MANIFEST_V4/Gate5 investigation).
# Every other extra field (distant_shipping_fee_amount,
# logistics_service_fee, shipping_insurance_fee_amount, etc.) was
# confirmed always-zero across the full population and is intentionally
# NOT added without that same kind of proof — do not guess-expand this
# list. `supplementary_component` is a nested sub-object, never a flat
# number; it must never be added here (parse_numeric would silently
# return None for it, and per-field strict semantics would then null out
# every row). Required like every field in this list: per
# strict_component_sum, if any one is absent/null, the whole
# computed_shipping_cost is None, never a partial sum.
TIKTOK_SHIPPING_FORMULA_FIELDS = [
    "actual_shipping_fee_amount", "shipping_fee_discount_amount",
    "customer_paid_shipping_fee_amount", "failed_delivery_subsidy_amount",
    "return_shipping_fee_amount",
]

# Revenue formula — RE-VERIFIED complete on full population (2026-09-21):
# the 3 fields NOT in this list (cod_service_fee_amount,
# distant_item_fee_amount, refund_cod_service_fee_amount) are confirmed
# always-zero across all 7,097 TIKTOK settlement_sku_fee rows with a
# revenue_breakdown_raw. Unlike shipping, revenue needed no field-list
# fix — every revenue residual found live traced to the separate
# multi-transaction-per-sku_id bug (see aggregate_tiktok_finance_skus).
TIKTOK_REVENUE_FORMULA_FIELDS = [
    "subtotal_before_discount_amount", "seller_discount_amount",
    "refund_subtotal_before_discount_amount", "seller_discount_refund_amount",
]

# Every other flat-numeric key observed on a live shipping_cost_breakdown
# as of the 2026-09-21 full-population scan, confirmed always-zero across
# all 7,097 rows -- NOT in the formula, but explicitly enumerated (rather
# than implicitly ignored) so audit_unknown_nonzero_components() can tell
# "known, proven always-zero so far" apart from "never seen before, needs
# investigation before trusting the formula on this row".
TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS = {
    "distant_shipping_fee_amount", "exchange_shipping_fee_amount", "fbt_free_shipping_fee_amount",
    "fbt_fulfillment_fee_reimbursement_amount", "fbt_key_merchant_subsidy", "fbt_overall_merchant_subsidy",
    "free_return_subsidy_amount", "international_leg_logistics_amount", "logistics_service_fee",
    "replacement_shipping_fee_amount", "return_shipping_fee_paid_buyer_amount",
    "return_shipping_label_fee_amount", "seller_self_shipping_service_fee_amount",
    "shipping_app_service_fee_amount", "shipping_insurance_fee_amount",
    "signature_confirmation_fee_amount", "tiktok_shop_shipping_incentive_amount",
}
# Nested sub-objects, never a flat number -- never summed, never flagged
# as "unknown nonzero" even if their own sub-fields are nonzero (that
# would need its own dedicated proof/decision, same as the top-level
# fields above got, before being added to any formula).
TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS = {"supplementary_component"}

TIKTOK_REVENUE_KNOWN_ALWAYS_ZERO_FIELDS = {
    "cod_service_fee_amount", "distant_item_fee_amount", "refund_cod_service_fee_amount",
}
TIKTOK_REVENUE_KNOWN_NESTED_FIELDS: set = set()


def audit_unknown_nonzero_components(payload: Optional[dict], formula_fields: list,
                                      known_always_zero: set, known_nested: set) -> list:
    """Flags any key in `payload` that is NOT already accounted for by
    `formula_fields` (summed), `known_always_zero` (proven zero so far,
    excluded from the formula on purpose), or `known_nested` (a
    sub-object, never a flat number) but carries a real nonzero numeric
    value. A non-empty result means TikTok's API has started returning a
    component nobody has evaluated yet -- exactly the class of gap that
    caused the original return_shipping_fee_amount bug (Gate 5, this
    session). Returns a list of (key, value) pairs; empty means clean.
    Callers that want a hard failure (e.g. a periodic audit job) should
    raise when this is non-empty -- this function itself never raises,
    so it stays safe to call from the write path without risking an ETL
    outage over a still-zero-impact discovery."""
    if not isinstance(payload, dict):
        return []
    accounted = set(formula_fields) | set(known_always_zero) | set(known_nested)
    unknown = []
    for key, raw_value in payload.items():
        if key in accounted:
            continue
        if isinstance(raw_value, dict):
            continue  # nested, unevaluated sub-object -- needs its own proof before either summing or whitelisting
        value = parse_numeric(raw_value)
        if value not in (None, Decimal(0)):
            unknown.append((key, value))
    return unknown


TRANSACTION_CANONICAL_FIELDS = [
    "statement_id", "sku_id", "sku_name", "product_name", "quantity",
    "settlement_amount", "revenue_amount", "shipping_cost_amount", "fee_tax_amount",
    "fee_tax_breakdown", "revenue_breakdown", "shipping_cost_breakdown",
]


def transaction_content_hash(tx: dict) -> str:
    """Deterministic content-addressed id for one raw sku_transaction
    entry (Phase 6C Gate 5 grain fix, TRANSACTION_IDEMPOTENCY_MODEL=H2).
    Hashes every source field TikTok returns on a tx entry — sorted
    keys, stable string representation, NULL/explicit-zero preserved,
    no ingestion metadata (etl_run_id, ingested_at, order_id are NOT
    part of this hash on purpose — order_id is a separate column in the
    table's unique key, not hashed in, because a live full-population
    probe proved identical content CAN legitimately repeat ACROSS
    different orders: the same (sku_id, statement_id, all-zero values)
    entry was observed verbatim on 2+ distinct order_ids, 2026-09-21
    duplicate-hash probe, 55 orders / 120 entries). This hash alone is
    NOT globally unique — see assign_occurrence_indices() for how
    within-order multiplicity is preserved even for a (so-far
    unobserved, but not assumed impossible) case of two byte-identical
    entries inside the SAME order."""
    canonical = {f: tx.get(f) for f in TRANSACTION_CANONICAL_FIELDS}
    blob = json.dumps(canonical, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assign_occurrence_indices(tx_list: list) -> list:
    """H2 model: returns [(tx, content_hash, occurrence_index), ...] for
    one order's sku_transactions, in the SAME array order the API
    returned them (proven live, 2026-09-21: identical re-fetch of the
    same order returns entries in the identical order, 4/4 orders
    tested — this makes occurrence_index deterministic and idempotent
    across replay, not an artifact of iteration order). occurrence_index
    is 0 for the first time a given content_hash appears within this
    order's list, 1 for the second, etc. — the DB unique key is
    (order_id, content_hash, occurrence_index), so a true duplicate
    (byte-identical entry twice in one order) gets TWO distinct rows
    (index 0 and 1), never silently collapsed to one."""
    seen_counts: dict = {}
    result = []
    for tx in tx_list:
        h = transaction_content_hash(tx)
        idx = seen_counts.get(h, 0)
        seen_counts[h] = idx + 1
        result.append((tx, h, idx))
    return result


def strict_component_sum(payload: Optional[dict], fields: list) -> Optional[Decimal]:
    """Sum `fields` out of `payload` under strict completeness: the
    result is a real total ONLY when every field in `fields` is present
    with a real numeric value (explicit zero counts as present). If
    `payload` itself isn't a dict (absent/None/malformed), or ANY single
    field in `fields` is missing/null, the result is None — a partial
    sum is never presented as if it were a complete total (PARTIAL !=
    COMPLETE; MISSING != ZERO). Only when every field parses to a real
    Decimal (Case: all-explicit-zero included) does this return their
    sum, which may legitimately be Decimal(0)."""
    if not isinstance(payload, dict):
        return None
    parts = [parse_numeric(payload.get(f)) for f in fields]
    if any(p is None for p in parts):
        return None
    return sum(parts, Decimal(0))


def normalize_tiktok_finance_sku(tx: dict) -> dict:
    """Single source of truth for turning one sku_transaction object
    (from finance_order_statement_transactions) into CORE's structured
    fields. Returns:
      - 'structured': {core_column_name: Decimal|None, ...} for every
        column in TIKTOK_FEE_STRUCTURED_MAP
      - 'computed_shipping_cost': Decimal|None — strict_component_sum
        over TIKTOK_SHIPPING_FORMULA_FIELDS. None whenever the breakdown
        object is absent, present-but-empty/all-null, or only partially
        populated (never a fabricated 0, never a partial sum passed off
        as complete); a real Decimal (possibly 0) only when every
        formula field is present with a real value.
      - 'computed_revenue': Decimal|None — same contract, over
        TIKTOK_REVENUE_FORMULA_FIELDS
      - 'fee_tax_breakdown_raw', 'revenue_breakdown_raw',
        'shipping_cost_breakdown_raw': the raw sub-objects, unmodified
    """
    fee_tax = tx.get("fee_tax_breakdown") or {}
    fee = fee_tax.get("fee") or {}

    structured = {
        core_col: parse_numeric(fee.get(raw_field))
        for core_col, raw_field in TIKTOK_FEE_STRUCTURED_MAP.items()
    }

    computed_shipping = strict_component_sum(tx.get("shipping_cost_breakdown"), TIKTOK_SHIPPING_FORMULA_FIELDS)
    computed_revenue = strict_component_sum(tx.get("revenue_breakdown"), TIKTOK_REVENUE_FORMULA_FIELDS)

    return {
        "structured": structured,
        "computed_shipping_cost": computed_shipping,
        "computed_revenue": computed_revenue,
        "fee_tax_breakdown_raw": tx.get("fee_tax_breakdown"),
        "revenue_breakdown_raw": tx.get("revenue_breakdown"),
        "shipping_cost_breakdown_raw": tx.get("shipping_cost_breakdown"),
    }


def aggregate_tiktok_finance_skus(tx_list: list) -> dict:
    """Aggregate every sku_transaction entry that shares one sku_id
    within a single order's finance_order_statement_transactions
    response. NOT YET WIRED into incr_worker.py's production write loop
    (see module docstring's STAGED convention) — designed and tested
    here first per Phase 6C Gate 5 remediation.

    Root cause this closes: TikTok can return MORE THAN ONE transaction
    line for the same sku_id in one order (e.g. an original sale line
    plus a separate refund-adjustment line, each with its own
    statement_id — proven live, order 585474165265106454 returned 4
    sku_transactions covering only 2 distinct sku_id values). The
    existing write path upserts on (channel,shop_id,order_id,sku_id) —
    no statement_id component — so looping and writing each tx
    individually makes a later entry silently overwrite an earlier one,
    permanently losing real fee/revenue data (proven: DB retained only 1
    of each colliding pair, understating order revenue by the dropped
    entry's full value). This function sums every entry instead of
    keeping only the last.

    Field-level rule, deliberately different from strict_component_sum:
    each structured/computed field is the sum of every entry's value for
    that field, treating a per-ENTRY None as "this transaction did not
    carry this fee" (0 contribution), not as missing data — appropriate
    here because the entries are independent transaction events, not
    sub-components of one total. A field is None in the aggregate only
    when EVERY entry in the group has it None (the fee/formula never
    appeared in any transaction for this sku_id this order).

    Raw payloads are kept as a list (one element per transaction line) —
    callers persisting this to a jsonb column must store it as a JSON
    array, a deliberate shape change from the single-object shape
    normalize_tiktok_finance_sku's raw fields use; any downstream reader
    of *_breakdown_raw (including sql/060 and ad-hoc reconciliation
    queries) must be updated for this shape before this function is
    wired into the write path.
    """
    normed = [normalize_tiktok_finance_sku(tx) for tx in tx_list]

    agg_structured = {}
    for col in TIKTOK_FEE_STRUCTURED_MAP:
        vals = [n["structured"][col] for n in normed if n["structured"][col] is not None]
        agg_structured[col] = sum(vals, Decimal(0)) if vals else None

    ship_vals = [n["computed_shipping_cost"] for n in normed if n["computed_shipping_cost"] is not None]
    rev_vals = [n["computed_revenue"] for n in normed if n["computed_revenue"] is not None]

    return {
        "structured": agg_structured,
        "computed_shipping_cost": sum(ship_vals, Decimal(0)) if ship_vals else None,
        "computed_revenue": sum(rev_vals, Decimal(0)) if rev_vals else None,
        "fee_tax_breakdown_raw": [n["fee_tax_breakdown_raw"] for n in normed],
        "revenue_breakdown_raw": [n["revenue_breakdown_raw"] for n in normed],
        "shipping_cost_breakdown_raw": [n["shipping_cost_breakdown_raw"] for n in normed],
        "n_transactions": len(tx_list),
    }
