"""
P11-FIX-4 — Shopee AMS "not published yet" is SOURCE_NOT_READY, not FAIL.

Proven live (run 35948968552, 09:51 ICT): the only due day was D-1 and
AMS had not published it yet; P11-HEAL's zero-progress raise turned
that into a hard FAIL for both incremental and D-1 reconciliation. No
DB, no HTTP, no Keychain: the handler, connection and secrets are fakes.

Run with: python3 -m pytest integrations/shopee/pilot_reporting/test_ams_not_ready.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_worker  # noqa: E402
from incr_worker import AmsSourceNotReady, _run_ams_catchup  # noqa: E402

NOT_READY = ("AMS API error: error_param - invalid time range, detail:start_date cannot be "
             "later than latest data date")


def _log(msg):
    pass


def test_only_day_not_ready_raises_source_not_ready():
    def process_day(d):
        raise RuntimeError(NOT_READY)

    with pytest.raises(AmsSourceNotReady):
        _run_ams_catchup([date(2026, 9, 23)], process_day, _log)


def test_real_error_on_first_day_is_still_a_plain_failure():
    def process_day(d):
        raise RuntimeError("AMS API error: error_auth - invalid access_token")

    with pytest.raises(RuntimeError) as exc:
        _run_ams_catchup([date(2026, 9, 23)], process_day, _log)
    assert not isinstance(exc.value, AmsSourceNotReady)


def test_invalid_time_range_without_publish_lag_wording_stays_a_failure():
    # a malformed request can also say "invalid time range" — without the
    # publish-lag wording it must never be reported as SOURCE_NOT_READY
    def process_day(d):
        raise RuntimeError("AMS API error: error_param - invalid time range, detail:end_date before start_date")

    with pytest.raises(RuntimeError) as exc:
        _run_ams_catchup([date(2026, 9, 23)], process_day, _log)
    assert not isinstance(exc.value, AmsSourceNotReady)


@pytest.mark.parametrize("error", [
    "HTTP 429 Too Many Requests",                       # 429 after retries
    "AMS API error: error_auth - invalid access_token",  # auth
    "network/timeout error: ReadTimeout",                # transport
    "Expecting value: line 1 column 1 (char 0)",         # malformed response
])
def test_non_publish_lag_errors_are_never_source_not_ready(error):
    def process_day(d):
        raise RuntimeError(error)

    with pytest.raises(RuntimeError) as exc:
        _run_ams_catchup([date(2026, 9, 23)], process_day, _log)
    assert not isinstance(exc.value, AmsSourceNotReady)


def test_documented_today_rejection_is_source_not_ready():
    def process_day(d):
        raise RuntimeError("AMS API error: error_param - data has not been updated")

    with pytest.raises(AmsSourceNotReady):
        _run_ams_catchup([date(2026, 9, 23)], process_day, _log)


def test_progress_then_not_ready_is_unchanged_partial_success():
    days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]

    def process_day(d):
        if d == days[2]:
            raise RuntimeError(NOT_READY)

    assert _run_ams_catchup(days, process_day, _log) == days[1]


class _Conn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


@pytest.mark.parametrize("mode_args", [[], ["reconcile", "2026-09-23"]])
def test_worker_main_reports_source_not_ready_exit_zero_no_sync_state(monkeypatch, capsys, mode_args):
    conn = _Conn()
    sync_calls = []

    def not_ready_handler(*a, **k):
        raise AmsSourceNotReady("AMS reports not available for any requested day (2026-09-23..2026-09-23)")

    monkeypatch.setattr(incr_worker, "get_secret", lambda key: "shop-1")
    monkeypatch.setattr(incr_worker.ic, "get_db_conn", lambda: conn)
    monkeypatch.setattr(incr_worker.ic, "upsert_sync_state", lambda *a, **k: sync_calls.append(a))
    monkeypatch.setitem(incr_worker.HANDLERS, "affiliate_ams", not_ready_handler)
    monkeypatch.setattr(incr_worker, "reconcile_domain", lambda *a, **k: not_ready_handler())
    monkeypatch.setattr(sys, "argv", ["incr_worker.py", "affiliate_ams", "2026-09-23T07:33:51+00:00",
                                      "2026-09-24T02:51:04+00:00", "run-1", *mode_args])

    rc = incr_worker.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert rc == 0
    assert out["status"] == "SOURCE_NOT_READY"
    assert "error" not in out
    assert sync_calls == []  # watermark untouched
    assert conn.rolled_back and not conn.committed
