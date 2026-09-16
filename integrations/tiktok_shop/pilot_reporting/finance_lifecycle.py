#!/usr/bin/env python3
"""
FINANCE RECONCILIATION V2 — Section 2/3/4 of the follow-up spec.

Builds the authoritative per-order finance lifecycle table from what
collect.py already wrote to normalized/*_<date>.csv (never calls the API
itself). Also maintains an append-only snapshot log across all runs/dates
so first_seen_unsettled / last_seen_unsettled reflect real observation
history, not a guess.

    python3 finance_lifecycle.py --date 2026-09-07
    python3 finance_lifecycle.py --date 2026-09-01

Root cause on record (Section 1 of the follow-up spec): investigated the 3
orders on 2026-09-07 that matched a statement with settlement_amount="0".
All 3 are order.status=CANCELLED. Their raw
finance_order_statement_transactions / finance_statement_transactions
records show revenue_breakdown.subtotal_before_discount_amount exactly
offset by refund_subtotal_before_discount_amount (and the same for
supplementary_component.customer_payment_amount vs customer_refund_amount)
— i.e. TikTok recognized revenue then reversed it dollar-for-dollar in the
same statement. settlement_amount=0 is the mathematically correct result
of a processed cancellation, not missing/broken data. Confirmed with a
second, older date (2026-09-01, 7 of 62 orders CANCELLED) showing the
identical pattern once enough time had passed for the reversal to post.
This finding is DATA-PROVEN, not assumed.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from pilot_common import NORMALIZED_DIR, REPORTS_DIR, now_utc_iso

try:
    from zoneinfo import ZoneInfo
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover
    VN_TZ = None

SNAPSHOT_LOG_PATH = NORMALIZED_DIR / "finance_snapshot_log.csv"
# V2.1 Section 7: observed_at/previous_state/current_state/state_changed/
# statement_id/payment_id, plus order_id/report_date/order_create_time kept
# as index fields (not in the spec's literal list but required to look a
# row back up — dropping them would make the log unusable).
SNAPSHOT_LOG_FIELDS = ["observed_at", "report_date", "order_id", "order_create_time",
                       "order_status", "previous_state", "current_state", "state_changed",
                       "statement_id", "payment_id"]

RETURN_PENDING_STATUSES = {
    "RETURN_OR_REFUND_REQUEST_PENDING", "AWAITING_BUYER_SHIP", "BUYER_SHIPPED_ITEM",
    "AWAITING_BUYER_RESPONSE",
}
# V2.1 Section 2: extended lifecycle. UNSETTLED -> SETTLED (order-level
# revenue recognized) -> STATEMENT_ISSUED (statement itself closed,
# payment_status=SETTLED at the statement) -> PAYMENT_PENDING / PAID (only
# asserted once Payments.id actually resolves — see PAYMENT_LINKAGE_NOTE).
FINANCE_STATES = {
    "UNSETTLED", "SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID",
    "CANCELLED_NO_SETTLEMENT_EXPECTED", "RETURN_REFUND_PENDING", "MISSING_FINANCE", "UNKNOWN",
}
TERMINAL_STATES = {"PAID", "CANCELLED_NO_SETTLEMENT_EXPECTED"}

# V2.1 Section 1 finding, recorded once here so every consumer of this
# module carries the same caveat: live-tested by cross-referencing all 492
# distinct statement.payment_id values (2026-09-01 + 2026-09-07 pulls)
# against the shop's ENTIRE Payments history (164 rows, full 365-day
# window, pagination exhausted — confirmed no more exist). Overlap = 0.
# statement.payment_id and Payments.id are evidently different ID
# namespaces in this live account (plausibly an internal "payment order"
# id vs the consolidated bank-transfer Payments record — TikTok's own
# pagination cursor for Get Payments literally names its field
# "payment_order_id", which is suggestive but not confirmed by docs).
# Until this resolves differently, PAID is only ever asserted when an
# actual Payments.id match is found — never assumed from payment_status
# alone.
PAYMENT_LINKAGE_NOTE = (
    "statement.payment_id not found in Get Payments history (0/492 shop-wide match rate "
    "observed on 2026-09-08) — payment identity/status not confirmable via documented "
    "linkage in this account; state held at STATEMENT_ISSUED rather than assumed PAID/PENDING"
)


def D(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    s = str(x).strip()
    if s == "":
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def read_csv(report_date: str, name: str) -> list[dict]:
    path = NORMALIZED_DIR / f"{name}_{report_date}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def epoch_to_vn_date(epoch: Any) -> Optional[str]:
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return None
    if e <= 0:
        return None
    dt = datetime.fromtimestamp(e, tz=VN_TZ or timezone.utc)
    return dt.strftime("%Y-%m-%d")


def load_payments_all() -> dict[str, dict]:
    path = NORMALIZED_DIR / "payments_all.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def bank_last4(masked: Optional[str]) -> str:
    """TikTok already masks this ('********9999') — we only ever keep the
    last 4 digits it already exposed, never more."""
    if not masked:
        return ""
    digits = "".join(c for c in masked if c.isdigit())
    return digits[-4:] if digits else ""


def load_snapshot_log() -> list[dict]:
    if not SNAPSHOT_LOG_PATH.exists():
        return []
    with open(SNAPSHOT_LOG_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_snapshot_log(rows: list[dict]) -> None:
    """Append-only — never truncates/overwrites prior runs' rows (Section 3:
    'Khong overwrite lich su')."""
    is_new = not SNAPSHOT_LOG_PATH.exists()
    with open(SNAPSHOT_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_LOG_FIELDS)
        if is_new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def first_last_seen_unsettled(log_rows: list[dict], order_id: str) -> tuple[Optional[str], Optional[str]]:
    seen = [r["observed_at"] for r in log_rows
            if r["order_id"] == order_id and r["current_state"] == "UNSETTLED"]
    if not seen:
        return None, None
    return min(seen), max(seen)


def last_known_state(log_rows: list[dict], order_id: str) -> Optional[str]:
    """Most recent current_state this order was observed in, from any prior
    run (any report_date) — used to compute state_changed for the new
    snapshot row. None if this is the first time we've ever seen it."""
    rows_for_order = [r for r in log_rows if r["order_id"] == order_id]
    if not rows_for_order:
        return None
    return max(rows_for_order, key=lambda r: r["observed_at"])["current_state"]


