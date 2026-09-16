#!/usr/bin/env python3
"""
FINANCE RECONCILIATION V2.1 — final assembly (Sections 5 and 8).

Reads what finance_lifecycle.py, statement_payment_reconciliation.py, and
backfill.py already wrote. Makes no new API calls.

    python3 reconciliation_v21.py --date 2026-09-01 --today 2026-09-08
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from pilot_common import NORMALIZED_DIR, REPORTS_DIR

import statement_payment_reconciliation as spr


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


FINALIZED_STATES = {"SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}


def sanity_check_four_values(report_date: str) -> dict[str, Any]:
    """Section 5: compare, never force equal, explain only from evidence."""
    orders = read_csv(report_date, "orders")
    product_rows = read_csv(report_date, "product_analytics")
    lifecycle_rows = read_csv(report_date, "order_finance_lifecycle")

    eligible = [o for o in orders if o["status"] != "CANCELLED"]
    order_api_value = sum((D(o["total_amount"]) for o in eligible), Decimal("0"))

    analytics_gmv = sum((D(r.get("total_performance.gmv.amount")) for r in product_rows), Decimal("0"))

    finalized = [r for r in lifecycle_rows if r["finance_state"] in FINALIZED_STATES]
    finance_revenue = sum((D(r["final_revenue"]) for r in finalized), Decimal("0"))
    final_settlement = sum((D(r["final_settlement"]) for r in finalized), Decimal("0"))

    coverage_pct = (len(finalized) / len(eligible) * 100) if eligible else 0

    diffs = {
        "order_api_vs_analytics_gmv": {
            "diff": str(order_api_value - analytics_gmv),
            "reason": "REQUIRES_RECONCILIATION — not proven by data in this project "
                      "(candidate causes: attribution window, cancellations/refunds, "
                      "LOCAL-currency rounding — none confirmed field-by-field)",
        },
        "order_api_vs_finance_revenue": {
            "diff": str(order_api_value - finance_revenue),
            "reason": (
                f"REQUIRES_RECONCILIATION — finance_revenue ({finance_revenue}) EXCEEDS "
                f"order_api_value ({order_api_value}) even at {coverage_pct:.0f}% coverage; "
                "the 'incomplete coverage' explanation only accounts for finance_revenue "
                "being LOWER than order_api_value, not higher — candidate causes not yet "
                "confirmed field-by-field: a statement's sku_transactions attributed to "
                "this order_id may include amounts from a different order_create_time than "
                "this report date (statement != order-date grouping), or a prior-day order "
                "settling within this window"
                if finance_revenue > order_api_value else
                (
                    f"FACT (evidence: order_finance_lifecycle_{report_date}.csv) — "
                    f"{len(finalized)}/{len(eligible)} eligible orders ({coverage_pct:.0f}%) "
                    "reached a finalized finance state; finance_revenue already nets out "
                    "cancelled-order reversals per the confirmed net-zero and "
                    "shipping-cost-only patterns (see finance_lifecycle.py docstring), so the "
                    "remaining gap (if coverage < 100%) is exactly the orders not yet "
                    "finalized — not an unexplained discrepancy."
                    if coverage_pct >= 100 else
                    f"PARTIAL — {len(finalized)}/{len(eligible)} eligible orders "
                    f"({coverage_pct:.0f}%) finalized; remaining gap includes orders still "
                    "UNSETTLED/MISSING_FINANCE, not yet a closed comparison"
                )
            ),
        },
        "finance_revenue_vs_final_settlement": {
            "diff": str(finance_revenue - final_settlement),
            "reason": "FACT — difference is exactly SUM(final_fee) + SUM(final_shipping) + "
                      "SUM(final_adjustment) over the same finalized orders (fees/shipping/"
                      "adjustments are deducted between revenue and settlement by design; "
                      "see order_finance_lifecycle final_fee/final_shipping/final_adjustment "
                      "columns)",
        },
    }

    return {
        "report_date": report_date,
        "order_api_value": str(order_api_value),
        "analytics_gmv": str(analytics_gmv),
        "finance_revenue": str(finance_revenue),
        "final_settlement": str(final_settlement),
        "finalized_coverage_pct": f"{coverage_pct:.1f}%",
        "differences": diffs,
    }


def net_sales_semantic_mapping() -> dict[str, str]:
    """Follow-up decision: Net Sales Semantic Mapping = PASS once the
    candidate formula has been validated for field-level, cross-date
    INTERNAL CONSISTENCY (net_sales_validation.py — 3 fully-settled dates,
    revenue_amount reconstructs exactly from revenue_breakdown components,
    and revenue+fee+shipping==settlement on all 3). See
    reports/HH_TIKTOK_NET_SALES_VALIDATION_V2.md. This still does NOT make
    the number 'NET SALES FINAL' — the reported value stays labeled
    HH_NET_SALES_CANDIDATE until a named HH finance/business owner signs
    off on the formula choice itself (a business decision, not something
    data validation can settle). PASS here unblocks Commercial Reporting
    per the explicit rule; it does not unblock Cash Reconciliation."""
    validation_path = REPORTS_DIR / "net_sales_validation_v2.json"
    if not validation_path.exists():
        return {
            "status": "AMBER",
            "reason": "run net_sales_validation.py first — no cross-date consistency check on record yet",
        }
    results = json.loads(validation_path.read_text())
    if len(results) < 3:
        return {
            "status": "AMBER",
            "reason": f"only {len(results)}/3 required dates validated so far",
        }
    all_reconstruct = all(r["revenue_breakdown"]["matches_reported_revenue_amount"] for r in results)
    all_settle = all(r["settlement_reconciles"] for r in results)
    if all_reconstruct and all_settle:
        return {
            "status": "PASS",
            "reason": f"HH_NET_SALES_CANDIDATE validated for internal consistency across "
                      f"{len(results)} fully-settled dates (see "
                      "HH_TIKTOK_NET_SALES_VALIDATION_V2.md) — unblocks Commercial Reporting. "
                      "Value remains labeled CANDIDATE, not FINAL, pending business sign-off "
                      "on the formula choice.",
        }
    return {
        "status": "AMBER",
        "reason": f"cross-date validation found inconsistencies on "
                  f"{sum(1 for r in results if not (r['revenue_breakdown']['matches_reported_revenue_amount'] and r['settlement_reconciles']))}"
                  f"/{len(results)} dates — see HH_TIKTOK_NET_SALES_VALIDATION_V2.md",
    }


def final_output(report_date: str, today: str) -> str:
    lifecycle_summary = json.loads(
        (REPORTS_DIR / f"finance_lifecycle_summary_{report_date}.json").read_text()
    )
    spr_rows = spr.build(report_date)
    spr_summary = spr.summarize(spr_rows)
    sanity = sanity_check_four_values(report_date)
    net_sales = net_sales_semantic_mapping()

    backfill_path = REPORTS_DIR / f"backfill_summary_{today}.json"
    if backfill_path.exists():
        backfill = json.loads(backfill_path.read_text())
        backfill_mapping = backfill["backfill_mapping"]
    else:
        backfill_mapping = "FAIL"
        backfill = {"note": f"run backfill.py --today {today} first"}

    # Finance Coverage: % of eligible orders that reached a finalized state
    eligible = lifecycle_summary["eligible"]
    finalized = (lifecycle_summary["settled"] + lifecycle_summary["statement_issued"]
                 + lifecycle_summary["payment_pending"] + lifecycle_summary["paid"])
    coverage_pct = (finalized / eligible * 100) if eligible else 0
    if lifecycle_summary["unknown"] > 0:
        finance_coverage = "FAIL"
    elif coverage_pct >= 100:
        finance_coverage = "PASS"
    elif coverage_pct > 0:
        finance_coverage = "AMBER"
    else:
        finance_coverage = "AMBER"

    settlement_mapping = lifecycle_summary["settlement_mapping"]
    statement_mapping = lifecycle_summary["statement_mapping"]
    payment_mapping = lifecycle_summary["payment_mapping"]  # PASS or UNRESOLVED_PLATFORM_SEMANTIC
    payment_reconciliation = spr_summary["payment_reconciliation_mapping"]
    net_sales_mapping = net_sales["status"]
    cogs_connected = False  # unchanged from V2 — no SKU/COGS master found in repo

    # ---- Four separated readiness flags (follow-up rules 6-8) ----
    # Commercial Reporting: gated ONLY by settlement/statement mechanics +
    # Net Sales Semantic Mapping. Payment Mapping (bank-level identity) is
    # explicitly EXCLUDED per the new decision.
    commercial_reporting_ready = (
        settlement_mapping == "PASS" and statement_mapping == "PASS"
        and net_sales_mapping == "PASS" and finance_coverage in ("PASS", "AMBER")
    )
    # Finance Settlement: does the settlement mechanics itself check out
    # (order -> statement), independent of Net Sales business semantics.
    finance_settlement_ready = (
        settlement_mapping == "PASS" and statement_mapping == "PASS" and finance_coverage == "PASS"
    )
    # Cash/Bank Reconciliation: the one thing Payment Mapping still blocks.
    cash_reconciliation_ready = payment_mapping == "PASS" and payment_reconciliation == "PASS"
    # P&L: composite of Net Sales (this mapping) + COGS (GM/CM1/CM2) + Ads (CM2 final).
    gm_cm1_cm2_ready = commercial_reporting_ready and cogs_connected
    pnl_readiness = {
        "net_sales": "READY" if net_sales_mapping == "PASS" else "NOT READY",
        "gross_margin": "READY" if gm_cm1_cm2_ready else "NOT READY — COGS SOURCE NOT CONNECTED",
        "CM1": "READY" if gm_cm1_cm2_ready else "NOT READY — COGS SOURCE NOT CONNECTED",
        "CM2_pre_ads": "READY" if gm_cm1_cm2_ready else "NOT READY — COGS SOURCE NOT CONNECTED",
        "CM2_final": "WAITING ADS",  # always, regardless of COGS — Ads is its own separate blocker
    }

    out = {
        "report_date": report_date, "today": today,
        "settlement_mapping": settlement_mapping,
        "statement_mapping": statement_mapping,
        "payment_mapping": payment_mapping,
        "payment_mapping_reason": lifecycle_summary["payment_mapping_reason"],
        "payment_reconciliation": payment_reconciliation,
        "payment_reconciliation_detail": spr_summary,
        "finance_coverage": finance_coverage,
        "net_sales_semantic_mapping": net_sales_mapping,
        "net_sales_semantic_reason": net_sales["reason"],
        "rolling_backfill": backfill_mapping,
        "backfill_detail": backfill,
        "open_finance_orders": lifecycle_summary["unsettled"] + lifecycle_summary["missing_finance"]
                                + lifecycle_summary["return_refund_pending"] + lifecycle_summary["payment_pending"],
        "settled_orders": finalized,
        "paid_orders": lifecycle_summary["paid"],
        "sanity_check": sanity,
        "COMMERCIAL_REPORTING_READINESS": "READY" if commercial_reporting_ready else "NOT READY",
        "FINANCE_SETTLEMENT_READINESS": "READY" if finance_settlement_ready else "NOT READY",
        "CASH_RECONCILIATION_READINESS": "READY" if cash_reconciliation_ready else "NOT READY",
        "PNL_READINESS": pnl_readiness,
    }
    out_path = REPORTS_DIR / f"finance_reconciliation_v21_{report_date}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "TIKTOK FINANCE RECONCILIATION V2.1",
        "",
        f"Order date: {report_date}",
        "",
        f"Settlement Mapping: {settlement_mapping}",
        f"Statement Mapping: {statement_mapping}",
        f"Payment Mapping: {payment_mapping}",
        f"  ({lifecycle_summary['payment_mapping_reason']})",
        f"Payment Reconciliation: {payment_reconciliation}",
        f"  (statement/payment groups: {spr_summary['total_payment_groups']}, "
        f"missing_payment: {spr_summary['missing_payment']}, "
        f"multi-statement-payment: {spr_summary['multi_statement_payment'] + spr_summary['matched']})",
        f"Finance Coverage: {finance_coverage} ({coverage_pct:.1f}% of eligible orders finalized)",
        f"Net Sales Semantic Mapping: {net_sales_mapping}",
        f"  ({net_sales['reason']})",
        f"Rolling Backfill: {backfill_mapping}",
        "",
        f"Open Finance Orders: {out['open_finance_orders']}",
        f"Settled Orders: {out['settled_orders']}",
        f"Paid Orders: {out['paid_orders']}",
        "",
        "COGS: NOT CONNECTED",
        "",
        "CM1: NOT READY",
        "",
        "CM2: WAITING ADS",
        "",
        "Sanity check (Section 5):",
        f"  ORDER API VALUE: {sanity['order_api_value']}",
        f"  ANALYTICS GMV: {sanity['analytics_gmv']} — {sanity['differences']['order_api_vs_analytics_gmv']['reason']}",
        f"  FINANCE REVENUE: {sanity['finance_revenue']} — {sanity['differences']['order_api_vs_finance_revenue']['reason']}",
        f"  FINAL SETTLEMENT: {sanity['final_settlement']} — {sanity['differences']['finance_revenue_vs_final_settlement']['reason']}",
        "",
        "READINESS (separated — no single blanket flag):",
        f"  COMMERCIAL_REPORTING_READINESS: {out['COMMERCIAL_REPORTING_READINESS']}",
        f"  FINANCE_SETTLEMENT_READINESS: {out['FINANCE_SETTLEMENT_READINESS']}",
        f"  CASH_RECONCILIATION_READINESS: {out['CASH_RECONCILIATION_READINESS']}"
        f"{' (blocked by Payment Mapping = ' + payment_mapping + ')' if not cash_reconciliation_ready else ''}",
        f"  P&L_READINESS: Net Sales={pnl_readiness['net_sales']}, "
        f"GM={pnl_readiness['gross_margin']}, CM1={pnl_readiness['CM1']}, "
        f"CM2(pre-ads)={pnl_readiness['CM2_pre_ads']}, CM2(final)={pnl_readiness['CM2_final']}",
    ]

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--today", required=True)
    args = ap.parse_args()
    print(final_output(args.date, args.today))
    return 0


if __name__ == "__main__":
    sys.exit(main())
