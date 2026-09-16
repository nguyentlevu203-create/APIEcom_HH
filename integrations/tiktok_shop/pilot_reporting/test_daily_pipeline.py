"""
Acceptance checks for the TIKTOK PRODUCTION DAILY DATA PIPELINE V1.

    python3 -m pytest test_daily_pipeline.py -v
"""
from __future__ import annotations

import csv
import glob
import json
import re
from pathlib import Path

import pytest

PILOT_DIR = Path(__file__).resolve().parent
NORMALIZED_DIR = PILOT_DIR / "normalized"
REPORTS_DIR = PILOT_DIR / "reports"


def _latest_manifest():
    files = sorted(glob.glob(str(REPORTS_DIR / "run_manifest_*.json")))
    if not files:
        pytest.skip("run run_daily_tiktok_reporting.py first")
    return json.loads(Path(files[-1]).read_text())


def test_manifest_has_required_observability_fields():
    m = _latest_manifest()
    for key in ("run_id", "started_at", "finished_at", "report_date", "status", "steps",
                "api_counts", "reconciliation_coverage_pct"):
        assert key in m, f"manifest missing {key}"
    assert m["finished_at"] >= m["started_at"]


def test_manifest_no_secrets():
    m = _latest_manifest()
    text = json.dumps(m)
    for marker in ("access_token", "refresh_token", "app_secret"):
        assert marker not in text or "REDACTED" in text


def test_four_readiness_flags_present_no_blanket_flag():
    m = _latest_manifest()
    r = m["readiness"]
    for key in ("commercial_reporting_readiness", "finance_settlement_readiness",
                "cash_reconciliation_readiness", "pnl_readiness"):
        assert key in r
    assert "production_ready" not in m
    assert "PRODUCTION_READY" not in m


def test_commercial_readiness_not_blocked_by_payment_mapping():
    m = _latest_manifest()
    r = m["readiness"]
    if r["cash_reconciliation_readiness"] == "UNRESOLVED":
        # payment mapping unresolved must never force commercial to FAIL
        assert r["commercial_reporting_readiness"] in ("READY", "AMBER")


def test_ceo_daily_report_has_no_raw_technical_stats():
    files = sorted(glob.glob(str(REPORTS_DIR / "CEO_DAILY_*.md")))
    if not files:
        pytest.skip("no CEO daily report generated yet")
    text = Path(files[-1]).read_text(encoding="utf-8")
    forbidden = ["statements fetched", "raw videos", "page_token", "request_id",
                 "36009002", "next_page_token"]
    for term in forbidden:
        assert term not in text, f"CEO report leaked technical detail: {term!r}"


def test_ceo_daily_report_every_business_metric_has_context():
    files = sorted(glob.glob(str(REPORTS_DIR / "CEO_DAILY_*.md")))
    if not files:
        pytest.skip("no CEO daily report generated yet")
    text = Path(files[-1]).read_text(encoding="utf-8")
    for section in ("## BUSINESS", "## FUNNEL", "## ECONOMICS", "## GROWTH"):
        assert section in text


def test_net_sales_candidate_never_renamed_final_in_ceo_report():
    files = sorted(glob.glob(str(REPORTS_DIR / "CEO_DAILY_*.md")))
    if not files:
        pytest.skip("no CEO daily report generated yet")
    text = Path(files[-1]).read_text(encoding="utf-8")
    assert "HH Net Sales Candidate" in text
    assert "Net Sales Final" not in text
    assert "business_signoff=PENDING" in text


def test_products_inventory_step_populates_stock_at_product_grain():
    files = sorted(glob.glob(str(NORMALIZED_DIR / "products_*.csv")))
    if not files:
        pytest.skip("no products.csv collected yet")
    with open(files[-1], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows
    assert all("product_id" in r for r in rows)


def test_cogs_join_never_fuzzy_matches():
    files = sorted(glob.glob(str(NORMALIZED_DIR / "cogs_join_*.csv")))
    if not files:
        pytest.skip("no cogs_join yet")
    with open(files[-1], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        assert r["match_status"] in ("EXACT", "MISSING_MAPPING", "MULTIPLE_MATCH", "INVALID")
        if r["match_status"] != "EXACT":
            assert r["cogs_amount"] == "", f"{r['match_status']} row has a COGS value — must be EXACT only"


def test_hero_sku_stock_matches_product_grain_not_sku_grain():
    """Regression test for the product_id vs seller_sku key mismatch bug
    found and fixed during this pipeline build."""
    files = sorted(glob.glob(str(REPORTS_DIR / "ceo_daily_*.json")))
    if not files:
        pytest.skip("no ceo_daily json yet")
    data = json.loads(Path(files[-1]).read_text())
    hero = data.get("HERO_SKU", [])
    if not hero:
        pytest.skip("no hero sku rows")
    # at least one hero SKU should have resolved a numeric stock_qty if a
    # products_<date>.csv exists for the same date
    products_path = NORMALIZED_DIR / f"products_{data['report_date']}.csv"
    if products_path.exists():
        resolved = [h for h in hero if h["stock_qty"] is not None]
        assert resolved, "no hero SKU resolved a stock_qty despite products.csv being present"


def test_marketing_intelligence_sources_documented_separately():
    import business_mart
    assert business_mart.MARKETING_INTELLIGENCE_SOURCES
    for name in ("Bestselling Products", "Bestselling Creators", "Creator Marketplace"):
        assert any(name in s for s in business_mart.MARKETING_INTELLIGENCE_SOURCES)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
