"""
P11 RECOVERY D — TikTok incr_worker.py main() emits flushed phase markers
around the incremental success path (handler_complete ... before_exit),
safe fields only, without breaking the "last JSON line" result parsing
that pipelines/incremental.py relies on. DB and handler are faked.

Run with: python3 -m pytest integrations/tiktok_shop/pilot_reporting/test_worker_phase_markers.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_worker  # noqa: E402

FAKE_TOKEN = "FAKE-ACCESS-phase-0000"
PHASES = ["handler_complete", "sync_state_updated", "db_commit_complete", "db_close_complete",
          "result_emitted", "before_exit"]


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return self

    def commit(self):
        self.log.append("commit")

    def close(self):
        self.log.append("close")

    def rollback(self):
        self.log.append("rollback")


def _run(monkeypatch, capsys, handler):
    log = []
    monkeypatch.setattr(incr_worker.pilot_common, "load_shop_info", lambda: {"shop_id": "shop-1",
                                                                            "access_token": FAKE_TOKEN})
    monkeypatch.setattr(incr_worker.ic, "get_db_conn", lambda: FakeConn(log))
    monkeypatch.setattr(incr_worker.ic, "upsert_sync_state", lambda *a, **k: log.append("sync_state"))
    monkeypatch.setitem(incr_worker.HANDLERS, "finance", handler)
    monkeypatch.setattr(sys, "argv", ["incr_worker.py", "finance", "2026-10-05T04:00:00+00:00",
                                      "2026-10-05T04:45:00+00:00", "run-fin"])
    rc = incr_worker.main()
    return rc, capsys.readouterr().out, log


def _phases(out):
    return [json.loads(l[len(incr_worker.WORKER_PHASE_MARKER):]) for l in out.splitlines()
            if l.startswith(incr_worker.WORKER_PHASE_MARKER)]


def test_success_path_emits_all_phases_in_order(monkeypatch, capsys):
    rc, out, log = _run(monkeypatch, capsys, lambda *a: {"settlement": {"received": 3}})
    assert rc == 0 and log == ["sync_state", "commit", "close"]
    phases = _phases(out)
    assert [p["phase"] for p in phases] == PHASES
    assert all(set(p) == {"domain", "etl_run_id", "phase", "elapsed_seconds", "at"} for p in phases)
    assert {p["etl_run_id"] for p in phases} == {"run-fin"}
    assert FAKE_TOKEN not in out
    # result line is still the last parseable JSON line for incremental.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "pipelines"))
    import incremental
    assert incremental._worker_status_hint(False, out) == "PASS"
    lines = out.splitlines()
    result_idx = next(i for i, l in enumerate(lines) if l.startswith('{"status": "PASS"'))
    assert lines.index(next(l for l in lines if '"result_emitted"' in l)) > result_idx


def test_handler_failure_stops_before_handler_complete(monkeypatch, capsys):
    def boom(*a):
        raise RuntimeError("API exploded")

    rc, out, log = _run(monkeypatch, capsys, boom)
    assert rc == 1 and "rollback" in log and "commit" not in log
    assert _phases(out) == []
    assert json.loads(out.strip().splitlines()[-1])["status"] == "FAIL"
