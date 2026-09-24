"""
P11-LAST-MILE — PARTIAL_CATCHUP semantics in pipelines/incr_common.py
and pipelines/incremental.py's run_domain(). No DB, no subprocess, no
network: every DB/subprocess primitive is stubbed.

Run with: python3 -m pytest pipelines/test_partial_catchup.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_common as ic  # noqa: E402
import incremental  # noqa: E402


def test_partial_catchup_is_not_ok_and_not_a_hard_failure():
    assert not ic.is_domain_ok(ic.PARTIAL_CATCHUP_STATUS)
    assert ic.classify_domain_error({"status": ic.PARTIAL_CATCHUP_STATUS}) == "PARTIAL_CATCHUP"


def test_verdict_reports_partial_separately_and_process_fails():
    verdict = ic.compute_ingestion_verdict({
        "TIKTOK/orders": {"status": "PARTIAL_CATCHUP"},
        "SHOPEE/orders": {"status": "PASS"},
    })
    assert verdict == {
        "process_status": "FAIL", "failed_domains": [], "partial_catchup_domains": ["TIKTOK/orders"],
    }


def test_verdict_all_ok_has_empty_partial_list():
    verdict = ic.compute_ingestion_verdict({"TIKTOK/orders": {"status": "PASS"}})
    assert verdict == {"process_status": "PASS", "failed_domains": [], "partial_catchup_domains": []}


class _Conn:
    def cursor(self):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def _run_domain(monkeypatch, stdout, returncode=0, timed_out=False):
    finished = []
    monkeypatch.setattr(incremental.ic, "get_db_conn", lambda: _Conn())
    monkeypatch.setattr(incremental.ic, "get_sync_state", lambda *a: {
        "last_synced_at": datetime(2026, 9, 17, 9, 57, 12, tzinfo=timezone.utc), "last_business_date": None,
        "status": "success"})
    monkeypatch.setattr(incremental.ic, "start_run_log", lambda *a, **k: "run-id")
    monkeypatch.setattr(incremental.ic, "finish_run_log",
                        lambda ss, d, rid, status, rows=0, err="": finished.append((status, rows, err)))
    monkeypatch.setattr(incremental.ic, "run_contained_subprocess",
                        lambda args, cwd, timeout: (subprocess.CompletedProcess(args, returncode, stdout, ""), timed_out))
    return incremental.run_domain("TIKTOK", "orders", "shop-1", force=True), finished


CATCHUP = {"chunks_total": 34, "chunks_completed": 12, "last_committed_end": "2026-09-19T09:57:12+00:00",
           "catchup_complete": False, "stopped_by_soft_deadline": True}
RESULT = {"order_ids_count": 5, "order": {"received": 5, "inserted": 2, "updated": 3, "unchanged": 0}}


def test_run_domain_partial_catchup_status_and_run_log(monkeypatch):
    stdout = json.dumps({"status": "PARTIAL_CATCHUP", "result": RESULT, "catchup": CATCHUP})
    res, finished = _run_domain(monkeypatch, stdout)
    assert res["status"] == "PARTIAL_CATCHUP"
    assert res["catchup"]["last_committed_end"] == CATCHUP["last_committed_end"]
    assert finished == [("success", 5, "PARTIAL_CATCHUP: committed 12/34 chunk(s) through "
                                      "2026-09-19T09:57:12+00:00, backlog remains")]


def test_run_domain_partial_status_with_nonzero_exit_is_fail(monkeypatch):
    stdout = json.dumps({"status": "PARTIAL_CATCHUP", "result": RESULT, "catchup": CATCHUP})
    res, finished = _run_domain(monkeypatch, stdout, returncode=1)
    assert res["status"] == "FAIL"
    assert finished[0][0] == "fail"


def test_run_domain_timeout_reports_durably_committed_chunks(monkeypatch):
    marker = "HH_TIKTOK_ORDERS_CHUNK_JSON="
    stdout = "\n".join([
        marker + json.dumps({"chunk_end": "2026-09-17T13:57:12+00:00", "committed": True}),
        marker + json.dumps({"chunk_end": "2026-09-17T17:57:12+00:00", "committed": True}),
    ])
    res, finished = _run_domain(monkeypatch, stdout, returncode=-9, timed_out=True)
    assert res["status"] == "FAIL"
    assert "DOMAIN_WORKER_TIMEOUT" in res["error"]
    assert res["committed_chunks"] == 2
    assert res["last_committed_end"] == "2026-09-17T17:57:12+00:00"


def test_run_domain_timeout_without_markers_unchanged(monkeypatch):
    res, _ = _run_domain(monkeypatch, "", returncode=-9, timed_out=True)
    assert "committed_chunks" not in res and "last_committed_end" not in res


# =====================================================================
# P11-FIX-4 — SOURCE_NOT_READY (e.g. Shopee AMS D-1 not published yet)
# =====================================================================

def test_source_not_ready_is_ok_for_verdict():
    assert ic.is_domain_ok(ic.SOURCE_NOT_READY_STATUS)
    verdict = ic.compute_ingestion_verdict({"SHOPEE/affiliate_ams": {"status": "SOURCE_NOT_READY"}})
    assert verdict["process_status"] == "PASS"
    assert verdict["failed_domains"] == [] and verdict["partial_catchup_domains"] == []


def test_run_domain_source_not_ready(monkeypatch):
    stdout = json.dumps({"status": "SOURCE_NOT_READY",
                         "note": "AMS reports not available for any requested day (2026-09-23..2026-09-23)"})
    res, finished = _run_domain(monkeypatch, stdout)
    assert res["status"] == "SOURCE_NOT_READY"
    assert finished == [("success", 0, "SOURCE_NOT_READY: AMS reports not available for any requested day "
                                       "(2026-09-23..2026-09-23)")]


def test_run_domain_source_not_ready_with_nonzero_exit_is_fail(monkeypatch):
    res, finished = _run_domain(monkeypatch, json.dumps({"status": "SOURCE_NOT_READY"}), returncode=1)
    assert res["status"] == "FAIL"
    assert finished[0][0] == "fail"
