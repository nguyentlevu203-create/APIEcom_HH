"""
P11-LAST-MILE — scripts/run_production_cycle.py promotes reconciliation
domain failures and TikTok orders partial catch-up into a RED cycle
verdict. Pure functions only: no subprocess, no DB, no network.

Run with: python3 -m pytest scripts/test_recon_verdict_propagation.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_production_cycle as rpc  # noqa: E402

ALL_GOLD_SUCCESS = [{"label": f"step{i}", "status": "SUCCESS"} for i in range(7)]
INGESTION_OK = {"status": "SUCCESS", "ingestion_process_status": "PASS", "ingestion_failed_domains": []}


def _timing(window, source, domain, status="PASS"):
    return f"{rpc.RECON_DOMAIN_TIMING_MARKER}" + json.dumps(
        {"window": window, "source_system": source, "domain": domain, "status": status, "duration_seconds": 1.0})


def _final(failed):
    return f"{rpc.RECON_RESULT_MARKER}" + json.dumps(
        {"failed_domain_count": len(failed), "failed_domains": failed, "completed_domain_count": 33 - len(failed)})


def _stage(stdout, returncode=0, timed_out=False):
    parsed = rpc.parse_recon_timing_markers(stdout)
    status, failed = rpc.compute_recon_stage_status(timed_out, returncode, parsed)
    return {"status": status, "reconciliation_failed_domains": failed}


# A — all PASS, exit 0 => reconciliation SUCCESS
def test_all_pass_is_success():
    recon = _stage("\n".join([_timing("D1", "SHOPEE", "orders"), _final([])]))
    assert recon == {"status": "SUCCESS", "reconciliation_failed_domains": []}
    verdict, reasons = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "GREEN")
    assert verdict == "GREEN" and reasons == []


# B/C — a domain FAIL inside a completed reconcile.py => RED, named
def test_domain_failure_in_completed_run_is_red_with_named_reason():
    stdout = "\n".join([_timing("D1", "TIKTOK", "finance", "FAIL"), _final(["TIKTOK/finance:D1"])])
    recon = _stage(stdout, returncode=1)
    assert recon["status"] == "FAILED"
    verdict, reasons = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "GREEN")
    assert verdict == "RED"
    assert "RECONCILIATION_FAILED" in reasons
    assert "RECONCILIATION_DOMAIN_FAILURE:TIKTOK/finance:D1" in reasons


def test_failed_domain_count_alone_forces_failed_even_with_exit_zero():
    # defense in depth: a summary reporting a failure is never SUCCESS,
    # even if the process somehow exited 0
    recon = _stage(_final(["SHOPEE/orders:D3"]), returncode=0)
    assert recon["status"] == "FAILED"


def test_nonzero_exit_with_clean_summary_is_failed():
    assert _stage(_final([]), returncode=1)["status"] == "FAILED"


def test_completed_without_final_summary_is_unparseable_red():
    recon = _stage(_timing("D1", "SHOPEE", "orders"), returncode=0)
    assert recon["status"] == "RESULT_UNPARSEABLE"
    verdict, reasons = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "GREEN")
    assert verdict == "RED" and "RECONCILIATION_RESULT_UNPARSEABLE" in reasons


def test_summary_missing_failed_domain_count_is_unparseable():
    stdout = f"{rpc.RECON_RESULT_MARKER}" + json.dumps({"total_domains_attempted": 33})
    assert _stage(stdout)["status"] == "RESULT_UNPARSEABLE"


# D — outer 6000s stage timeout => partial timing preserved => RED
def test_d_outer_stage_timeout_keeps_partial_timings_and_is_red():
    stdout = "\n".join([
        _timing("D1", "SHOPEE", "orders"),
        _timing("D1", "TIKTOK", "finance", "FAIL"),
        _timing("D3", "SHOPEE", "orders"),
    ])  # killed before any final summary
    recon = _stage(stdout, returncode=-9, timed_out=True)
    assert recon["status"] == "TIMEOUT"
    assert recon["reconciliation_failed_domains"] == ["TIKTOK/finance:D1"]
    assert len(rpc.parse_recon_timing_markers(stdout)["domain_timings"]) == 3
    verdict, reasons = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "GREEN")
    assert verdict == "RED"
    assert "RECONCILIATION_TIMEOUT" in reasons


# E — healthcheck YELLOW must not erase a reconciliation domain FAIL
def test_e_healthcheck_yellow_plus_recon_domain_fail_is_red():
    recon = {"status": "FAILED", "reconciliation_failed_domains": ["TIKTOK/finance:D1"]}
    verdict, reasons = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "YELLOW")
    assert verdict == "RED"
    assert "RECONCILIATION_DOMAIN_FAILURE:TIKTOK/finance:D1" in reasons


def test_healthcheck_green_plus_recon_domain_fail_is_red():
    recon = {"status": "FAILED", "reconciliation_failed_domains": ["SHOPEE/ads:D7"]}
    verdict, _ = rpc.compute_cycle_verdict(INGESTION_OK, ALL_GOLD_SUCCESS, recon, "GREEN")
    assert verdict == "RED"


# TikTok orders partial catch-up — never GREEN/YELLOW
def test_partial_catchup_ingestion_is_red_with_distinct_reason():
    ingestion = {
        "status": "FAILED", "ingestion_process_status": "PROCESS_EXIT_NONZERO",
        "ingestion_failed_domains": [], "ingestion_partial_catchup_domains": ["TIKTOK/orders"],
    }
    verdict, reasons = rpc.compute_cycle_verdict(ingestion, ALL_GOLD_SUCCESS, {"status": "SUCCESS"}, "YELLOW")
    assert verdict == "RED"
    assert reasons == ["INGESTION_DOMAIN_PARTIAL_CATCHUP:TIKTOK/orders"]
    assert "INGESTION_PROCESS_FAILED" not in reasons


# =====================================================================
# P11-FIX-4
# =====================================================================

def test_timeout_path_does_not_count_source_not_ready_as_failure():
    stdout = "\n".join([
        _timing("D1", "SHOPEE", "affiliate_ams", "SOURCE_NOT_READY"),
        _timing("D1", "TIKTOK", "finance", "FAIL"),
    ])
    recon = _stage(stdout, returncode=-9, timed_out=True)
    assert recon["reconciliation_failed_domains"] == ["TIKTOK/finance:D1"]


def test_completed_recon_with_only_not_ready_is_success():
    stdout = "\n".join([_timing("D1", "SHOPEE", "affiliate_ams", "SOURCE_NOT_READY"), _final([])])
    assert _stage(stdout)["status"] == "SUCCESS"


def test_tiktok_orders_chunk_markers_are_parsed_from_full_stdout():
    filler = "x" * 10_000  # well past the 4KB tail that used to be the only persisted stdout
    chunk = {"chunk_start": "2026-09-17T09:57:12+00:00", "chunk_end": "2026-09-17T13:57:12+00:00",
             "duration_seconds": 30.1, "orders_seen": 40, "orders_inserted": 1, "orders_updated": 39,
             "items_inserted": 1, "items_updated": 70, "committed": True}
    stdout = "\n".join([
        rpc.TIKTOK_ORDERS_CHUNK_MARKER + json.dumps(chunk),
        filler,
        rpc.TIKTOK_ORDERS_CHUNK_MARKER + json.dumps({**chunk, "chunk_end": "2026-09-17T17:57:12+00:00"}),
        rpc.TIKTOK_ORDERS_CHUNK_MARKER + "{not json",
        rpc.TIKTOK_ORDERS_CATCHUP_MARKER + json.dumps({"status": "PARTIAL_CATCHUP", "chunks_completed": 2}),
        filler,
    ])
    parsed = rpc.parse_tiktok_orders_markers(stdout)
    assert [c["chunk_end"] for c in parsed["chunks"]] == ["2026-09-17T13:57:12+00:00", "2026-09-17T17:57:12+00:00"]
    assert parsed["catchup"] == {"status": "PARTIAL_CATCHUP", "chunks_completed": 2}


def test_no_tiktok_orders_markers():
    assert rpc.parse_tiktok_orders_markers("nothing here\n") == {"chunks": [], "catchup": None}


def test_gold_script_timeout_is_targeted():
    assert rpc.gold_script_timeout("_p6a_gold_build.py") == 1200
    for _, script in rpc.GOLD_CHAIN:
        if script != "_p6a_gold_build.py":
            assert rpc.gold_script_timeout(script) == 600