def resolve_payment(statement: Optional[dict], payments_by_id: dict[str, dict]) -> dict[str, Any]:
    """V2.1 Section 1: statement -> statement.payment_id -> Payments.id.
    Never joins Order directly to Payment. Returns NOT_AVAILABLE fields
    (not a guess) whenever the join doesn't resolve — see PAYMENT_LINKAGE_NOTE."""
    empty = {
        "payment_id": "", "payment_status": "", "payment_time": "",
        "payment_amount": "", "payment_settlement_amount": "",
        "payment_paid_time": "", "payment_bank_last4": "", "payment_resolved": False,
    }
    if not statement:
        return empty
    pid = statement.get("payment_id")
    if not pid:
        return empty
    payment = payments_by_id.get(pid)
    if not payment:
        return {**empty, "payment_id": pid, "payment_status": statement.get("payment_status") or ""}
    return {
        "payment_id": pid,
        "payment_status": payment.get("status") or statement.get("payment_status") or "",
        "payment_time": statement.get("payment_time") or "",
        "payment_amount": payment.get("amount_value"),
        "payment_settlement_amount": payment.get("settlement_amount_value"),
        "payment_paid_time": payment.get("paid_time"),
        "payment_bank_last4": bank_last4(payment.get("bank_account_masked")),
        "payment_resolved": True,
    }


