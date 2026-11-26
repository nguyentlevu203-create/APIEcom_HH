"""P16 — TikTok unsettled-order payout estimate helpers.

Kept dependency-free (no config / client imports) so it can be unit-tested
without the TikTok session stack. Used by incr_worker.run_finance().

TikTok /finance/202507/orders/unsettled returns the platform's own payout
estimate per not-yet-settled order:
    est_settlement_amount = est_revenue_amount + est_fee_tax_amount (negative)
                            + est_shipping_cost_amount
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

EMPTY_ESTIMATE = {
    "est_settlement_amount": None, "est_revenue_amount": None, "est_fee_tax_amount": None,
    "est_shipping_cost_amount": None, "est_unsettled_reason": None,
}


def _dec(v) -> Optional[Decimal]:
    # Same contract as pipelines/incr_common.dec: None/"" -> None, "0" -> 0.
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def unsettled_estimate(row) -> dict:
    """Pure: one unsettled-list row -> est_* column values (None if absent)."""
    if not row:
        return dict(EMPTY_ESTIMATE)
    return {
        "est_settlement_amount": _dec(row.get("est_settlement_amount")),
        "est_revenue_amount": _dec(row.get("est_revenue_amount")),
        "est_fee_tax_amount": _dec(row.get("est_fee_tax_amount")),
        "est_shipping_cost_amount": _dec(row.get("est_shipping_cost_amount")),
        "est_unsettled_reason": row.get("unsettled_reason") or None,
    }


def refresh_unsettled_estimates(cur, shop_id, unsettled_by_order, skip=frozenset()) -> int:
    """Refresh the estimate on EXISTING, non-MATCHED fact_settlement rows for
    every order in the unsettled list (skip = orders already written this
    run). Never inserts rows, never touches MATCHED rows."""
    refreshed = 0
    for oid, row in unsettled_by_order.items():
        if oid in skip:
            continue
        est = unsettled_estimate(row)
        if est["est_settlement_amount"] is None:
            continue
        cur.execute(
            """
            UPDATE core.fact_settlement SET
                est_settlement_amount = %s, est_revenue_amount = %s, est_fee_tax_amount = %s,
                est_shipping_cost_amount = %s, est_unsettled_reason = %s, est_updated_at = now()
            WHERE channel = 'TIKTOK' AND shop_id = %s AND settlement_id = %s
              AND settlement_type IS DISTINCT FROM 'MATCHED';
            """,
            (est["est_settlement_amount"], est["est_revenue_amount"], est["est_fee_tax_amount"],
             est["est_shipping_cost_amount"], est["est_unsettled_reason"], shop_id, oid),
        )
        refreshed += cur.rowcount
    return refreshed
