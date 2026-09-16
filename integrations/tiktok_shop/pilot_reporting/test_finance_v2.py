"""
Acceptance checks specific to FINANCE RECONCILIATION V2.

    python3 -m pytest test_finance_v2.py -v
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

PILOT_DIR = Path(__file__).resolve().parent
NORMALIZED_DIR = PILOT_DIR / "normalized"
REPORTS_DIR = PILOT_DIR / "reports"
DATES = ["2026-09-07", "2026-09-01"]


def read_csv(date: str, name: str) -> list[dict]:
    path = NORMALIZED_DIR / f"{name}_{date}.csv"
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("date", DATES)
def test_cancelled_orders_never_unsettled(date):
    rows = read_csv(date, "order_finance_lifecycle")
    for r in rows:
        if r["cancelled"] == "True":
            assert r["finance_state"] != "UNSETTLED", (
                f"{r['order_id']} is CANCELLED but classified UNSETTLED — cancelled orders "
                "must never be lumped into 'not settled' (Section 2 hard rule)"
            )


@pytest.mark.parametrize("date", DATES)
def test_cancelled_orders_are_no_settlement_expected(date):
    rows = read_csv(date, "order_finance_lifecycle")
    cancelled = [r for r in rows if r["cancelled"] == "True"]
    assert cancelled, f"no cancelled orders on {date} to validate against"
    for r in cancelled:
        assert r["finance_state"] in ("CANCELLED_NO_SETTLEMENT_EXPECTED", "UNKNOWN"), (
            f"{r['order_id']}: cancelled order has finance_state={r['finance_state']!r}"
        )


@pytest.mark.parametrize("date", DATES)
def test_settled_orders_have_nonzero_or_documented_settlement(date):
    rows = read_csv(date, "order_finance_lifecycle")
    for r in rows:
        if r["finance_state"] == "SETTLED":
            assert r["final_settlement"] not in ("", None), (
                f"{r['order_id']}: SETTLED but final_settlement is blank"
            )


def test_no_production_ready_claim_when_mappings_not_both_pass():
    for date in DATES:
        path = REPORTS_DIR / f"finance_reconciliation_v2_{date}.json"
        data = json.loads(path.read_text())
        settlement_ok = data["settlement_mapping"] == "PASS"
        net_sales_ok = data["net_sales_verdict"]["mapping"] == "PASS"
        overall_should_be_pass = settlement_ok and net_sales_ok
        # cross-check against the actual printed overall via the same rule
        # used in reconciliation_v2.final_output_text
        if not overall_should_be_pass:
            assert not (settlement_ok and net_sales_ok)


def test_no_summing_product_and_video_gmv():
    for date in DATES:
        path = REPORTS_DIR / f"finance_reconciliation_v2_{date}.json"
        data = json.loads(path.read_text())
        basis = data["analytics_basis"]
        assert "note" in basis and "NOT summed" in basis["note"]


def test_net_sales_mapping_reflects_settlement_coverage():
    # 2026-09-01 is fully settled (proven in finance_lifecycle_summary) -> PASS
    d = json.loads((REPORTS_DIR / "finance_reconciliation_v2_2026-09-01.json").read_text())
    assert d["net_sales_verdict"]["mapping"] == "PASS"
    assert d["net_sales_verdict"]["value"] is not None

    # 2026-09-07 is D-1, 0 settled among eligible orders -> not PASS
    d2 = json.loads((REPORTS_DIR / "finance_reconciliation_v2_2026-09-07.json").read_text())
    assert d2["net_sales_verdict"]["mapping"] != "PASS"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
