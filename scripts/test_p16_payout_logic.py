"""P16 payout ("tiền về") — pure rule tests. No DB, no network.

Run with: python3 -m pytest scripts/test_p16_payout_logic.py -v
"""
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "gold"))
sys.path.insert(0, str(ROOT / "integrations" / "tiktok_shop" / "pilot_reporting"))
sys.path.insert(0, str(ROOT / "pipelines"))

import importlib.util  # noqa: E402


def _tiktok_worker():
    """Load the dependency-free TikTok helper module by path under a unique
    name (shopee/ and tiktok_shop/ both have incr_worker.py / config.py, so
    bare imports depend on test ordering)."""
    name = "p16_tiktok_unsettled_estimate"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "integrations" / "tiktok_shop" / "pilot_reporting" / "unsettled_estimate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from _p16_payout_logic import (  # noqa: E402
    ACTUAL, ESTIMATE, MISSING, NO_PAYOUT_EXPECTED, aggregate, classify_order, payout_status,
)


# --- classify_order -------------------------------------------------------

def test_shopee_completed_escrow_is_actual():
    assert classify_order("SHOPEE", "COMPLETED", "escrow_estimate", D("100"), None) == (ACTUAL, D("100"))


def test_shopee_open_escrow_is_estimate():
    for st in ("READY_TO_SHIP", "SHIPPED", "TO_CONFIRM_RECEIVE", "PROCESSED"):
        assert classify_order("SHOPEE", st, "escrow_estimate", D("90"), None) == (ESTIMATE, D("90"))


def test_shopee_cancelled_with_escrow_value_is_actual_even_zero():
    assert classify_order("SHOPEE", "CANCELLED", "escrow_estimate", D("0"), None) == (ACTUAL, D("0"))


def test_shopee_api_error_or_no_row_is_missing():
    assert classify_order("SHOPEE", "COMPLETED", "api_error", None, None) == (MISSING, None)
    assert classify_order("SHOPEE", "SHIPPED", None, None, None) == (MISSING, None)


def test_tiktok_matched_is_actual_and_wins_over_estimate():
    assert classify_order("TIKTOK", "COMPLETED", "MATCHED", D("250"), D("260")) == (ACTUAL, D("250"))


def test_tiktok_unsettled_uses_platform_estimate():
    assert classify_order("TIKTOK", "DELIVERED", "NOT_SETTLED_YET", None, D("259293")) == (ESTIMATE, D("259293"))
    assert classify_order("TIKTOK", "IN_TRANSIT", "UNKNOWN", None, D("10")) == (ESTIMATE, D("10"))


def test_tiktok_unsettled_without_estimate_is_missing_never_zero():
    assert classify_order("TIKTOK", "DELIVERED", "NOT_SETTLED_YET", None, None) == (MISSING, None)


def test_cancelled_without_platform_value_is_excluded():
    assert classify_order("TIKTOK", "CANCELLED", "NOT_SETTLED_YET", None, None) == (NO_PAYOUT_EXPECTED, None)
    assert classify_order("SHOPEE", "CANCELLED", None, None, None) == (NO_PAYOUT_EXPECTED, None)


def test_matched_with_null_amount_does_not_fall_back_to_estimate():
    # A MATCHED row is the final state; an old estimate must not resurface.
    assert classify_order("TIKTOK", "COMPLETED", "MATCHED", None, D("5")) == (MISSING, None)


# --- payout_status / aggregate ---------------------------------------------

def test_status_precedence():
    assert payout_status(0, 0, 0) == "NO_ELIGIBLE_ORDERS"
    assert payout_status(0, 0, 3) == "MISSING_SOURCE"
    assert payout_status(2, 1, 1) == "PARTIAL_MISSING_SOURCE"
    assert payout_status(2, 1, 0) == "MIXED_ACTUAL_AND_ESTIMATE"
    assert payout_status(0, 4, 0) == "PLATFORM_ESTIMATE"
    assert payout_status(5, 0, 0) == "ACTUAL"


def test_aggregate_sums_and_counts():
    agg = aggregate([(ACTUAL, D("100")), (ESTIMATE, D("40")), (MISSING, None), (NO_PAYOUT_EXPECTED, None)])
    assert agg["payout"] == D("140")
    assert agg["payout_actual"] == D("100")
    assert agg["payout_estimated"] == D("40")
    assert (agg["payout_orders_actual"], agg["payout_orders_estimated"], agg["payout_orders_missing"]) == (1, 1, 1)
    assert agg["payout_status"] == "PARTIAL_MISSING_SOURCE"