def classify(
    order: dict, tx: Optional[dict], unsettled: Optional[dict],
    returns_for_order: list[dict], statement_by_id: dict[str, dict],
    payments_by_id: dict[str, dict],
) -> dict[str, Any]:
    status = order.get("status")
    cancelled = status == "CANCELLED"
    has_tx = tx is not None
    revenue = D(tx.get("revenue_amount")) if tx else Decimal("0")
    settlement = D(tx.get("settlement_amount")) if tx else Decimal("0")
    pending_return = any(r.get("return_status") in RETURN_PENDING_STATUSES for r in returns_for_order)

    evidence = ""
    state = "UNKNOWN"
    payment = resolve_payment(None, payments_by_id)  # default NOT_AVAILABLE, overwritten below if applicable

    if cancelled:
        state = "CANCELLED_NO_SETTLEMENT_EXPECTED"
        shipping = D(tx.get("shipping_cost_amount")) if tx else Decimal("0")
        if has_tx:
            if revenue == 0 and settlement == 0:
                evidence = ("DATA-CONFIRMED: order cancelled; statement entry shows "
                            "revenue_amount=0 and settlement_amount=0 (revenue booked then "
                            "reversed by an equal refund in the same statement — see raw "
                            "finance_order_statement_transactions revenue_breakdown / "
                            "supplementary_component for the offsetting pair)")
            elif revenue == 0 and settlement == shipping and settlement != 0:
                # Second confirmed pattern (found 2026-08-25, order
                # 585718638561887281): item already shipped before
                # cancellation, so the seller is still charged the actual
                # incurred shipping cost even though the sale itself is
                # fully reversed (revenue=0). settlement == shipping_cost
                # exactly, evidenced by sku_transactions[].
                # shipping_cost_breakdown.actual_shipping_fee_amount.
                evidence = ("DATA-CONFIRMED: order cancelled with revenue_amount=0 (sale fully "
                            f"reversed) but settlement_amount={settlement} equals "
                            "shipping_cost_amount exactly — seller was still charged the actual "
                            "incurred shipping fee (item already shipped pre-cancellation); see "
                            "sku_transactions[].shipping_cost_breakdown.actual_shipping_fee_amount")
            else:
                state = "UNKNOWN"
                evidence = (f"ANOMALY: order is CANCELLED but statement shows revenue={revenue} "
                            f"settlement={settlement} shipping={shipping} — does not match "
                            "either confirmed pattern (net-zero reversal, or shipping-cost-only "
                            "settlement); needs manual review, not auto-classified")
        else:
            evidence = ("order cancelled; no finance statement entry yet — the net-zero "
                        "reversal (confirmed pattern on other cancelled orders in this shop) "
                        "has not posted yet")
    elif has_tx and (revenue != 0 or settlement != 0):
        state = "SETTLED"
        evidence = f"order_statement_transactions: revenue={revenue}, settlement={settlement}"
        if pending_return:
            evidence += "; NOTE: a return/refund is still pending on this order post-settlement"

        stmt = statement_by_id.get(tx.get("statement_id") or "")
        stmt_payment_status = (stmt.get("payment_status") if stmt else None) or ""
        if stmt_payment_status == "SETTLED":
            state = "STATEMENT_ISSUED"
            evidence += f"; statement.payment_status=SETTLED (statement {tx.get('statement_id')} closed)"
            payment = resolve_payment(stmt, payments_by_id)
            if payment["payment_resolved"]:
                if payment["payment_status"] == "PAID":
                    state = "PAID"
                    evidence += f"; payment {payment['payment_id']} resolved, status=PAID"
                else:
                    state = "PAYMENT_PENDING"
                    evidence += (f"; payment {payment['payment_id']} resolved, "
                                 f"status={payment['payment_status']!r} (not yet PAID)")
            else:
                evidence += f"; {PAYMENT_LINKAGE_NOTE}"
        elif stmt_payment_status:
            evidence += f"; statement.payment_status={stmt_payment_status!r} (not yet SETTLED at statement level)"
    elif has_tx:
        state = "UNKNOWN"
        evidence = ("order is NOT cancelled but matched a statement with all-zero amounts — "
                    "does not match any confirmed pattern; needs manual review")
    elif unsettled is not None:
        state = "UNSETTLED"
        evidence = f"Get Unsettled Transactions: unsettled_reason={unsettled.get('unsettled_reason')!r}"
    elif pending_return:
        state = "RETURN_REFUND_PENDING"
        statuses = {r.get("return_status") for r in returns_for_order}
        evidence = f"active return(s) with status in {sorted(statuses)}, order not yet in a statement"
    else:
        state = "MISSING_FINANCE"
        pre_delivery = status in ("AWAITING_SHIPMENT", "PARTIALLY_SHIPPING",
                                   "AWAITING_COLLECTION", "IN_TRANSIT", "ON_HOLD", "UNPAID")
        if pre_delivery:
            evidence = (f"order not yet in a statement AND not present in Get Unsettled "
                        f"Transactions (order status={status!r}, not yet DELIVERED) — observed "
                        "pattern: TikTok appears to only start tracking an order in the "
                        "Unsettled Transactions feed once it reaches/approaches DELIVERED; "
                        "this is a data-observed lifecycle gap, not an error, but should not "
                        "be silently counted as UNSETTLED since it doesn't meet that API's own "
                        "criteria")
        else:
            evidence = ("order not cancelled, not matched to any statement, not in Get "
                        "Unsettled Transactions, and no return on file — unaccounted for in "
                        "finance data as of this run; needs investigation, not assumed settled")

    statement_id = (tx.get("statement_id") if tx else None) or ""
    statement_date = None
    statement_row = statement_by_id.get(statement_id) if statement_id else None
    if statement_row:
        statement_date = epoch_to_vn_date(statement_row.get("statement_time"))
        # payment_status doubles as the only statement-level status field
        # TikTok exposes on Get Statements (no separate 'status' field).
        statement_status = statement_row.get("payment_status") or ""
        statement_time = statement_row.get("statement_time") or ""
    else:
        statement_status = ""
        statement_time = ""

    return {
        "finance_state": state,
        "evidence": evidence,
        "statement_id": statement_id,
        "statement_date": statement_date,
        "statement_status": statement_status,
        "statement_time": statement_time,
        "payment_id": payment["payment_id"],
        "payment_status": payment["payment_status"],
        "payment_time": payment["payment_time"],
        "payment_amount": payment["payment_amount"],
        "payment_settlement_amount": payment["payment_settlement_amount"],
        "payment_paid_time": payment["payment_paid_time"],
        "payment_bank_last4": payment["payment_bank_last4"],
        "final_revenue": tx.get("revenue_amount") if tx else "",
        "final_fee": tx.get("fee_and_tax_amount") if tx else "",
        "final_shipping": tx.get("shipping_cost_amount") if tx else "",
        "final_settlement": tx.get("settlement_amount") if tx else "",
    }


