"""
P11-SEXTUS — integration-level tests for pipelines/incremental.py's
main(): the HH_INCREMENTAL_RESULT_JSON= marker line and the process
exit code it now derives from compute_ingestion_verdict(). DB/API calls
are monkeypatched out entirely (get_shop_ids, run_domain,
run_tiktok_ads_skip) — this only exercises the aggregation/exit-code
wiring, not the ETL work itself.

Run with: python3 -m pytest pipelines/test_incremental_exit_code.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incremental as inc  # noqa: E402

MARKER = "HH_INCREMENTAL_RESULT_JSON="


def _marker_payload(captured_stdout: str) -> dict:
    lines = [l for l in captured_stdout.splitlines() if l.startswith(MARKER)]
    assert len(lines) == 1, f"expected exactly one marker line, got {len(lines)}"
    return json.loads(lines[0][len(MARKER):])


def test_main_returns_nonzero_when_a_due_domain_fails(monkeypatch, capsys):
    monkeypatch.setattr(inc, "get_shop_ids", lambda: {"SHOPEE": "1", "TIKTOK": "2"})

    def fake_run_domain(source_system, domain, shop_id, force):
        if (source_system, domain) == ("SHOPEE", "affiliate_ams"):
            return {"status": "FAIL", "error": "429 Too Many Requests"}
        return {"status": "PASS"}

    monkeypatch.setattr(inc, "run_domain", fake_run_domain)
    monkeypatch.setattr(inc, "run_tiktok_ads_skip", lambda: {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0})
    monkeypatch.setattr(sys, "argv", ["incremental.py"])

    rc = inc.main()
    assert rc == 1

    payload = _marker_payload(capsys.readouterr().out)
    assert payload["process_status"] == "FAIL"
    assert payload["failed_domains"] == ["SHOPEE/affiliate_ams"]
    assert payload["domains"]["SHOPEE/affiliate_ams"]["status"] == "FAIL"


def test_main_returns_zero_when_every_domain_ok(monkeypatch, capsys):
    monkeypatch.setattr(inc, "get_shop_ids", lambda: {"SHOPEE": "1", "TIKTOK": "2"})
    monkeypatch.setattr(inc, "run_domain", lambda *a, **k: {"status": "PASS"})
    monkeypatch.setattr(inc, "run_tiktok_ads_skip", lambda: {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0})
    monkeypatch.setattr(sys, "argv", ["incremental.py"])

    rc = inc.main()
    assert rc == 0

    payload = _marker_payload(capsys.readouterr().out)
    assert payload["process_status"] == "PASS"
    assert payload["failed_domains"] == []


def test_not_due_domains_do_not_force_failure(monkeypatch, capsys):
    monkeypatch.setattr(inc, "get_shop_ids", lambda: {"SHOPEE": "1", "TIKTOK": "2"})
    monkeypatch.setattr(inc, "run_domain", lambda *a, **k: {"status": "NOT_DUE", "cadence_minutes": 30})
    monkeypatch.setattr(inc, "run_tiktok_ads_skip", lambda: {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0})
    monkeypatch.setattr(sys, "argv", ["incremental.py"])

    rc = inc.main()
    assert rc == 0


def test_domain_isolation_all_domains_still_run_after_one_fails(monkeypatch, capsys):
    monkeypatch.setattr(inc, "get_shop_ids", lambda: {"SHOPEE": "1", "TIKTOK": "2"})
    calls = []

    def fake_run_domain(source_system, domain, shop_id, force):
        calls.append((source_system, domain))
        if (source_system, domain) == ("TIKTOK", "orders"):
            return {"status": "FAIL", "error": "DOMAIN_WORKER_TIMEOUT: worker exceeded 900s"}
        return {"status": "PASS"}

    monkeypatch.setattr(inc, "run_domain", fake_run_domain)
    monkeypatch.setattr(inc, "run_tiktok_ads_skip", lambda: {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0})
    monkeypatch.setattr(sys, "argv", ["incremental.py"])

    rc = inc.main()
    assert rc == 1
    all_domains = (
        [("SHOPEE", d) for d in inc.SHOPEE_ONLY_DOMAINS] + [("TIKTOK", d) for d in inc.TIKTOK_ONLY_DOMAINS]
    )
    # every domain still got its turn, not just the ones before the failure
    assert set(calls) == set(all_domains)


def test_marker_line_survives_a_large_multiline_error(monkeypatch, capsys):
    # Q4/Q9 — a result well past 4KB, including an embedded newline in
    # one domain's error text, must still land as exactly one parseable
    # marker line (json.dumps escapes embedded newlines as \n).
    big_error = ("line one of a traceback\n" + "x" * 4990)

    monkeypatch.setattr(inc, "get_shop_ids", lambda: {"SHOPEE": "1", "TIKTOK": "2"})

    def fake_run_domain(source_system, domain, shop_id, force):
        if (source_system, domain) == ("TIKTOK", "orders"):
            return {"status": "FAIL", "error": big_error}
        return {"status": "PASS"}

    monkeypatch.setattr(inc, "run_domain", fake_run_domain)
    monkeypatch.setattr(inc, "run_tiktok_ads_skip", lambda: {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0})
    monkeypatch.setattr(sys, "argv", ["incremental.py"])

    rc = inc.main()
    assert rc == 1

    out = capsys.readouterr().out
    assert len(out) > 4096  # proves this scenario actually exceeds the old bounded-tail size
    payload = _marker_payload(out)
    assert payload["failed_domains"] == ["TIKTOK/orders"]
    assert payload["domains"]["TIKTOK/orders"]["error"] == big_error