def test_aggregate_all_missing_is_null_not_zero():
    agg = aggregate([(MISSING, None), (MISSING, None)])
    assert agg["payout"] is None and agg["payout_actual"] is None and agg["payout_estimated"] is None
    assert agg["payout_status"] == "MISSING_SOURCE"


def test_aggregate_estimate_moves_to_actual_after_settlement():
    before = aggregate([classify_order("TIKTOK", "DELIVERED", "NOT_SETTLED_YET", None, D("260"))])
    after = aggregate([classify_order("TIKTOK", "COMPLETED", "MATCHED", D("255"), D("260"))])
    assert (before["payout_estimated"], before["payout_actual"]) == (D("260"), None)
    assert (after["payout_estimated"], after["payout_actual"], after["payout"]) == (None, D("255"), D("255"))
    assert after["payout_status"] == "ACTUAL"


def test_aggregate_keeps_real_negative_payouts():
    agg = aggregate([(ACTUAL, D("-30000")), (ACTUAL, D("50000"))])
    assert agg["payout"] == D("20000")


# --- gold row building --------------------------------------------------------

def test_build_rows_groups_by_order_business_date():
    from _p16_payout_gold import build_rows
    rows = build_rows([
        ("2026-11-20", "TIKTOK", "s1", "COMPLETED", "MATCHED", D("100"), None),
        ("2026-11-20", "TIKTOK", "s1", "DELIVERED", "NOT_SETTLED_YET", None, D("50")),
        ("2026-11-21", "TIKTOK", "s1", "DELIVERED", "NOT_SETTLED_YET", None, None),
    ])
    by = {(r[0], r[3]): (r[4], r[5]) for r in rows}
    assert by[("2026-11-20", "payout")] == (D("150"), "MIXED_ACTUAL_AND_ESTIMATE")
    assert by[("2026-11-20", "payout_actual")] == (D("100"), "ACTUAL")
    assert by[("2026-11-20", "payout_estimated")] == (D("50"), "PLATFORM_ESTIMATE")
    assert by[("2026-11-21", "payout")] == (None, "MISSING_SOURCE")
    assert by[("2026-11-21", "payout_actual")] == (None, "MISSING_SOURCE")
    assert by[("2026-11-21", "payout_orders_missing")] == (1, "READY")
    assert len(rows) == 2 * 6


# --- TikTok worker estimate mapping -----------------------------------------

def test_unsettled_estimate_maps_platform_fields():
    unsettled_estimate = _tiktok_worker().unsettled_estimate
    est = unsettled_estimate({
        "order_id": "586012397469927110", "est_settlement_amount": "259293",
        "est_revenue_amount": "344000", "est_fee_tax_amount": "-84707",
        "est_shipping_cost_amount": "0", "unsettled_reason": "WAITING_FOR_PACKAGE_DELIVERY",
    })
    assert est == {
        "est_settlement_amount": D("259293"), "est_revenue_amount": D("344000"),
        "est_fee_tax_amount": D("-84707"), "est_shipping_cost_amount": D("0"),
        "est_unsettled_reason": "WAITING_FOR_PACKAGE_DELIVERY",
    }


def test_unsettled_estimate_missing_row_or_blank_is_none():
    unsettled_estimate = _tiktok_worker().unsettled_estimate
    assert unsettled_estimate(None)["est_settlement_amount"] is None
    assert unsettled_estimate({"est_settlement_amount": ""})["est_settlement_amount"] is None


class _FakeCur:
    def __init__(self):
        self.calls, self.rowcount = [], 1

    def execute(self, sql, params):
        self.calls.append((sql, params))


def test_refresh_only_updates_non_matched_and_skips_window_orders():
    refresh_unsettled_estimates = _tiktok_worker().refresh_unsettled_estimates
    cur = _FakeCur()
    n = refresh_unsettled_estimates(cur, "shop", {
        "a": {"est_settlement_amount": "10"},
        "b": {"est_settlement_amount": "20"},
        "c": {"est_settlement_amount": ""},
    }, skip={"b"})
    assert n == 1 and len(cur.calls) == 1
    sql, params = cur.calls[0]
    assert "IS DISTINCT FROM 'MATCHED'" in sql and "UPDATE core.fact_settlement" in sql
    assert params[0] == D("10") and params[-1] == "a"
