"""P16 — pure payout ("tiền về") rules. No DB, no I/O — unit-tested in
scripts/test_p16_payout_logic.py.

Per order, attributed to the order's own business_date:

    SHOPEE  escrow_estimate row with a value (escrow_amount_after_adjustment)
              -> ACTUAL   when the order is terminal (COMPLETED / CANCELLED)
              -> ESTIMATE otherwise (Shopee's own calculation, can still move)
    TIKTOK  MATCHED row with settlement_amount            -> ACTUAL
            otherwise est_settlement_amount from /unsettled -> ESTIMATE
    neither -> MISSING, except a CANCELLED order with no platform value,
               which is NO_PAYOUT_EXPECTED (not counted anywhere).

A value is never invented: no % fee formula, no 0 default. Mirrors the
legacy hh-lpm-etl precedence (settlement if present, else estimate) but the
estimate is the PLATFORM's own figure, not a locally computed one.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

ACTUAL = "ACTUAL"
ESTIMATE = "ESTIMATE"
MISSING = "MISSING"
NO_PAYOUT_EXPECTED = "NO_PAYOUT_EXPECTED"

SHOPEE_TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED"})


def classify_order(channel: str, order_status: Optional[str], settlement_type: Optional[str],
                   settlement_amount: Optional[Decimal],
                   est_settlement_amount: Optional[Decimal]) -> tuple[str, Optional[Decimal]]:
    status = (order_status or "").upper()
    if channel == "SHOPEE":
        if settlement_type == "escrow_estimate" and settlement_amount is not None:
            return (ACTUAL if status in SHOPEE_TERMINAL_STATUSES else ESTIMATE), settlement_amount
    elif channel == "TIKTOK":
        if settlement_type == "MATCHED" and settlement_amount is not None:
            return ACTUAL, settlement_amount
        if settlement_type != "MATCHED" and est_settlement_amount is not None:
            return ESTIMATE, est_settlement_amount
    if status == "CANCELLED":
        return NO_PAYOUT_EXPECTED, None
    return MISSING, None


def payout_status(n_actual: int, n_estimated: int, n_missing: int) -> str:
    n_eligible = n_actual + n_estimated + n_missing
    if n_eligible == 0:
        return "NO_ELIGIBLE_ORDERS"
    if n_missing == n_eligible:
        return "MISSING_SOURCE"
    if n_missing > 0:
        return "PARTIAL_MISSING_SOURCE"
    if n_estimated > 0 and n_actual > 0:
        return "MIXED_ACTUAL_AND_ESTIMATE"
    if n_estimated > 0:
        return "PLATFORM_ESTIMATE"
    return "ACTUAL"


def aggregate(classified: Iterable[tuple[str, Optional[Decimal]]]) -> dict:
    """classified: (basis, value) per order of ONE (business_date, channel, shop)."""
    n = {ACTUAL: 0, ESTIMATE: 0, MISSING: 0}
    total = {ACTUAL: Decimal(0), ESTIMATE: Decimal(0)}
    for basis, value in classified:
        if basis == NO_PAYOUT_EXPECTED:
            continue
        n[basis] += 1
        if basis in total:
            total[basis] += value
    actual = total[ACTUAL] if n[ACTUAL] else None
    estimated = total[ESTIMATE] if n[ESTIMATE] else None
    known = n[ACTUAL] + n[ESTIMATE]
    return {
        "payout": (total[ACTUAL] + total[ESTIMATE]) if known else None,
        "payout_status": payout_status(n[ACTUAL], n[ESTIMATE], n[MISSING]),
        "payout_actual": actual,
        "payout_estimated": estimated,
        "payout_orders_actual": n[ACTUAL],
        "payout_orders_estimated": n[ESTIMATE],
        "payout_orders_missing": n[MISSING],
    }
