"""
P11-RECON-OBS — unit tests for scripts/run_production_cycle.py's
parse_recon_timing_markers(): reading pipelines/reconcile.py's flushed
timing markers off FULL subprocess stdout, including when the process
was killed mid-run (no final summary marker) or stdout exceeds 4KB.
Pure function — no subprocess, no DB.

Run with: python3 -m pytest scripts/test_recon_observability.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_production_cycle as rpc  # noqa: E402


def _domain_marker(window, domain, duration=1.0, status="PASS"):
    payload = {
        "window": window, "business_date": "2026-09-20", "source_system": "SHOPEE", "domain": domain,
        "status": status, "duration_seconds": duration, "timed_out": False,
        "rows_processed": 3, "etl_run_id": f"run-{domain}", "error_class": None,
    }
    return f"{rpc.RECON_DOMAIN_TIMING_MARKER}{json.dumps(payload)}"


def _window_start(window):
    return f"{rpc.RECON_WINDOW_START_MARKER}{json.dumps({'window': window, 'business_date': '2026-09-20'})}"


def _window_end(window, completed=5, failed=0):
    payload = {
        "window": window, "business_date": "2026-09-20", "elapsed_seconds": 12.5,
        "completed_domain_count": completed, "failed_domain_count": failed,
    }
    return f"{rpc.RECON_WINDOW_END_MARKER}{json.dumps(payload)}"


# =====================================================================
# Case A — normal completion
# =====================================================================

def test_case_a_normal_completion_all_markers_parse():
    lines = [
        _window_start("D1"),
        _domain_marker("D1", "orders"),
        _domain_marker("D1", "finance"),
        _window_end("D1", completed=2, failed=0),
        _window_start("D3"),
        _domain_marker("D3", "orders"),
        _window_end("D3", completed=1, failed=0),
        _window_start("D7"),
        _domain_marker("D7", "orders"),
        _window_end("D7", completed=1, failed=0),
        f"{rpc.RECON_RESULT_MARKER}{json.dumps({'total_domains_attempted': 4, 'total_domains_success': 4})}",
    ]
    stdout = "\n".join(lines)
    parsed = rpc.parse_recon_timing_markers(stdout)
    assert len(parsed["domain_timings"]) == 4
    assert parsed["completed_windows"] == ["D1", "D3", "D7"]
    assert parsed["last_window"] == "D7"
    assert parsed["final_summary"] is not None
    assert parsed["final_summary"]["total_domains_attempted"] == 4


# =====================================================================
# Case B — stage killed after several domains, no final marker
# =====================================================================

def test_case_b_killed_mid_run_preserves_partial_timings():
    lines = [_window_start("D1")]
    for i in range(17):
        lines.append(_domain_marker("D1", f"domain{i}"))
    # no window end, no final summary — simulates the outer 3600s kill
    stdout = "\n".join(lines)
    parsed = rpc.parse_recon_timing_markers(stdout)
    assert len(parsed["domain_timings"]) == 17
    assert parsed["completed_windows"] == []  # window never finished
    assert parsed["last_window"] == "D1"  # still know where it was working
    assert parsed["final_summary"] is None


def test_completed_domain_count_derived_from_timings_when_no_summary():
    lines = [_window_start("D1")] + [_domain_marker("D1", f"d{i}") for i in range(17)]
    parsed = rpc.parse_recon_timing_markers("\n".join(lines))
    # this is exactly what run_reconciliation() does: len(domain_timings)
    assert len(parsed["domain_timings"]) == 17


# =====================================================================
# Case C — stdout exceeds 4KB; must not depend on a bounded tail
# =====================================================================

def test_case_c_marker_near_start_of_large_stdout_still_parses():
    noise = "x" * 6000  # well past the old 4000-char stdout_tail bound
    early_marker = _domain_marker("D1", "orders")
    lines = [early_marker, noise, _window_end("D1", completed=1, failed=0)]
    stdout = "\n".join(lines)
    assert len(stdout) > 4096
    # the old approach (stdout[-4000:]) would have dropped early_marker entirely
    assert early_marker not in stdout[-4000:]
    parsed = rpc.parse_recon_timing_markers(stdout)
    assert len(parsed["domain_timings"]) == 1
    assert parsed["domain_timings"][0]["domain"] == "orders"
    assert parsed["completed_windows"] == ["D1"]


def test_case_c_final_summary_after_large_noise_still_parses():
    noise = "y" * 8000
    stdout = "\n".join([_domain_marker("D1", "orders"), noise,
                         f"{rpc.RECON_RESULT_MARKER}{json.dumps({'total_domains_attempted': 1})}"])
    parsed = rpc.parse_recon_timing_markers(stdout)
    assert parsed["final_summary"]["total_domains_attempted"] == 1


# =====================================================================
# No markers at all / malformed lines
# =====================================================================

def test_no_markers_returns_empty_structure():
    parsed = rpc.parse_recon_timing_markers("just some ordinary log output\nnothing special here\n")
    assert parsed == {
        "domain_timings": [], "completed_windows": [], "last_window": None, "final_summary": None,
    }


def test_malformed_marker_line_is_skipped_not_fatal():
    stdout = "\n".join([
        f"{rpc.RECON_DOMAIN_TIMING_MARKER}{{not valid json",
        _domain_marker("D1", "orders"),
    ])
    parsed = rpc.parse_recon_timing_markers(stdout)
    assert len(parsed["domain_timings"]) == 1  # the malformed line is skipped, not fatal