def build_lifecycle(report_date: str) -> Path:
    orders = read_csv(report_date, "orders")
    match_rows = read_csv(report_date, "finance_match_status")
    order_tx_rows = read_csv(report_date, "order_finance_transactions")
    finance_tx_rows = read_csv(report_date, "finance_transactions")  # statement-level, has adjustment_amount
    unsettled_rows = read_csv(report_date, "finance_unsettled")
    statement_rows = read_csv(report_date, "finance_statements")
    returns_rows = read_csv(report_date, "returns")

    tx_by_order = {r["order_id"]: r for r in order_tx_rows}
    match_by_order = {r["order_id"]: r for r in match_rows}
    unsettled_by_order = {r["order_id"]: r for r in unsettled_rows}
    statement_by_id = {r["id"]: r for r in statement_rows}
    payments_by_id = load_payments_all()
    returns_by_order: dict[str, list[dict]] = {}
    for r in returns_rows:
        returns_by_order.setdefault(r.get("order_id"), []).append(r)
    # adjustment_amount only lives in finance_transactions.csv (statement-level)
    adjustment_by_order: dict[str, str] = {}
    # Fallback source: the per-order call (finance_order_statement_transactions)
    # can come back empty for an order that a transient rate-limit hit even
    # after retries (observed live, e.g. 2026-09-01 order 585829547961910796),
    # while the SAME order's real settlement figures are still sitting in the
    # broader statement-level pull (finance_transactions.csv) we already have
    # on disk. Use that as a fallback rather than reporting a real settled
    # order as MISSING_FINANCE.
    stmt_tx_by_order: dict[str, dict] = {}
    for r in finance_tx_rows:
        oid = r.get("order_id")
        if not oid:
            continue
        if oid not in adjustment_by_order:
            adjustment_by_order[oid] = r.get("adjustment_amount")
        if oid not in stmt_tx_by_order:
            stmt_tx_by_order[oid] = {
                "statement_id": r.get("statement_id"),
                "revenue_amount": r.get("revenue_amount"),
                "fee_and_tax_amount": r.get("fee_tax_amount"),
                "shipping_cost_amount": r.get("shipping_cost_amount"),
                "settlement_amount": r.get("settlement_amount"),
            }

    run_ts = now_utc_iso()
    snapshot_rows = []
    lifecycle_rows = []
    prior_log = load_snapshot_log()  # read BEFORE appending this run's rows

    fallback_used = 0
    for o in orders:
        oid = o["order_id"]
        tx = tx_by_order.get(oid)
        used_fallback = False
        if tx is None and oid in stmt_tx_by_order:
            tx = stmt_tx_by_order[oid]
            used_fallback = True
            fallback_used += 1
        unsettled = unsettled_by_order.get(oid)
        returns_for_order = returns_by_order.get(oid, [])
        result = classify(o, tx, unsettled, returns_for_order, statement_by_id, payments_by_id)
        if used_fallback:
            result["evidence"] += (" [source: finance_transactions.csv statement-level "
                                    "fallback — the per-order call came back empty/failed "
                                    "for this order even after retries]")

        prev_state = last_known_state(prior_log, oid)
        snapshot_rows.append({
            "observed_at": run_ts, "report_date": report_date, "order_id": oid,
            "order_create_time": o.get("create_time"), "order_status": o.get("status"),
            "previous_state": prev_state or "",
            "current_state": result["finance_state"],
            "state_changed": prev_state is not None and prev_state != result["finance_state"],
            "statement_id": result["statement_id"],
            "payment_id": result["payment_id"],
        })

        lifecycle_rows.append({
            "order_id": oid,
            "order_create_date": epoch_to_vn_date(o.get("create_time")) or report_date,
            "order_status": o.get("status"),
            "eligible_sale": o.get("status") != "CANCELLED",
            "cancelled": o.get("status") == "CANCELLED",
            "finance_state": result["finance_state"],
            "first_seen_unsettled": "",  # filled below, using the FULL log incl. this run
            "last_seen_unsettled": "",
            "statement_id": result["statement_id"],
            "statement_status": result["statement_status"],
            "statement_time": result["statement_time"],
            "statement_date": result["statement_date"] or "",
            "settled_date": result["statement_date"] if result["finance_state"] in
                             ("SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID") else "",
            "payment_id": result["payment_id"],
            "payment_status": result["payment_status"],
            "payment_time": result["payment_time"],
            "payment_amount": result["payment_amount"],
            "payment_settlement_amount": result["payment_settlement_amount"],
            "payment_paid_time": result["payment_paid_time"],
            "payment_bank_last4": result["payment_bank_last4"],
            "estimated_revenue": unsettled.get("est_revenue_amount") if unsettled else "",
            "estimated_fee": unsettled.get("est_fee_tax_amount") if unsettled else "",
            "estimated_settlement": unsettled.get("est_settlement_amount") if unsettled else "",
            "final_revenue": result["final_revenue"],
            "final_fee": result["final_fee"],
            "final_shipping": result["final_shipping"],
            "final_adjustment": adjustment_by_order.get(oid, ""),
            "final_settlement": result["final_settlement"],
            "evidence": result["evidence"],
        })

    append_snapshot_log(snapshot_rows)
    full_log = prior_log + snapshot_rows
    for row in lifecycle_rows:
        first_seen, last_seen = first_last_seen_unsettled(full_log, row["order_id"])
        row["first_seen_unsettled"] = first_seen or ""
        row["last_seen_unsettled"] = last_seen or ""

    out_path = NORMALIZED_DIR / f"order_finance_lifecycle_{report_date}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = list(lifecycle_rows[0].keys()) if lifecycle_rows else [
            "order_id", "order_create_date", "order_status", "eligible_sale", "cancelled",
            "finance_state", "first_seen_unsettled", "last_seen_unsettled", "statement_id",
            "statement_status", "statement_time", "statement_date", "settled_date",
            "payment_id", "payment_status", "payment_time", "payment_amount",
            "payment_settlement_amount", "payment_paid_time", "payment_bank_last4",
            "estimated_revenue", "estimated_fee", "estimated_settlement", "final_revenue",
            "final_fee", "final_shipping", "final_adjustment", "final_settlement", "evidence",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in lifecycle_rows:
            w.writerow(row)

    if fallback_used:
        print(f"[finance_lifecycle] {fallback_used} order(s) used the statement-level "
              f"fallback source (per-order call was empty/failed after retries)")

    return out_path


