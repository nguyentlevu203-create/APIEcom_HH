"""
P11-HEAL — unit tests for the TikTok orders backlog-chunking fix in
incr_worker.py: _tiktok_orders_compute_chunks, _run_tiktok_orders_catchup,
plus the cross-file timeout-margin invariant.

Root cause fixed: run_orders() used to fetch+write an entire multi-day
backlog window in one pass, inside one transaction, with no internal
time awareness. pipelines/incremental.py's external
TIKTOK_ORDERS_TIMEOUT_SECONDS=900 then SIGTERM/SIGKILLs the whole
process group mid-run (proven live, repeatedly, most recently
2026-09-23) — losing everything and never advancing sync_state, so the
backlog never shrinks. These tests are pure (no DB, no HTTP, no
network) — process_chunk/now_monotonic are fakes.

Run with: python3 -m pytest integrations/tiktok_shop/pilot_reporting/test_orders_catchup.py -v
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from incr_worker import (  # noqa: E402
    TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS,
    _run_tiktok_orders_catchup,
    _tiktok_orders_compute_chunks,
)

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent.parent.parent
INCREMENTAL_SRC = (ROOT / "pipelines" / "incremental.py").read_text(encoding="utf-8")


def _fake_log(msg):
    pass


# =====================================================================
# _tiktok_orders_compute_chunks
# =====================================================================

def test_normal_small_window_is_a_single_unchanged_chunk():
    start = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)  # normal 30-min-cadence steady state
    chunks = _tiktok_orders_compute_chunks(start, end)
    assert chunks == [(start, end)]


def test_large_backlog_is_split_into_day_sized_chunks():
    start = datetime(2026, 9, 17, 9, 57, 12, tzinfo=UTC)
    end = datetime(2026, 9, 23, 4, 1, 0, tzinfo=UTC)  # ~6 days stale, proven live
    chunks = _tiktok_orders_compute_chunks(start, end)
    assert len(chunks) >= 6
    # contiguous, no gaps, no overlaps
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (s1, e1), (s2, e2) in zip(chunks, chunks[1:]):
        assert e1 == s2
    for chunk_start, chunk_end in chunks:
        assert chunk_end - chunk_start <= timedelta(days=1)


def test_each_chunk_bounded_by_chunk_size():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 10, tzinfo=UTC)
    chunk_size = timedelta(hours=6)
    chunks = _tiktok_orders_compute_chunks(start, end, chunk_size=chunk_size)
    assert all(ce - cs <= chunk_size for cs, ce in chunks)
    assert chunks[0][0] == start
    assert chunks[-1][1] == end


def test_empty_or_inverted_window_yields_no_chunks():
    t = datetime(2026, 9, 23, tzinfo=UTC)
    assert _tiktok_orders_compute_chunks(t, t) == []
    assert _tiktok_orders_compute_chunks(t, t - timedelta(seconds=1)) == []


# =====================================================================
# _run_tiktok_orders_catchup — pure control flow
# =====================================================================

def _clock(sequence):
    it = iter(sequence)
    return lambda: next(it)


def _hour_chunks(n):
    # real datetimes — production code calls .isoformat() on chunk
    # bounds in its log lines, so fakes must support that too.
    base = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    return [(base + timedelta(hours=i), base + timedelta(hours=i + 1)) for i in range(n)]


def test_large_backlog_split_into_chunks_all_succeed():
    chunks = _hour_chunks(5)
    calls = []
    result = _run_tiktok_orders_catchup(
        chunks, lambda s, e: calls.append((s, e)), _fake_log,
        deadline_monotonic=1000.0, now_monotonic=lambda: 0.0,
    )
    assert result == chunks[-1][1]  # end of last chunk
    assert calls == chunks


def test_each_chunk_bounded_deadline_stops_before_starting_next():
    chunks = _hour_chunks(5)
    calls = []
    # now_monotonic() is checked once per loop iteration, before that
    # chunk starts: 0.0 (chunk 0 proceeds), 1.0 (chunk 1 proceeds),
    # 100.0 (deadline exceeded -> stop before chunk 2 ever starts).
    clock = _clock([0.0, 1.0, 100.0])
    result = _run_tiktok_orders_catchup(
        chunks, lambda s, e: calls.append((s, e)), _fake_log,
        deadline_monotonic=50.0, now_monotonic=clock,
    )
    assert calls == chunks[:2]  # stopped before starting chunk index 2
    assert result == chunks[1][1]  # end of last completed chunk


def test_later_chunks_stop_if_earlier_chunk_fails():
    chunks = _hour_chunks(4)
    calls = []

    def process_chunk(s, e):
        calls.append((s, e))
        if (s, e) == chunks[1]:
            raise RuntimeError("order_detail failed: code=50000 message=internal error")

    result = _run_tiktok_orders_catchup(
        chunks, process_chunk, _fake_log, deadline_monotonic=1000.0, now_monotonic=lambda: 0.0,
    )
    assert calls == chunks[:2]  # chunks[2] and chunks[3] never attempted
    assert result == chunks[0][1]  # end of the one chunk that DID complete


def test_sync_state_advances_only_through_completed_chunk_zero_progress_raises():
    chunks = _hour_chunks(2)

    def process_chunk(s, e):
        raise RuntimeError("order_detail failed: code=50000 message=internal error")

    with pytest.raises(RuntimeError):
        _run_tiktok_orders_catchup(
            chunks, process_chunk, _fake_log, deadline_monotonic=1000.0, now_monotonic=lambda: 0.0,
        )


def test_empty_chunks_raises():
    with pytest.raises(RuntimeError):
        _run_tiktok_orders_catchup(
            [], lambda s, e: None, _fake_log, deadline_monotonic=1000.0, now_monotonic=lambda: 0.0,
        )


def test_deadline_already_passed_before_first_chunk_raises():
    chunks = _hour_chunks(1)
    calls = []
    with pytest.raises(RuntimeError):
        _run_tiktok_orders_catchup(
            chunks, lambda s, e: calls.append((s, e)), _fake_log,
            deadline_monotonic=0.0, now_monotonic=lambda: 100.0,
        )
    assert calls == []  # never even attempted


# =====================================================================
# Cross-file invariant — internal soft deadline must leave real margin
# under the external worker-kill timeout (same style as
# scripts/test_run_production_cycle_timeouts.py)
# =====================================================================

def test_internal_deadline_has_real_margin_below_external_worker_timeout():
    m = re.search(r'"TIKTOK",\s*"orders"\)\s*:\s*(\w+)', INCREMENTAL_SRC)
    assert m, "could not find the (\"TIKTOK\",\"orders\") timeout override in pipelines/incremental.py"
    const_name = m.group(1)
    m2 = re.search(rf"^{const_name}\s*=\s*(\d+)", INCREMENTAL_SRC, re.MULTILINE)
    assert m2, f"could not resolve {const_name} to a literal value in pipelines/incremental.py"
    external_timeout = int(m2.group(1))

    assert TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS < external_timeout, (
        f"internal catch-up deadline ({TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS}s) must stay below the "
        f"external worker timeout ({external_timeout}s) it's meant to avoid hitting"
    )
    margin = external_timeout - TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS
    assert margin >= 60, (
        f"only {margin}s of margin between the internal deadline and the external kill — "
        f"too tight to reliably finish an in-flight chunk plus the final DB commit"
    )
