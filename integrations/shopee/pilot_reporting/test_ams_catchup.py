"""
P11-HEAL — unit tests for the Shopee AMS backlog/catch-up fix in
incr_worker.py: _is_ams_date_not_ready, _ams_compute_catchup_days,
_run_ams_catchup, _ams_effective_sync_end.

Root cause fixed: SHOPEE/affiliate_ams was an all-or-nothing pass over
every day in the backlog — one RuntimeError anywhere (proven live
2026-09-23: "AMS API error: error_param - invalid time range,
detail:start_date cannot be later than latest data date" on the newest
requested day after an 8-day-stale watermark) rolled back the WHOLE
transaction, so days that had already succeeded were lost too and
sync_state never advanced. These tests are pure (no DB, no HTTP, no
Keychain) — process_day/log are fakes.

Run with: python3 -m pytest integrations/shopee/pilot_reporting/test_ams_catchup.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from incr_worker import (  # noqa: E402
    _ams_compute_catchup_days,
    _ams_effective_sync_end,
    _is_ams_date_not_ready,
    _run_ams_catchup,
)

UTC = timezone.utc


# =====================================================================
# _is_ams_date_not_ready
# =====================================================================

def test_recognizes_the_exact_live_error_text():
    assert _is_ams_date_not_ready(
        "AMS API error: error_param - invalid time range, "
        "detail:start_date cannot be later than latest data date"
    )


def test_recognizes_documented_today_rejection():
    assert _is_ams_date_not_ready("AMS API error: error_param - The data has not been updated")


def test_case_insensitive():
    assert _is_ams_date_not_ready("INVALID TIME RANGE")


def test_none_and_empty_are_not_matches():
    assert not _is_ams_date_not_ready(None)
    assert not _is_ams_date_not_ready("")


def test_unrelated_error_is_not_treated_as_date_not_ready():
    assert not _is_ams_date_not_ready("AMS API error: error_auth - access_token invalid")
    assert not _is_ams_date_not_ready("429")
    assert not _is_ams_date_not_ready("Connection timed out")


# =====================================================================
# _ams_compute_catchup_days
# =====================================================================

def test_target_date_mode_returns_exactly_that_one_day():
    d = date(2026, 9, 20)
    days = _ams_compute_catchup_days(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 23, tzinfo=UTC),
        datetime(2026, 9, 23, tzinfo=UTC), target_date=d,
    )
    assert days == [d]


def test_stale_watermark_produces_multi_day_catchup():
    window_start = datetime(2026, 9, 15, 2, 24, 48, tzinfo=UTC)  # ~09-15 VN
    window_end = datetime(2026, 9, 23, 4, 0, 0, tzinfo=UTC)
    now = window_end
    days = _ams_compute_catchup_days(window_start, window_end, now)
    # yesterday relative to "now" (VN date), 1-day AMS lag
    assert days[0] == date(2026, 9, 15)
    assert days[-1] == date(2026, 9, 22)
    assert days == sorted(days)
    assert len(set(days)) == len(days)  # no duplicate dates


def test_today_is_never_requested_via_incremental_path():
    now = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)
    days = _ams_compute_catchup_days(now - timedelta(hours=1), now, now)
    today_vn = date(2026, 9, 23)  # UTC+7, 10:00 UTC = 17:00 VN, same calendar day
    assert today_vn not in days


def test_window_end_beyond_yesterday_is_capped_at_yesterday():
    now = datetime(2026, 9, 23, 4, 0, 0, tzinfo=UTC)
    window_end_far_future = datetime(2026, 12, 31, tzinfo=UTC)
    days = _ams_compute_catchup_days(now - timedelta(days=2), window_end_far_future, now)
    assert max(days) <= date(2026, 9, 22)


# =====================================================================
# _run_ams_catchup — pure control flow
# =====================================================================

def _fake_log(msg):
    pass


def test_all_days_succeed_returns_last_day():
    days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    calls = []
    result = _run_ams_catchup(days, lambda d: calls.append(d), _fake_log)
    assert result == date(2026, 9, 22)
    assert calls == days


def test_date_not_ready_on_last_day_keeps_earlier_progress():
    days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    calls = []

    def process_day(d):
        calls.append(d)
        if d == date(2026, 9, 22):
            raise RuntimeError(
                "AMS API error: error_param - invalid time range, "
                "detail:start_date cannot be later than latest data date"
            )

    result = _run_ams_catchup(days, process_day, _fake_log)
    assert result == date(2026, 9, 21)  # last day that actually completed
    assert calls == days  # attempted the failing day, then stopped (no days after it anyway)


def test_date_not_ready_mid_backlog_stops_and_does_not_attempt_later_days():
    days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
    calls = []

    def process_day(d):
        calls.append(d)
        if d == date(2026, 9, 17):
            raise RuntimeError("invalid time range: latest data date exceeded")

    result = _run_ams_catchup(days, process_day, _fake_log)
    assert result == date(2026, 9, 16)
    assert calls == [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]  # 09-18 never attempted


def test_zero_progress_raises_no_artificial_sync_state_advancement():
    days = [date(2026, 9, 20), date(2026, 9, 21)]

    def process_day(d):
        raise RuntimeError("AMS API error: error_param - The data has not been updated")

    with pytest.raises(RuntimeError):
        _run_ams_catchup(days, process_day, _fake_log)


def test_429_within_one_chunk_propagates_as_a_real_failure():
    # Simulates ic.with_backoff already exhausting its retry budget and
    # re-raising — this must NOT be swallowed as "date not ready"; the
    # whole call must still fail exactly as before this fix.
    days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    calls = []

    def process_day(d):
        calls.append(d)
        if d == date(2026, 9, 21):
            raise RuntimeError("429")

    with pytest.raises(RuntimeError, match="429"):
        _run_ams_catchup(days, process_day, _fake_log)
    assert calls == [date(2026, 9, 20), date(2026, 9, 21)]  # never reached 09-22


def test_non_runtime_error_still_propagates():
    days = [date(2026, 9, 20)]

    def process_day(d):
        raise ValueError("not a RuntimeError at all")

    with pytest.raises(ValueError):
        _run_ams_catchup(days, process_day, _fake_log)


# =====================================================================
# _ams_effective_sync_end — sync_state only advances through confirmed
# successful coverage
# =====================================================================

def test_full_completion_returns_none_use_window_end():
    days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    assert _ams_effective_sync_end(days, date(2026, 9, 22)) is None


def test_partial_completion_returns_end_of_last_completed_day():
    days = [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    result = _ams_effective_sync_end(days, date(2026, 9, 20))
    assert result is not None
    # end-of-day 2026-09-20 in Asia/Ho_Chi_Minh (UTC+7), converted to UTC
    assert result == datetime(2026, 9, 20, 16, 59, 59, tzinfo=UTC)


def test_empty_days_returns_none():
    assert _ams_effective_sync_end([], None) is None
