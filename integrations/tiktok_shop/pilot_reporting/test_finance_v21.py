"""
Acceptance checks specific to FINANCE RECONCILIATION V2.1.

    python3 -m pytest test_finance_v21.py -v
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

PILOT_DIR = Path(__file__).resolve().parent
NORMALIZED_DIR = PILOT_DIR / "normalized"
REPORTS_DIR = PILOT_DIR / "reports"
DATES = ["2026-09-07", "2026-09-05", "2026-09-01", "2026-08-25"]


def read_csv(date: str, name: str) -> list[dict]:
    with open(NORMALIZED_DIR / f"{name}_{date}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("date", DATES)
def test_no_unknown_finance_states(date):
    rows = read_csv(date, "order_finance_lifecycle")
    unknown = [r for r in rows if r["finance_state"] == "UNKNOWN"]
    assert not unknown, f"{date}: {len(unknown)} order(s) still UNKNOWN — {unknown[:2]}"


@pytest.mark.parametrize("date", DATES)
def test_paid_only_when_payment_actually_resolved(date):
    """PAID must never be asserted without a real Payments.id match — no
    guessing from statement.payment_status alone."""
    rows = read_csv(date, "order_finance_lifecycle")
    for r in rows:
        if r["finance_state"] == "PAID":
            assert r["payment_id"], f"{r['order_id']}: PAID but no payment_id recorded"
            assert r["payment_status"] == "PAID"


@pytest.mark.parametrize("date", DATES)
def test_statement_issued_requires_statement_id(date):
    rows = read_csv(date, "order_finance_lifecycle")
    for r in rows:
        if r["finance_state"] in ("STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"):
            assert r["statement_id"], f"{r['order_id']}: {r['finance_state']} but no statement_id"
            assert r["statement_status"] == "SETTLED"


@pytest.mark.parametrize("date", DATES)
def test_no_direct_order_to_payment_join(date):
    """The lifecycle must only resolve payment_id through a statement — an
    order with NO statement_id must never carry a payment_id."""
    rows = read_csv(date, "order_finance_lifecycle")
    for r in rows:
        if not r["statement_id"]:
            assert not r["payment_id"], (
                f"{r['order_id']}: has payment_id {r['payment_id']!r} but no statement_id — "
                "this would mean Order was joined directly to Payment, which Section 1 forbids"
            )


def test_many_statements_to_one_payment_supported():
    rows = read_csv("2026-09-07", "statement_payment_reconciliation") if \
        (NORMALIZED_DIR / "statement_payment_reconciliation_2026-09-07.csv").exists() else []
    if not rows:
        pytest.skip("run statement_payment_reconciliation.py first")
    multi = [r for r in rows if int(r["statement_count"]) > 1]
    assert multi, "expected at least one payment_id shared by >1 statement (observed live: 2)"


@pytest.mark.parametrize("date", DATES)
def test_commercial_reporting_never_gated_by_payment_mapping(date):
    """Follow-up decision: Payment Mapping only blocks Cash Reconciliation,
    never Commercial Reporting — even when Payment Mapping is
    UNRESOLVED_PLATFORM_SEMANTIC (the expected, permanent state here)."""
    path = REPORTS_DIR / f"finance_reconciliation_v21_{date}.json"
    if not path.exists():
        pytest.skip(f"run reconciliation_v21.py --date {date} first")
    data = json.loads(path.read_text())
    if data["payment_mapping"] not in ("PASS",):
        # Payment Mapping is NOT PASS here — Commercial Reporting must still
        # be able to reach READY if settlement/statement/net-sales allow it.
        if (data["settlement_mapping"] == "PASS" and data["statement_mapping"] == "PASS"
                and data["net_sales_semantic_mapping"] == "PASS"):
            assert data["COMMERCIAL_REPORTING_READINESS"] == "READY", (
                f"{date}: Commercial Reporting incorrectly blocked by Payment Mapping"
            )


@pytest.mark.parametrize("date", DATES)
def test_cash_reconciliation_gated_by_payment_mapping_only(date):
    path = REPORTS_DIR / f"finance_reconciliation_v21_{date}.json"
    if not path.exists():
        pytest.skip(f"run reconciliation_v21.py --date {date} first")
    data = json.loads(path.read_text())
    if data["payment_mapping"] != "PASS":
        assert data["CASH_RECONCILIATION_READINESS"] == "NOT READY"


def test_four_readiness_flags_are_separate_not_one_blanket_flag():
    for date in DATES:
        path = REPORTS_DIR / f"finance_reconciliation_v21_{date}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        assert "production_reporting_ready" not in data, (
            "the old single blanket flag must be gone — use the 4 separated readiness flags"
        )
        for key in ("COMMERCIAL_REPORTING_READINESS", "FINANCE_SETTLEMENT_READINESS",
                    "CASH_RECONCILIATION_READINESS", "PNL_READINESS"):
            assert key in data, f"{date}: missing {key}"


def test_net_sales_semantic_mapping_requires_3plus_date_validation():
    """PASS is now allowed, but only backed by net_sales_validation.py's
    cross-date consistency check — never from coverage alone."""
    validation_path = REPORTS_DIR / "net_sales_validation_v2.json"
    assert validation_path.exists(), "run net_sales_validation.py first"
    results = json.loads(validation_path.read_text())
    assert len(results) >= 3, "Section 4 requires at least 3 validated dates"
    for date in DATES:
        path = REPORTS_DIR / f"finance_reconciliation_v21_{date}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if data["net_sales_semantic_mapping"] == "PASS":
            assert all(
                r["revenue_breakdown"]["matches_reported_revenue_amount"] and r["settlement_reconciles"]
                for r in results
            ), f"{date}: net_sales_semantic_mapping=PASS but validation results don't fully support it"


def test_net_sales_value_never_labeled_final():
    """Rule 5: candidate formula must always be called HH_NET_SALES_CANDIDATE,
    never asserted (unnegated) as 'NET SALES FINAL'."""
    import re
    for path in REPORTS_DIR.glob("*.md"):
        if "NET_SALES" not in path.name:
            continue
        text = path.read_text(encoding="utf-8").upper().replace("_", " ").replace("`", "")
        for m in re.finditer(r"NET SALES FINAL", text):
            preceding = text[max(0, m.start() - 12):m.start()]
            assert "NOT" in preceding, (
                f"{path.name}: 'NET SALES FINAL' appears without a negating 'NOT' nearby "
                f"— context: ...{preceding}[NET SALES FINAL]..."
            )


def test_payment_mapping_reflects_live_linkage_gap():
    """Documents the actual finding: 0/492 payment_id values resolved
    against the shop's full Payments history. If this ever legitimately
    changes (TikTok fixes the linkage, or a future payment resolves), this
    test should be revisited — it is not meant to be a permanent failure."""
    path = REPORTS_DIR / "finance_reconciliation_v21_2026-09-01.json"
    if not path.exists():
        pytest.skip("run reconciliation_v21.py --date 2026-09-01 first")
    data = json.loads(path.read_text())
    assert data["payment_mapping"] == "UNRESOLVED_PLATFORM_SEMANTIC"
    assert "0/492" in data["payment_mapping_reason"] or "not found in Get Payments" in data["payment_mapping_reason"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
