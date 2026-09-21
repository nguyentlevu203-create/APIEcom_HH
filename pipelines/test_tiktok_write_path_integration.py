"""
Phase 6C Task D — integration-style local tests for the TikTok finance
write-path ALGORITHM (Design 3 / H2), exercising the exact same
functions integrations/tiktok_shop/pilot_reporting/incr_worker.py's
run_finance() now calls (assign_occurrence_indices,
aggregate_tiktok_finance_skus, audit_unknown_nonzero_components)
end-to-end against in-memory fake tables that enforce the SAME unique
constraints as sql/061 (fact_settlement_sku_transaction) and the
existing fact_settlement_sku_fee.

No DB, no network, no psycopg2 — a full black-box test of run_finance()
itself would require mocking bootstrap_session()'s real Keychain/API
auth chain, which is out of scope; this instead pins down the
write-path ALGORITHM's correctness, which is where Gate 5's bug lived.

Run with: python3 -m pytest pipelines/test_tiktok_write_path_integration.py -v
"""
from __future__ import annotations
from decimal import Decimal

import pytest

from canonical_normalizer import (
    normalize_tiktok_finance_sku, aggregate_tiktok_finance_skus,
    assign_occurrence_indices, audit_unknown_nonzero_components,
    TIKTOK_SHIPPING_FORMULA_FIELDS, TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS,
    TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
)


class FakeDB:
    """In-memory stand-in for the two real tables, enforcing the same
    unique constraints sql/061 (content_hash+occurrence_index, scoped
    per order_id) and the existing fact_settlement_sku_fee
    (order_id,sku_id) upsert key define."""

    def __init__(self):
        self.sku_transaction: dict[tuple, dict] = {}   # (order_id, content_hash, occurrence_index) -> row
        self.sku_fee: dict[tuple, dict] = {}            # (order_id, sku_id) -> row (last write wins, like a real UPSERT)
        self.dq_warnings: list[str] = []

    def apply_order(self, order_id: str, tx_transactions: list) -> None:
        """Mirrors incr_worker.py::run_finance's per-order steps 1+2
        exactly: persist every tx lossless (ON CONFLICT DO NOTHING),
        audit unknown components, then derive + upsert the aggregate."""
        valid_tx = [tx for tx in tx_transactions if tx.get("sku_id")]

        for tx, content_hash, occurrence_index in assign_occurrence_indices(valid_tx):
            key = (order_id, content_hash, occurrence_index)
            if key not in self.sku_transaction:  # ON CONFLICT DO NOTHING
                self.sku_transaction[key] = dict(tx)

            unknown = audit_unknown_nonzero_components(
                tx.get("shipping_cost_breakdown"), TIKTOK_SHIPPING_FORMULA_FIELDS,
                TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS,
            )
            if unknown:
                self.dq_warnings.append(f"order={order_id} sku={tx.get('sku_id')} unknown={unknown}")

        by_sku: dict[str, list] = {}
        for tx in valid_tx:
            by_sku.setdefault(tx.get("sku_id"), []).append(tx)

        for sku_id, tx_group in by_sku.items():
            agg = aggregate_tiktok_finance_skus(tx_group)
            self.sku_fee[(order_id, sku_id)] = agg  # UPSERT: last apply_order() call for this key wins, like ON CONFLICT DO UPDATE

    def ledger_rows_for(self, order_id: str) -> list:
        return [v for (oid, _, _), v in self.sku_transaction.items() if oid == order_id]


def _tx(sku_id, statement_id, settlement="0", revenue_over=None, shipping_over=None, extra_shipping=None):
    revenue = {
        "subtotal_before_discount_amount": "0", "seller_discount_amount": "0",
        "refund_subtotal_before_discount_amount": "0", "seller_discount_refund_amount": "0",
    }
    revenue.update(revenue_over or {})
    shipping = {
        "actual_shipping_fee_amount": "0", "shipping_fee_discount_amount": "0",
        "customer_paid_shipping_fee_amount": "0", "failed_delivery_subsidy_amount": "0",
        "return_shipping_fee_amount": "0",
    }
    shipping.update(shipping_over or {})
    if extra_shipping:
        shipping.update(extra_shipping)
    fee = {
        "platform_commission_amount": "0", "transaction_fee_amount": "0",
        "voucher_xtra_service_fee_amount": "0", "vn_fix_infrastructure_fee": "0",
        "affiliate_commission_amount": "0", "affiliate_ads_commission_amount": "0",
        "affiliate_partner_commission_amount": "0", "tap_shop_ads_commission": "0",
    }
    return {
        "sku_id": sku_id, "statement_id": statement_id, "settlement_amount": settlement,
        "revenue_amount": "0", "shipping_cost_amount": "0", "fee_tax_amount": "0",
        "sku_name": "Test SKU", "product_name": "Test Product", "quantity": "1",
        "fee_tax_breakdown": {"fee": fee, "tax": {}},
        "revenue_breakdown": revenue, "shipping_cost_breakdown": shipping,
    }


# D1 — single SKU, single tx
def test_d1_single_sku_single_tx():
    db = FakeDB()
    tx = _tx("skuA", "stmt1", settlement="1000", revenue_over={"subtotal_before_discount_amount": "1000"})
    db.apply_order("orderA", [tx])
    assert len(db.ledger_rows_for("orderA")) == 1
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")


# D2 — same SKU, sale + refund tx (different statement_id)
def test_d2_sale_plus_refund_same_sku():
    db = FakeDB()
    sale = _tx("skuA", "stmt1", revenue_over={"subtotal_before_discount_amount": "1000"})
    refund = _tx("skuA", "stmt2", revenue_over={"refund_subtotal_before_discount_amount": "-1000"})
    db.apply_order("orderA", [sale, refund])
    assert len(db.ledger_rows_for("orderA")) == 2  # both preserved, lossless
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("0")  # nets correctly


