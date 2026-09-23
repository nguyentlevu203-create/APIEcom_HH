"""
P11-SEXTUS Q8 — unit tests for scripts/run_production_cycle.py's
compute_cycle_verdict() and parse_incremental_marker(). Pure functions:
no subprocess, no DB, no network. Reproduces the exact live-regression
scenarios from run 35714248327 (2026-09-22): Shopee AMS 429 and TikTok
orders worker timeout both incrementally FAILING while GOLD/
reconciliation/healthcheck all looked fine — GitHub reported `success`
regardless, because the old verdict never looked at ingestion at all.

Run with: python3 -m pytest scripts/test_production_cycle_verdict.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_production_cycle as rpc  # noqa: E402

ALL_GOLD_SUCCESS = [{"label": f"step{i}", "status": "SUCCESS"} for i in range(7)]
RECON_SUCCESS = {"status": "SUCCESS"}


def _ingestion(status, ingestion_process_status, failed_domains=None):
    return {
        "status": status,
        "ingestion_process_status": ingestion_process_status,
        "ingestion_failed_domains": failed_domains or [],
    }


# =====================================================================
# Q8 Case A — Shopee AMS 429, everything else fine, healthcheck YELLOW
# =====================================================================

def test_case_a_shopee_ams_429_forces_red():
    ingestion = _ingestion("FAILED", "FAIL", ["SHOPEE/affiliate_ams"])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "YELLOW")
    assert verdict == "RED"
    assert "INGESTION_DOMAIN_FAILURE:SHOPEE/affiliate_ams" in reasons


# =====================================================================
# Q8 Case B — TikTok orders DOMAIN_WORKER_TIMEOUT
# =====================================================================

def test_case_b_tiktok_orders_timeout_forces_red():
    ingestion = _ingestion("FAILED", "FAIL", ["TIKTOK/orders"])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "YELLOW")
    assert verdict == "RED"
    assert "INGESTION_DOMAIN_FAILURE:TIKTOK/orders" in reasons


# =====================================================================
# Q8 Case C — all due domains PASS, some NOT_DUE, ads NO_PERMISSION,
# healthcheck YELLOW only for a known non-blocking reason
# =====================================================================

def test_case_c_known_yellow_only_stays_yellow_exit_zero():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "YELLOW")
    assert verdict == "YELLOW"
    assert reasons == []


# =====================================================================
# Q8 Case D — everything green
# =====================================================================

def test_case_d_all_green():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "GREEN")
    assert verdict == "GREEN"
    assert reasons == []


# =====================================================================
# Q6 — a later reconciliation success must not mask an ingestion
# domain failure (both proven live in the same run, 35714248327)
# =====================================================================

def test_q6_reconciliation_success_does_not_mask_ingestion_failure():
    ingestion = _ingestion("FAILED", "FAIL", ["TIKTOK/orders", "SHOPEE/affiliate_ams"])
    # Reconciliation succeeded — exactly what happened live.
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "YELLOW")
    assert verdict == "RED"
    assert "INGESTION_DOMAIN_FAILURE:TIKTOK/orders" in reasons
    assert "INGESTION_DOMAIN_FAILURE:SHOPEE/affiliate_ams" in reasons


# =====================================================================
# Process-level ingestion failures (not domain-shaped)
# =====================================================================

def test_ingestion_process_timeout_forces_red():
    ingestion = _ingestion("TIMEOUT", "TIMEOUT", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "GREEN")
    assert verdict == "RED"
    assert "INGESTION_PROCESS_TIMEOUT" in reasons


def test_ingestion_process_exception_forces_red():
    ingestion = _ingestion("EXCEPTION", "EXCEPTION", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "GREEN")
    assert verdict == "RED"
    assert "INGESTION_PROCESS_EXCEPTION" in reasons


def test_q9_unparseable_result_fails_closed():
    ingestion = _ingestion("FAILED", "INGESTION_RESULT_UNPARSEABLE", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "GREEN")
    assert verdict == "RED"
    assert "INGESTION_RESULT_UNPARSEABLE" in reasons


def test_process_exit_nonzero_with_no_named_domain_still_fails_closed():
    # Defensive case: subprocess exited non-zero but somehow reported no
    # failed domains — must not be silently treated as success.
    ingestion = _ingestion("FAILED", "PROCESS_EXIT_NONZERO", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "GREEN")
    assert verdict == "RED"
    assert "INGESTION_PROCESS_FAILED" in reasons


# =====================================================================
# Gold / reconciliation / healthcheck failures still force RED, unchanged
# =====================================================================

def test_gold_step_failure_forces_red():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    gold = ALL_GOLD_SUCCESS[:-1] + [{"label": "cm2_step", "status": "FAILED"}]
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, gold, RECON_SUCCESS, "GREEN")
    assert verdict == "RED"
    assert "GOLD_STEP_FAILURE:cm2_step" in reasons


def test_reconciliation_failure_forces_red():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, {"status": "TIMEOUT"}, "GREEN")
    assert verdict == "RED"
    assert "RECONCILIATION_TIMEOUT" in reasons


def test_healthcheck_red_forces_red():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "RED")
    assert verdict == "RED"
    assert "HEALTHCHECK_RED" in reasons


def test_unknown_healthcheck_is_conservative_yellow():
    ingestion = _ingestion("SUCCESS", "PASS", [])
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, RECON_SUCCESS, "UNKNOWN")
    assert verdict == "YELLOW"
    assert reasons == []


# =====================================================================
# parse_incremental_marker — Q3/Q4/Q9
# =====================================================================

def test_parse_marker_reads_last_matching_line():
    stdout = (
        "some noisy worker output\n"
        '{"pretty": "json dump printed earlier, not the marker"}\n'
        'HH_INCREMENTAL_RESULT_JSON={"domains": {}, "process_status": "PASS", "failed_domains": []}\n'
    )
    result = rpc.parse_incremental_marker(stdout)
    assert result == {"domains": {}, "process_status": "PASS", "failed_domains": []}


def test_parse_marker_handles_result_over_4kb():
    big_domains = {f"SHOPEE/domain{i}": {"status": "PASS"} for i in range(500)}
    import json as _json
    payload = _json.dumps({"domains": big_domains, "process_status": "PASS", "failed_domains": []})
    stdout = f"lots of prior noise\n{'y' * 5000}\nHH_INCREMENTAL_RESULT_JSON={payload}\n"
    assert len(stdout) > 4096
    result = rpc.parse_incremental_marker(stdout)
    assert result["process_status"] == "PASS"
    assert len(result["domains"]) == 500


def test_parse_marker_missing_returns_none():
    assert rpc.parse_incremental_marker("no marker line here at all\n") is None


def test_parse_marker_malformed_json_returns_none():
    assert rpc.parse_incremental_marker("HH_INCREMENTAL_RESULT_JSON={not valid json\n") is None
