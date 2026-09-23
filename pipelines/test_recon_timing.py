"""
P11-RECON-OBS — unit tests for pipelines/reconcile.py's timing-marker
emission: _emit_domain_timing(). Pure/isolated — no DB, no HTTP, no
subprocess.

Covers Q7.D (flush contract) and Q7.E (no secret-bearing fields).

Run with: python3 -m pytest pipelines/test_recon_timing.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile  # noqa: E402


# =====================================================================
# Q7.D — flush contract
# =====================================================================

def test_emit_domain_timing_flushes_immediately(monkeypatch, capsys):
    calls = []
    real_print = print

    def spy_print(*args, **kwargs):
        calls.append(kwargs.get("flush"))
        real_print(*args, **kwargs)

    monkeypatch.setattr(reconcile, "print", spy_print, raising=False)
    result = {"status": "PASS", "etl_run_id": "abc-123", "recon_rows": [{"a": 1}]}
    reconcile._emit_domain_timing("D1", date(2026, 9, 20), "SHOPEE", "orders", result, 1.234, False)
    assert calls == [True]  # exactly one print call, flush=True


def test_emit_domain_timing_prints_the_exact_marker_prefix(capsys):
    result = {"status": "PASS", "etl_run_id": "abc-123", "recon_rows": []}
    reconcile._emit_domain_timing("D1", date(2026, 9, 20), "SHOPEE", "orders", result, 1.234, False)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith(reconcile.RECON_DOMAIN_TIMING_MARKER)]
    assert len(lines) == 1
    payload = json.loads(lines[0][len(reconcile.RECON_DOMAIN_TIMING_MARKER):])
    assert payload["window"] == "D1"
    assert payload["source_system"] == "SHOPEE"
    assert payload["domain"] == "orders"
    assert payload["status"] == "PASS"


# =====================================================================
# Q7.E — no secret-bearing fields
# =====================================================================

SECRET_BEARING_ERROR = (
    "AMS API error: error_auth - access_token=abcSECRETtoken123 partner_key=SUPERSECRETKEY invalid"
)


def test_payload_never_carries_raw_error_text(capsys):
    result = {"status": "FAIL", "etl_run_id": "xyz-456", "error": SECRET_BEARING_ERROR}
    payload = reconcile._emit_domain_timing("D3", date(2026, 9, 20), "SHOPEE", "affiliate_ams", result, 5.0, False)
    serialized = json.dumps(payload)
    assert "SECRETtoken123" not in serialized
    assert "SUPERSECRETKEY" not in serialized
    assert set(payload.keys()) == {
        "window", "business_date", "source_system", "domain", "status",
        "duration_seconds", "timed_out", "rows_processed", "etl_run_id", "error_class",
    }
    # the raw error still gets a safe classification, never the text itself
    assert payload["error_class"] in (None, "DOMAIN_WORKER_TIMEOUT", "SOURCE_RATE_LIMITED", "SOURCE_FAILURE")


def test_sink_collects_the_same_sanitized_payload():
    sink: list = []
    result = {"status": "FAIL", "etl_run_id": "xyz-789", "error": SECRET_BEARING_ERROR}
    payload = reconcile._emit_domain_timing("D7", date(2026, 9, 13), "TIKTOK", "finance", result, 900.0, True, sink=sink)
    assert sink == [payload]
    assert "SECRETtoken123" not in json.dumps(sink)


def test_rows_processed_zero_for_a_failed_domain():
    result = {"status": "FAIL", "etl_run_id": "e1", "error": "boom"}
    payload = reconcile._emit_domain_timing("D1", date(2026, 9, 20), "TIKTOK", "orders", result, 2.0, False)
    assert payload["rows_processed"] == 0


def test_rows_processed_counts_recon_rows_for_a_passed_domain():
    result = {"status": "PASS", "etl_run_id": "e2", "recon_rows": [{"a": 1}, {"a": 2}, {"a": 3}]}
    payload = reconcile._emit_domain_timing("D1", date(2026, 9, 20), "TIKTOK", "orders", result, 2.0, False)
    assert payload["rows_processed"] == 3