# D3 — same SKU, same statement_id, different amounts (the exact live bug pattern)
def test_d3_same_sku_same_statement_different_amounts():
    db = FakeDB()
    real = _tx("skuA", "stmt1", settlement="230539", revenue_over={"subtotal_before_discount_amount": "336000"})
    zero = _tx("skuA", "stmt1", settlement="0")
    db.apply_order("orderA", [real, zero])
    assert len(db.ledger_rows_for("orderA")) == 2  # both content-distinct, both kept
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("336000")  # real value not lost


# D4 — same SKU, IDENTICAL duplicate source entries
def test_d4_identical_duplicate_entries_multiplicity_preserved():
    db = FakeDB()
    tx = _tx("skuA", "stmt1", settlement="500", revenue_over={"subtotal_before_discount_amount": "500"})
    db.apply_order("orderA", [tx, dict(tx)])  # API genuinely returns the same entry twice
    assert len(db.ledger_rows_for("orderA")) == 2  # multiplicity 2 survives
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")  # summed twice, correctly


# D5 — two SKUs, one has a refund
def test_d5_two_skus_one_refunded():
    db = FakeDB()
    sku_a_sale = _tx("skuA", "stmt1", revenue_over={"subtotal_before_discount_amount": "1000"})
    sku_b_sale = _tx("skuB", "stmt1", revenue_over={"subtotal_before_discount_amount": "500"})
    sku_b_refund = _tx("skuB", "stmt2", revenue_over={"refund_subtotal_before_discount_amount": "-500"})
    db.apply_order("orderA", [sku_a_sale, sku_b_sale, sku_b_refund])
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")
    assert db.sku_fee[("orderA", "skuB")]["computed_revenue"] == Decimal("0")


# D6 — replay identical API payload twice
def test_d6_replay_identical_payload_twice_no_change():
    db = FakeDB()
    tx_list = [_tx("skuA", "stmt1", revenue_over={"subtotal_before_discount_amount": "1000"})]
    db.apply_order("orderA", tx_list)
    n_rows_1 = len(db.ledger_rows_for("orderA"))
    db.apply_order("orderA", [dict(t) for t in tx_list])  # identical re-fetch
    n_rows_2 = len(db.ledger_rows_for("orderA"))
    assert n_rows_1 == n_rows_2 == 1  # no duplicate row added
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")  # not doubled


# D7 — second fetch adds a refund tx
def test_d7_second_fetch_adds_refund():
    db = FakeDB()
    sale = _tx("skuA", "stmt1", revenue_over={"subtotal_before_discount_amount": "1000"})
    db.apply_order("orderA", [sale])
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")

    refund = _tx("skuA", "stmt2", revenue_over={"refund_subtotal_before_discount_amount": "-1000"})
    db.apply_order("orderA", [sale, refund])  # a later poll now includes the sale again + the new refund
    assert len(db.ledger_rows_for("orderA")) == 2  # sale re-seen (no dup), refund newly added
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("0")


# D8 — second fetch "removes"/replaces a tx (IMMUTABLE_EVENT_LEDGER semantics:
# an old entry that TikTok stops returning must NOT be un-counted from the
# ledger, since this session's evidence classifies the source as an
# immutable append-only ledger, not a replaceable snapshot).
def test_d8_disappearing_tx_stays_in_ledger_under_immutable_semantics():
    db = FakeDB()
    tx1 = _tx("skuA", "stmt1", revenue_over={"subtotal_before_discount_amount": "1000"})
    db.apply_order("orderA", [tx1])
    assert len(db.ledger_rows_for("orderA")) == 1

    # A later fetch no longer includes stmt1 (hypothetical — not observed
    # live, but the ledger table must never retroactively drop it: this
    # apply_order call only ADDS what it sees, it never deletes).
    tx2 = _tx("skuB", "stmt2", revenue_over={"subtotal_before_discount_amount": "500"})
    db.apply_order("orderA", [tx2])
    assert len(db.ledger_rows_for("orderA")) == 2  # stmt1's row still present -- never deleted
    assert db.sku_fee[("orderA", "skuA")]["computed_revenue"] == Decimal("1000")  # unaffected by the other sku's later-only fetch


# D9 — unknown non-zero shipping component
def test_d9_unknown_nonzero_component_flags_warning_but_does_not_crash():
    db = FakeDB()
    tx = _tx("skuA", "stmt1", extra_shipping={"brand_new_fee_field_amount": "-999"})
    db.apply_order("orderA", [tx])  # must not raise
    assert len(db.dq_warnings) == 1
    assert "brand_new_fee_field_amount" in db.dq_warnings[0]
    # computed value still produced from the KNOWN formula fields (doesn't crash/block ingestion)
    assert db.sku_fee[("orderA", "skuA")]["computed_shipping_cost"] == Decimal("0")


# D10 — explicit zero vs NULL at the write-path level
def test_d10_explicit_zero_vs_null_at_aggregate_level():
    db = FakeDB()
    tx_explicit_zero = _tx("skuA", "stmt1")  # all shipping fields explicit "0"
    db.apply_order("orderA", [tx_explicit_zero])
    assert db.sku_fee[("orderA", "skuA")]["computed_shipping_cost"] == Decimal("0")  # real zero, not None

    tx_partial = dict(_tx("skuB", "stmt2"))
    del tx_partial["shipping_cost_breakdown"]["return_shipping_fee_amount"]  # partial -- one field missing
    db.apply_order("orderA", [tx_partial])
    assert db.sku_fee[("orderA", "skuB")]["computed_shipping_cost"] is None  # PARTIAL != COMPLETE, never fabricated


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
