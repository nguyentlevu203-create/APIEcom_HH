"""
P11-SEXTUS — unit tests for the domain-status/verdict semantics in
pipelines/incr_common.py (is_domain_ok, classify_domain_error,
compute_ingestion_verdict). Pure functions, no DB, no subprocess.

Run with: python3 -m pytest pipelines/test_ingestion_verdict.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_common as ic  # noqa: E402


# =====================================================================
# Q1 — status classes
# =====================================================================

def test_only_statuses_actual_code_produces_are_ok():
    assert ic.is_domain_ok("PASS")
    assert ic.is_domain_ok("NOT_DUE")
    assert ic.is_domain_ok("NO_PERMISSION / SEPARATE_ADS_API")


def test_no_fabricated_no_data_due_status():
    # NO_DATA_DUE does not exist anywhere in current code/runtime —
    # must NOT be treated as an OK status.
    assert not ic.is_domain_ok("NO_DATA_DUE")


def test_fail_and_unknown_statuses_are_not_ok():
    assert not ic.is_domain_ok("FAIL")
    assert not ic.is_domain_ok(None)
    assert not ic.is_domain_ok("SOMETHING_NEW_NOBODY_WROTE_YET")  # fail-closed


def test_rows_processed_zero_is_not_itself_a_failure_signal():
    # is_domain_ok only looks at "status" — a PASS domain result that
    # happened to process 0 rows must never be misread as a failure.
    result = {"status": "PASS", "result": {"orders": {"received": 0}}}
    assert ic.is_domain_ok(result["status"])


# =====================================================================
# classify_domain_error
# =====================================================================

def test_classify_ok_domain_returns_none():
    assert ic.classify_domain_error({"status": "PASS"}) is None
    assert ic.classify_domain_error({"status": "NOT_DUE"}) is None


def test_classify_timeout():
    err = "DOMAIN_WORKER_TIMEOUT: worker exceeded 900s, process group terminated"
    assert ic.classify_domain_error({"status": "FAIL", "error": err}) == "DOMAIN_WORKER_TIMEOUT"


def test_classify_rate_limited():
    assert ic.classify_domain_error({"status": "FAIL", "error": "429 Too Many Requests"}) == "SOURCE_RATE_LIMITED"


def test_classify_generic_failure():
    assert ic.classify_domain_error({"status": "FAIL", "error": "boom"}) == "SOURCE_FAILURE"


# =====================================================================
# Q8 — compute_ingestion_verdict, exact live-regression scenarios
# =====================================================================

def test_case_a_shopee_ams_429():
    results = {
        "SHOPEE/orders": {"status": "PASS"},
        "SHOPEE/affiliate_ams": {"status": "FAIL", "error": "429 Too Many Requests"},
        "TIKTOK/orders": {"status": "PASS"},
    }
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "FAIL"
    assert verdict["failed_domains"] == ["SHOPEE/affiliate_ams"]


def test_case_b_tiktok_orders_worker_timeout():
    results = {
        "SHOPEE/orders": {"status": "PASS"},
        "TIKTOK/orders": {"status": "FAIL", "error": "DOMAIN_WORKER_TIMEOUT: worker exceeded 900s"},
    }
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "FAIL"
    assert verdict["failed_domains"] == ["TIKTOK/orders"]


def test_case_c_not_due_and_ads_skip_are_not_failures():
    results = {
        "SHOPEE/orders": {"status": "PASS"},
        "SHOPEE/returns": {"status": "NOT_DUE", "cadence_minutes": 30},
        "TIKTOK/ads": {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0},
    }
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "PASS"
    assert verdict["failed_domains"] == []


def test_case_d_all_green():
    results = {f"SHOPEE/{d}": {"status": "PASS"} for d in ("orders", "returns", "finance")}
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "PASS"
    assert verdict["failed_domains"] == []


def test_case_e_domain_isolation_multiple_failures_all_reported():
    # Simulates: domain 2 fails, domains 3..N still ran (they're present
    # in the results dict with their own real outcomes) — proves
    # isolation didn't stop the loop, and every failure surfaces, not
    # just the first one found.
    results = {
        "SHOPEE/orders": {"status": "PASS"},
        "SHOPEE/affiliate_ams": {"status": "FAIL", "error": "429"},
        "SHOPEE/finance": {"status": "PASS"},
        "TIKTOK/orders": {"status": "FAIL", "error": "DOMAIN_WORKER_TIMEOUT: worker exceeded 900s"},
        "TIKTOK/returns": {"status": "PASS"},
    }
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "FAIL"
    assert verdict["failed_domains"] == ["SHOPEE/affiliate_ams", "TIKTOK/orders"]


def test_no_shop_id_domain_counts_as_failure():
    results = {"SHOPEE/orders": {"status": "FAIL", "error": "no shop_id for SHOPEE"}}
    verdict = ic.compute_ingestion_verdict(results)
    assert verdict["process_status"] == "FAIL"
    assert verdict["failed_domains"] == ["SHOPEE/orders"]
