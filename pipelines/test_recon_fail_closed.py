"""
P11-LAST-MILE — pipelines/reconcile.py fails closed and uses a targeted
per-(source_system, domain) worker timeout. No DB, no subprocess, no
network: run_reconcile_domain / the worker subprocess / run-log writes
are all stubbed.

Run with: python3 -m pytest pipelines/test_recon_fail_closed.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile  # noqa: E402

ALL_DOMAINS = (
    [("SHOPEE", d) for d in reconcile.SHOPEE_RECON_DOMAINS]
    + [("TIKTOK", d) for d in reconcile.TIKTOK_RECON_DOMAINS]
)


def _final_summary(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith(reconcile.RECON_RESULT_MARKER):
            return json.loads(line[len(reconcile.RECON_RESULT_MARKER):])
    raise AssertionError("no final reconciliation summary marker")


def _run_main(monkeypatch, fake_domain):
    calls = []

    def fake_run_reconcile_domain(window_label, source_system, domain, shop_id, business_date, timings_sink=None):
        calls.append((window_label, source_system, domain))
        status = fake_domain(window_label, source_system, domain)
        result = {"status": status, "etl_run_id": "x"}
        if status != "PASS":
            result["error"] = "SOURCE_FAILURE"
        reconcile._emit_domain_timing(window_label, business_date, source_system, domain, result, 1.0,
                                      timed_out=False, sink=timings_sink)
        return result

    monkeypatch.setattr(reconcile, "get_shop_ids", lambda: {"SHOPEE": "s1", "TIKTOK": "t1"})
    monkeypatch.setattr(reconcile, "run_reconcile_domain", fake_run_reconcile_domain)
    monkeypatch.setattr(sys, "argv", ["reconcile.py"])
    return reconcile.main(), calls


def test_domain_count_is_11_per_window_33_total():
    assert len(ALL_DOMAINS) == 11


# A — all 33 PASS => exit 0
def test_a_all_pass_exit_zero(monkeypatch, capsys):
    rc, calls = _run_main(monkeypatch, lambda w, s, d: "PASS")
    summary = _final_summary(capsys.readouterr().out)
    assert rc == 0
    assert len(calls) == 33
    assert summary["failed_domain_count"] == 0
    assert summary["failed_domains"] == []
    assert summary["completed_domain_count"] == 33


# B — one domain FAIL, later domains still run, exit non-zero
def test_b_one_domain_fail_isolated_but_process_nonzero(monkeypatch, capsys):
    rc, calls = _run_main(
        monkeypatch, lambda w, s, d: "FAIL" if (w, s, d) == ("D1", "SHOPEE", "finance") else "PASS",
    )
    summary = _final_summary(capsys.readouterr().out)
    assert rc != 0
    assert len(calls) == 33  # every later domain, every later window still ran
    assert summary["failed_domain_count"] == 1
    assert summary["failed_domains"] == ["SHOPEE/finance:D1"]
    assert summary["completed_domain_count"] == 32


# C — TIKTOK/finance exceeds its 1200s => contained FAIL, later domains
# run, final non-zero. Goes through the REAL run_reconcile_domain with
# only the subprocess/run-log primitives stubbed.
def test_targeted_timeout_only_tiktok_finance_raised():
    assert reconcile.recon_domain_timeout("TIKTOK", "finance") == 1200
    for source_system, domain in ALL_DOMAINS:
        if (source_system, domain) != ("TIKTOK", "finance"):
            assert reconcile.recon_domain_timeout(source_system, domain) == 900, (source_system, domain)


def test_c_tiktok_finance_timeout_contained_then_later_domains_run(monkeypatch, capsys):
    seen_timeouts = []
    finished = []

    class _Conn:
        def cursor(self):
            return self

        def commit(self):
            pass

        def close(self):
            pass

    def fake_contained(args, cwd, timeout):
        domain = args[2]
        source = "TIKTOK" if "tiktok_shop" in args[1] else "SHOPEE"
        seen_timeouts.append(((source, domain), timeout))
        if (source, domain) == ("TIKTOK", "finance"):
            return subprocess.CompletedProcess(args, -15, "", ""), True
        return subprocess.CompletedProcess(args, 0, json.dumps({"status": "PASS", "recon_rows": [], "result": {}}), ""), False

    monkeypatch.setattr(reconcile.ic, "get_db_conn", lambda: _Conn())
    monkeypatch.setattr(reconcile.ic, "start_run_log", lambda *a, **k: "run-id")
    monkeypatch.setattr(reconcile.ic, "finish_run_log",
                        lambda ss, d, rid, status, rows=0, err="": finished.append((ss, d, status, err)))
    monkeypatch.setattr(reconcile.ic, "run_contained_subprocess", fake_contained)
    monkeypatch.setattr(reconcile, "get_shop_ids", lambda: {"SHOPEE": "s1", "TIKTOK": "t1"})
    monkeypatch.setattr(sys, "argv", ["reconcile.py"])

    rc = reconcile.main()
    summary = _final_summary(capsys.readouterr().out)

    assert rc != 0
    assert len(seen_timeouts) == 33  # every domain after the timed-out one still executed
    assert all(t == 1200 for (key, t) in seen_timeouts if key == ("TIKTOK", "finance"))
    assert all(t == 900 for (key, t) in seen_timeouts if key != ("TIKTOK", "finance"))
    assert summary["failed_domains"] == ["TIKTOK/finance:D1", "TIKTOK/finance:D3", "TIKTOK/finance:D7"]
    assert summary["failed_domain_count"] == 3
    timed_out_logs = [f for f in finished if f[:2] == ("TIKTOK", "finance")]
    assert all(status == "fail" and "1200s" in err for _, _, status, err in timed_out_logs)