FINALIZED_STATES = {"SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}
STATEMENT_CLOSED_STATES = {"STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}
PAYMENT_RESOLVED_STATES = {"PAYMENT_PENDING", "PAID"}


def summarize(report_date: str, lifecycle_path: Path) -> dict[str, Any]:
    with open(lifecycle_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    from collections import Counter
    state_counts = Counter(r["finance_state"] for r in rows)
    total = len(rows)
    cancelled = sum(1 for r in rows if r["cancelled"] == "True")
    eligible = total - cancelled

    finalized_rows = [r for r in rows if r["finance_state"] in FINALIZED_STATES]
    unsettled_rows = [r for r in rows if r["finance_state"] == "UNSETTLED"]
    statement_closed_rows = [r for r in rows if r["finance_state"] in STATEMENT_CLOSED_STATES]
    payment_resolved_rows = [r for r in rows if r["finance_state"] in PAYMENT_RESOLVED_STATES]

    est_revenue = sum((D(r["estimated_revenue"]) for r in unsettled_rows), Decimal("0"))
    est_fee = sum((D(r["estimated_fee"]) for r in unsettled_rows), Decimal("0"))
    est_settlement = sum((D(r["estimated_settlement"]) for r in unsettled_rows), Decimal("0"))

    final_revenue = sum((D(r["final_revenue"]) for r in finalized_rows), Decimal("0"))
    final_fee = sum((D(r["final_fee"]) for r in finalized_rows), Decimal("0"))
    final_shipping = sum((D(r["final_shipping"]) for r in finalized_rows), Decimal("0"))
    final_adjustment = sum((D(r["final_adjustment"]) for r in finalized_rows), Decimal("0"))
    final_settlement = sum((D(r["final_settlement"]) for r in finalized_rows), Decimal("0"))

    settlement_mapping_pass = state_counts.get("UNKNOWN", 0) == 0 and (
        len(finalized_rows) == 0 or all(D(r["final_settlement"]) != 0 or r["order_status"] == "CANCELLED" for r in finalized_rows)
    )

    # Statement Mapping: every order that reached a "revenue recognized"
    # state must carry a resolvable statement_id (Section 8).
    statement_mapping_pass = state_counts.get("UNKNOWN", 0) == 0 and all(
        r["statement_id"] for r in finalized_rows
    )

    # Payment Mapping (V2.1 decision, superseded by V2.1 follow-up): stop
    # trying to force-resolve statement.payment_id against Payments.id
    # without official TikTok evidence that they're the same ID space (see
    # PAYMENT_LINKAGE_NOTE and reports/HH_TIKTOK_PAYMENT_ID_SUPPORT_CASE.md).
    # This is now a fixed semantic status, not a PASS/AMBER/FAIL verdict —
    # it blocks CASH/BANK RECONCILIATION only, never Commercial/CEO
    # reporting or Net Sales (business decision, not a coding gap).
    if len(payment_resolved_rows) == len(statement_closed_rows) and statement_closed_rows:
        payment_mapping = "PASS"  # only if it ever genuinely fully resolves
        payment_mapping_reason = f"{len(payment_resolved_rows)}/{len(statement_closed_rows)} resolved"
    else:
        payment_mapping = "UNRESOLVED_PLATFORM_SEMANTIC"
        payment_mapping_reason = (
            f"{len(payment_resolved_rows)}/{len(statement_closed_rows)} resolved — "
            f"{PAYMENT_LINKAGE_NOTE}. Escalated to TikTok — see "
            "reports/HH_TIKTOK_PAYMENT_ID_SUPPORT_CASE.md. Blocks CASH/BANK RECONCILIATION "
            "only; does not block Commercial Reporting."
        )

    return {
        "report_date": report_date,
        "orders": total,
        "eligible": eligible,
        "cancelled": cancelled,
        "unsettled": state_counts.get("UNSETTLED", 0),
        "settled": state_counts.get("SETTLED", 0),
        "statement_issued": state_counts.get("STATEMENT_ISSUED", 0),
        "payment_pending": state_counts.get("PAYMENT_PENDING", 0),
        "paid": state_counts.get("PAID", 0),
        "cancelled_no_settlement_expected": state_counts.get("CANCELLED_NO_SETTLEMENT_EXPECTED", 0),
        "return_refund_pending": state_counts.get("RETURN_REFUND_PENDING", 0),
        "missing_finance": state_counts.get("MISSING_FINANCE", 0),
        "unknown": state_counts.get("UNKNOWN", 0),
        "estimated_revenue": str(est_revenue),
        "estimated_fee": str(est_fee),
        "estimated_settlement": str(est_settlement),
        "final_revenue": str(final_revenue),
        "final_fee": str(final_fee),
        "final_shipping": str(final_shipping),
        "final_adjustment": str(final_adjustment),
        "final_settlement": str(final_settlement),
        "settlement_mapping": "PASS" if settlement_mapping_pass else "FAIL",
        "statement_mapping": "PASS" if statement_mapping_pass else "FAIL",
        "payment_mapping": payment_mapping,
        "payment_mapping_reason": payment_mapping_reason,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()

    path = build_lifecycle(args.date)
    summary = summarize(args.date, path)

    out_json = REPORTS_DIR / f"finance_lifecycle_summary_{args.date}.json"
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nLifecycle table: {path}")
    print(f"Summary: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
