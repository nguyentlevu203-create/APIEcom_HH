"""
P11-HEAL / P11-LAST-MILE — unit tests for the TikTok orders backlog
catch-up in incr_worker.py: _tiktok_orders_compute_chunks,
_run_tiktok_orders_durable_catchup, run_orders_incremental's wiring,
plus the cross-file timeout-margin invariant.

Root cause fixed (proven live, runs 35816592219 and 35832785885): both
runs started TikTok/orders at the same watermark and both hit the 900s
external worker kill — P11-HEAL's 1-day chunks all shared ONE
transaction committed at the very end, so the kill rolled every
finished chunk back and sync_state never moved. Now each chunk commits
its rows + watermark on its own connection. These tests are pure (no
DB, no HTTP, no network) — connections, fetch and clock are fakes.

Run with: python3 -m pytest integrations/tiktok_shop/pilot_reporting/test_orders_catchup.py -v
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_worker  # noqa: E402
from incr_worker import (  # noqa: E402
    TIKTOK_ORDERS_CATCHUP_MARKER,
    TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS,
    TIKTOK_ORDERS_CHUNK_MARKER,
    TIKTOK_ORDERS_CHUNK_SIZE,
    _run_tiktok_orders_durable_catchup,
    _tiktok_orders_compute_chunks,
)

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent.parent.parent
INCREMENTAL_SRC = (ROOT / "pipelines" / "incremental.py").read_text(encoding="utf-8")
WORKER_SRC = (Path(__file__).resolve().parent / "incr_worker.py").read_text(encoding="utf-8")


def _fake_log(msg):
    pass


# =====================================================================
# Fakes — a tiny transactional "database": rows/watermark written on a
# connection only become visible in `committed_*` on that connection's
# commit(); rollback() or close() without commit discards them (exactly
# what Postgres does when a connection dies mid-transaction).
# =====================================================================

class FakeDB:
    def __init__(self):
        self.committed_chunks: list = []
        self.watermark = None
        self.events: list[str] = []
        self.connections: list = []

    def connect(self):
        conn = FakeConn(self)
        self.connections.append(conn)
        return conn


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.pending_chunks: list = []
        self.pending_watermark = None
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self

    def commit(self):
        self.db.committed_chunks.extend(self.pending_chunks)
        if self.pending_watermark is not None:
            self.db.watermark = self.pending_watermark
        self.db.events.append("commit")
        self.pending_chunks, self.pending_watermark = [], None
        self.committed = True

    def rollback(self):
        self.pending_chunks, self.pending_watermark = [], None
        self.rolled_back = True
        self.db.events.append("rollback")

    def close(self):
        self.pending_chunks, self.pending_watermark = [], None  # uncommitted work is lost
        self.closed = True


class SimulatedExternalKill(BaseException):
    """Stands in for the external SIGTERM/SIGKILL: not an Exception, so
    the coordinator cannot catch/handle it — it just unwinds, the way a
    killed process simply stops."""


def _chunks(n, hours=4):
    base = datetime(2026, 9, 17, 9, 57, 12, tzinfo=UTC)
    return [(base + timedelta(hours=hours * i), base + timedelta(hours=hours * (i + 1))) for i in range(n)]


def _run(chunks, db, *, fail_at=None, kill_at=None, clock=None, deadline=1000.0, emitted=None):
    def fetch_chunk(s, e):
        db.events.append(f"fetch:{s.isoformat()}")
        return {"chunk": (s, e)}

    def write_chunk(cur, fetched, s, e):
        idx = chunks.index((s, e))
        if fail_at is not None and idx == fail_at:
            cur.pending_chunks.append((s, e))  # partially written before failing
            raise RuntimeError("order_detail failed: code=50000 message=internal error")
        if kill_at is not None and idx == kill_at:
            cur.pending_chunks.append((s, e))
            raise SimulatedExternalKill()
        cur.pending_chunks.append((s, e))
        return {"orders_seen": 2, "orders_inserted": 1, "orders_updated": 1,
                "items_inserted": 3, "items_updated": 0}

    def advance_watermark(cur, chunk_end):
        cur.pending_watermark = chunk_end

    def emit(prefix, payload):
        if emitted is not None:
            emitted.append((prefix, payload))

    return _run_tiktok_orders_durable_catchup(
        chunks, fetch_chunk, write_chunk, advance_watermark, db.connect, _fake_log,
        deadline_monotonic=deadline, now_monotonic=clock or (lambda: 0.0), emit=emit,
    )


def _clock(sequence):
    it = iter(sequence)
    return lambda: next(it)


# =====================================================================
# _tiktok_orders_compute_chunks — 4h chunks, exact timestamps
# =====================================================================

def test_chunk_size_is_sub_day_4_hours():
    assert TIKTOK_ORDERS_CHUNK_SIZE == timedelta(hours=4)


def test_normal_small_window_is_a_single_unchanged_chunk():
    start = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)  # normal 30-min-cadence steady state
    assert _tiktok_orders_compute_chunks(start, end) == [(start, end)]


def test_large_backlog_split_into_contiguous_4h_chunks_exact_timestamps():
    start = datetime(2026, 9, 17, 9, 57, 12, 448506, tzinfo=UTC)  # proven-live watermark
    end = datetime(2026, 9, 23, 7, 38, 37, tzinfo=UTC)
    chunks = _tiktok_orders_compute_chunks(start, end)
    assert chunks[0][0] == start  # not rounded to a date/hour
    assert chunks[-1][1] == end
    for (s1, e1), (s2, e2) in zip(chunks, chunks[1:]):
        assert e1 == s2  # no gaps, no overlaps
    assert all(ce - cs <= timedelta(hours=4) for cs, ce in chunks)
    assert all(ce - cs == timedelta(hours=4) for cs, ce in chunks[:-1])
    assert chunks[-1][1] - chunks[-1][0] <= timedelta(hours=4)  # last may be shorter


def test_empty_or_inverted_window_yields_no_chunks():
    t = datetime(2026, 9, 23, tzinfo=UTC)
    assert _tiktok_orders_compute_chunks(t, t) == []
    assert _tiktok_orders_compute_chunks(t, t - timedelta(seconds=1)) == []


# =====================================================================
# Case 1 — first 2 chunks commit, third fails
# =====================================================================

def test_case1_two_chunks_commit_third_fails_prior_chunks_survive():
    chunks = _chunks(5)
    db = FakeDB()
    meta = _run(chunks, db, fail_at=2)

    assert db.committed_chunks == chunks[:2]  # chunk1, chunk2 durable; chunk3's partial write gone
    assert db.watermark == chunks[1][1]  # sync_state = chunk2.end
    assert db.connections[2].rolled_back and not db.connections[2].committed
    assert meta["status"] == "FAIL"
    assert meta["chunks_completed"] == 2
    assert meta["chunks_attempted"] == 3  # chunks 4/5 never started
    assert meta["last_committed_end"] == chunks[1][1].isoformat()
    assert meta["catchup_complete"] is False


# =====================================================================
# Case 2 — soft deadline after 3 chunks
# =====================================================================

def test_case2_soft_deadline_after_three_chunks_is_partial_catchup():
    chunks = _chunks(6)
    db = FakeDB()
    # checked once before each chunk: 3 pass, the 4th is over budget
    meta = _run(chunks, db, clock=_clock([0.0, 1.0, 2.0, 100.0]), deadline=50.0)

    assert db.committed_chunks == chunks[:3]
    assert db.watermark == chunks[2][1]  # sync_state = chunk3.end
    assert len(db.connections) == 3  # no fourth chunk started
    assert meta["status"] == "PARTIAL_CATCHUP"
    assert meta["stopped_by_soft_deadline"] is True
    assert meta["catchup_complete"] is False
    assert meta["chunks_total"] == 6 and meta["chunks_attempted"] == 3 and meta["chunks_completed"] == 3
    assert meta["requested_window_end"] == chunks[-1][1].isoformat()
    assert meta["last_committed_end"] == chunks[2][1].isoformat()


# =====================================================================
# Case 3 — all chunks succeed
# =====================================================================

def test_case3_all_chunks_succeed_is_pass_watermark_at_requested_end():
    chunks = _chunks(4)
    db = FakeDB()
    meta = _run(chunks, db)

    assert db.committed_chunks == chunks
    assert db.watermark == chunks[-1][1]
    assert meta["status"] == "PASS"
    assert meta["catchup_complete"] is True
    assert meta["last_committed_end"] == meta["requested_window_end"]
    # one independent commit per chunk, never one big final commit
    assert db.events.count("commit") == 4
    assert all(c.committed and c.closed for c in db.connections)


# =====================================================================
# Case 4 — first chunk fails
# =====================================================================

def test_case4_first_chunk_fails_zero_watermark_advancement():
    chunks = _chunks(3)
    db = FakeDB()
    meta = _run(chunks, db, fail_at=0)

    assert db.committed_chunks == []
    assert db.watermark is None
    assert meta["status"] == "FAIL"
    assert meta["chunks_completed"] == 0
    assert meta["last_committed_end"] is None


def test_deadline_already_passed_before_first_chunk_is_fail_not_partial():
    chunks = _chunks(2)
    db = FakeDB()
    meta = _run(chunks, db, clock=lambda: 100.0, deadline=0.0)
    assert db.connections == []
    assert db.watermark is None
    assert meta["status"] == "FAIL"
    assert meta["error_class"] == "NO_PROGRESS"


def test_empty_chunks_is_fail():
    meta = _run([], FakeDB())
    assert meta["status"] == "FAIL"


# =====================================================================
# Case 5 — external termination after prior committed chunks
# =====================================================================

def test_case5_external_kill_mid_chunk_cannot_roll_back_earlier_commits():
    chunks = _chunks(5)
    db = FakeDB()
    with pytest.raises(SimulatedExternalKill):
        _run(chunks, db, kill_at=2)

    # chunks 1-2 were committed BEFORE chunk 3 was even fetched — they
    # never depended on any later/final commit.
    assert db.committed_chunks == chunks[:2]
    assert db.watermark == chunks[1][1]
    assert db.events == [
        f"fetch:{chunks[0][0].isoformat()}", "commit",
        f"fetch:{chunks[1][0].isoformat()}", "commit",
        f"fetch:{chunks[2][0].isoformat()}",
    ]
    killed = db.connections[2]
    assert not killed.committed
    assert killed.pending_chunks == []  # in-flight chunk discarded (connection closed uncommitted)


def test_worker_main_does_not_commit_or_move_sync_state_after_orders_catchup():
    # The incremental orders branch in main() must return straight after
    # the durable catch-up — no final upsert_sync_state/commit that the
    # earlier chunks could depend on.
    m = re.search(r'if mode != "reconcile" and domain == "orders":(.*?)\n    conn = ic.get_db_conn\(\)',
                  WORKER_SRC, re.DOTALL)
    assert m, "incremental orders branch not found in main()"
    branch = m.group(1)
    assert "run_orders_incremental(" in branch
    assert "upsert_sync_state" not in branch
    assert ".commit()" not in branch


# =====================================================================
# Case 6 — normal 30-minute steady-state window
# =====================================================================

def test_case6_steady_state_30min_window_is_one_chunk_pass():
    start = datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    chunks = _tiktok_orders_compute_chunks(start, end)
    db = FakeDB()
    meta = _run(chunks, db)
    assert chunks == [(start, end)]
    assert meta["status"] == "PASS"
    assert db.watermark == end
    assert db.events.count("commit") == 1


# =====================================================================
# Progress markers — safe fields only, one per committed chunk
# =====================================================================

def test_progress_marker_emitted_per_committed_chunk_only():
    chunks = _chunks(4)
    emitted = []
    _run(chunks, FakeDB(), fail_at=2, emitted=emitted)
    chunk_markers = [p for prefix, p in emitted if prefix == TIKTOK_ORDERS_CHUNK_MARKER]
    assert [m["chunk_end"] for m in chunk_markers] == [chunks[0][1].isoformat(), chunks[1][1].isoformat()]
    for m in chunk_markers:
        assert set(m) == {"chunk_start", "chunk_end", "duration_seconds", "orders_seen", "orders_inserted",
                          "orders_updated", "items_inserted", "items_updated", "committed"}
        assert m["committed"] is True
    summaries = [p for prefix, p in emitted if prefix == TIKTOK_ORDERS_CATCHUP_MARKER]
    assert len(summaries) == 1
    assert "error" not in summaries[0]  # raw error text never in the marker


def test_default_emit_flushes_parseable_marker_line(capsys):
    incr_worker._emit_marker(TIKTOK_ORDERS_CHUNK_MARKER, {"chunk_end": "x", "committed": True})
    line = capsys.readouterr().out.strip()
    assert line.startswith(TIKTOK_ORDERS_CHUNK_MARKER)
    assert json.loads(line[len(TIKTOK_ORDERS_CHUNK_MARKER):]) == {"chunk_end": "x", "committed": True}


# =====================================================================
# run_orders_incremental — real write/watermark wiring against a fake
# cursor (no DB, no HTTP): each chunk's fact_order UPSERTs AND its
# etl_sync_state update run on the same connection, then that
# connection commits, before the next chunk's fetch.
# =====================================================================

class RecordingConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        if "control.etl_sync_state" in sql:
            self.log.append(("sync_state", params[3]))
        elif "core.fact_order_item" in sql:
            self.log.append(("item", params[2]))
        elif "core.fact_order" in sql:
            self.log.append(("order", params[2]))

    def fetchone(self):
        return (True,)

    def commit(self):
        self.log.append(("commit", None))

    def rollback(self):
        self.log.append(("rollback", None))

    def close(self):
        pass


def test_run_orders_incremental_commits_rows_and_watermark_per_chunk(monkeypatch, capsys):
    start = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=10)  # 3 chunks: 4h, 4h, 2h
    log: list = []

    def fake_fetch(session, s, e, _log):
        log.append(("fetch", s))
        return [{"id": f"o{s.hour}", "create_time": int(s.timestamp()), "line_items": [{"id": "li1"}]}], 1

    monkeypatch.setattr(incr_worker.pilot_common, "bootstrap_session", lambda: object())
    monkeypatch.setattr(incr_worker, "_fetch_orders_window", fake_fetch)

    result, meta = incr_worker.run_orders_incremental(
        "run-1", "shop-1", start, end, _fake_log, connect=lambda: RecordingConn(log),
    )
    ends = [start + timedelta(hours=4), start + timedelta(hours=8), end]
    expected = []
    for (s, e) in zip([start] + ends[:-1], ends):
        expected += [("fetch", s), ("order", f"o{s.hour}"), ("item", f"o{s.hour}"), ("sync_state", e), ("commit", None)]
    assert log == expected
    assert meta["status"] == "PASS"
    assert result["order"]["received"] == 3 and result["order_item"]["received"] == 3

    out = capsys.readouterr().out
    assert out.count(TIKTOK_ORDERS_CHUNK_MARKER) == 3


def test_reconciliation_orders_path_never_touches_sync_state():
    # run_orders (reconciliation single-day handler) and the shared
    # fetch/write helpers must never move the incremental watermark.
    for fn in ("run_orders", "run_orders_window", "_write_orders", "_fetch_orders_window"):
        m = re.search(rf"^def {fn}\(.*?(?=^def |\Z)", WORKER_SRC, re.DOTALL | re.MULTILINE)
        assert m, fn
        assert "upsert_sync_state" not in m.group(0), fn
        assert ".commit()" not in m.group(0), fn


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
        f"too tight to reliably finish an in-flight chunk"
    )


# =====================================================================
# P11-FIX-4 — tiktok_client.NetworkError (e.g. ReadTimeout) is retried
# by the existing bounded backoff instead of failing on the first try
# (run 35948968552: TIKTOK/finance D3 died on one ReadTimeout).
# =====================================================================

def test_network_error_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(incr_worker.ic.time, "sleep", lambda s: None)
    attempts = []

    def call():
        attempts.append(1)
        if len(attempts) < 3:
            raise incr_worker.NetworkError("GET /finance/x/statement_transactions: network/timeout error: ReadTimeout")
        return "ok"

    assert incr_worker._with_backoff(call, "tiktok finance x", _fake_log) == "ok"
    assert len(attempts) == 3


def test_network_error_still_raises_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(incr_worker.ic.time, "sleep", lambda s: None)
    attempts = []

    def call():
        attempts.append(1)
        raise incr_worker.NetworkError("GET /x: network/timeout error: ReadTimeout")

    with pytest.raises(RuntimeError, match="ReadTimeout"):
        incr_worker._with_backoff(call, "tiktok finance x", _fake_log)
    assert len(attempts) == 1 + len(incr_worker.ic.BACKOFF_DELAYS)


def test_non_network_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(incr_worker.ic.time, "sleep", lambda s: None)
    attempts = []

    def call():
        attempts.append(1)
        raise ValueError("bad payload")

    with pytest.raises(ValueError):
        incr_worker._with_backoff(call, "x", _fake_log)
    assert len(attempts) == 1


def test_all_tiktok_worker_api_calls_use_the_network_aware_backoff():
    assert "ic.with_backoff(call" not in WORKER_SRC
