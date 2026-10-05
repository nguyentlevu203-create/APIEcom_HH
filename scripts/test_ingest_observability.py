"""
P11 RECOVERY D — ingestion observability: live relay of worker output,
per-domain START/END/FINALIZED markers, TikTok worker phase markers, and
the orchestrator's in-flight report when the ingestion stage is killed.
Real child processes for the timeout tests; DB and APIs always faked.

Run with: python3 -m pytest scripts/test_ingest_observability.py -v
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipelines"))
sys.path.insert(0, str(ROOT / "scripts"))
import incr_common as ic  # noqa: E402
import incremental  # noqa: E402
import run_production_cycle as cycle  # noqa: E402

FAKE_SECRET = "FAKE-SECRET-should-never-appear-9999"
START_END_KEYS = {"source_system", "domain", "etl_run_id", "started_at", "finished_at", "elapsed_seconds",
                  "worker_timeout", "returncode", "timed_out", "status"}


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


# --- the gap that lost run 37262150230's output -----------------------------------

def test_buffered_child_output_is_lost_on_kill_but_unbuffered_survives(tmp_path):
    child = _script(tmp_path, "child.py", """
        import time
        print("HH_INGEST_DOMAIN_START_JSON={\\"etl_run_id\\": \\"r1\\"}")
        time.sleep(30)
    """)
    lost, timed_out = ic.run_contained_subprocess([sys.executable, str(child)], tmp_path, 1.5)
    assert timed_out and "HH_INGEST_DOMAIN_START_JSON" not in (lost.stdout or "")
    kept, timed_out = ic.run_contained_subprocess([sys.executable, str(child)], tmp_path, 1.5, env=ic.unbuffered_env())
    assert timed_out and "HH_INGEST_DOMAIN_START_JSON" in kept.stdout


def test_streaming_relays_live_and_still_captures(tmp_path):
    child = _script(tmp_path, "child.py", """
        import sys
        print("line-1"); print("line-2"); print("err-1", file=sys.stderr)
        print('{"status": "PASS"}')
    """)
    relayed_out, relayed_err = [], []
    proc, timed_out = ic.run_streaming_contained_subprocess(
        [sys.executable, str(child)], tmp_path, 10, relay_out=relayed_out.append, relay_err=relayed_err.append)
    assert not timed_out and proc.returncode == 0
    assert relayed_out == ["line-1\n", "line-2\n", '{"status": "PASS"}\n'] and proc.stdout == "".join(relayed_out)
    assert relayed_err == ["err-1\n"] and proc.stderr == "err-1\n"


def test_streaming_timeout_kills_group_and_keeps_partial_output(tmp_path):
    child = _script(tmp_path, "child.py", """
        import subprocess, sys, time
        print("before-hang")
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # grandchild in same group
        time.sleep(60)
    """)
    proc, timed_out = ic.run_streaming_contained_subprocess(
        [sys.executable, str(child)], tmp_path, 2, relay_out=lambda line: None)
    assert timed_out and proc.returncode is not None and proc.returncode < 0
    assert "before-hang" in proc.stdout


def test_nested_worker_phase_markers_survive_outer_stage_kill(tmp_path):
    """Outer stage (orchestrator) -> middle (incremental.py-like, streaming
    relay) -> worker that emits phases then hangs after commit, exactly
    the run 37262150230 shape. The outer kill must still yield the markers."""
    worker = _script(tmp_path, "worker.py", f"""
        import json, time
        for phase in ("handler_complete", "sync_state_updated", "db_commit_complete"):
            print("HH_TIKTOK_WORKER_PHASE_JSON=" + json.dumps({{"domain": "finance", "etl_run_id": "run-fin", "phase": phase}}))
        time.sleep(60)  # hang after commit
    """)
    middle = _script(tmp_path, "middle.py", f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'pipelines')!r})
        import incr_common as ic
        ic.emit_marker("HH_INGEST_DOMAIN_START_JSON=", {{"source_system": "TIKTOK", "domain": "finance",
            "etl_run_id": "run-fin", "started_at": "t0", "worker_timeout": 3600, "status": "STARTED"}})
        ic.run_streaming_contained_subprocess([sys.executable, {str(worker)!r}], {str(tmp_path)!r}, 3600)
    """)
    outer, timed_out = ic.run_contained_subprocess([sys.executable, str(middle)], tmp_path, 4, env=ic.unbuffered_env())
    assert timed_out
    parsed = cycle.parse_ingest_domain_markers(outer.stdout)
    assert parsed["inflight"]["domain"] == "finance"
    assert parsed["inflight"]["stage"] == "IN_WORKER"
    assert parsed["inflight"]["last_worker_phase"] == "db_commit_complete"


# --- marker parsing / in-flight classification ------------------------------------

def _line(prefix, **payload):
    return prefix + json.dumps(payload)


def test_inflight_finalizing_run_log_when_end_but_no_finalized():
    stdout = "\n".join([
        _line(cycle.INGEST_DOMAIN_START_MARKER, source_system="SHOPEE", domain="orders", etl_run_id="a", status="STARTED"),
        _line(cycle.INGEST_DOMAIN_END_MARKER, source_system="SHOPEE", domain="orders", etl_run_id="a", status="PASS",
              elapsed_seconds=3.0, returncode=0, timed_out=False),
        _line(cycle.INGEST_DOMAIN_FINALIZED_MARKER, source_system="SHOPEE", domain="orders", etl_run_id="a", status="PASS"),
        _line(cycle.INGEST_DOMAIN_START_MARKER, source_system="TIKTOK", domain="finance", etl_run_id="b", status="STARTED"),
        _line(cycle.INGEST_DOMAIN_END_MARKER, source_system="TIKTOK", domain="finance", etl_run_id="b", status="PASS",
              elapsed_seconds=36.1, returncode=0, timed_out=False),
        "some worker log line",
    ])
    parsed = cycle.parse_ingest_domain_markers(stdout)
    assert [t["domain"] for t in parsed["timeline"]] == ["orders", "finance"]
    assert parsed["inflight"]["domain"] == "finance" and parsed["inflight"]["stage"] == "FINALIZING_RUN_LOG"


def test_no_inflight_when_all_finalized_and_malformed_lines_ignored():
    stdout = "\n".join([
        _line(cycle.INGEST_DOMAIN_START_MARKER, domain="x", etl_run_id="a"),
        _line(cycle.INGEST_DOMAIN_END_MARKER, domain="x", etl_run_id="a", status="FAIL"),
        _line(cycle.INGEST_DOMAIN_FINALIZED_MARKER, domain="x", etl_run_id="a", status="FAIL"),
        cycle.INGEST_DOMAIN_START_MARKER + "{not json",
    ])
    parsed = cycle.parse_ingest_domain_markers(stdout)
    assert parsed["inflight"] is None and len(parsed["events"]) == 3


# --- incremental.run_domain emits START before the worker, END before finish_run_log

class _FakeConn:
    def cursor(self):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def test_run_domain_markers_order_and_safe_fields(monkeypatch, capsys):
    order = []
    monkeypatch.setattr(ic, "get_db_conn", lambda: _FakeConn())
    monkeypatch.setattr(ic, "get_sync_state", lambda *a: None)
    monkeypatch.setattr(ic, "start_run_log", lambda *a, **k: "run-123")
    monkeypatch.setattr(ic, "finish_run_log", lambda *a, **k: order.append("finish_run_log"))

    def fake_stream(args, cwd, timeout):
        order.append(("worker", timeout))
        assert FAKE_SECRET not in " ".join(map(str, args))
        out = 'worker says hi\n{"status": "PASS", "result": {"order": {"received": 2}}}\n'
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})(), False

    monkeypatch.setattr(ic, "run_streaming_contained_subprocess", fake_stream)
    monkeypatch.setenv("SOME_TOKEN", FAKE_SECRET)
    result = incremental.run_domain("TIKTOK", "finance", "shop-1", force=True)
    assert result["status"] == "PASS"
    out = capsys.readouterr().out
    lines = out.splitlines()
    start = [json.loads(l[len(incremental.INGEST_DOMAIN_START_MARKER):]) for l in lines
             if l.startswith(incremental.INGEST_DOMAIN_START_MARKER)]
    end = [json.loads(l[len(incremental.INGEST_DOMAIN_END_MARKER):]) for l in lines
           if l.startswith(incremental.INGEST_DOMAIN_END_MARKER)]
    assert len(start) == len(end) == 1
    assert set(start[0]) == set(end[0]) == START_END_KEYS
    assert end[0]["status"] == "PASS" and end[0]["returncode"] == 0 and end[0]["timed_out"] is False
    assert end[0]["worker_timeout"] == incremental.FINANCE_TIMEOUT_SECONDS
    assert order[0][0] == "worker" and order[-1] == "finish_run_log"
    assert out.index(incremental.INGEST_DOMAIN_START_MARKER) < out.index(incremental.INGEST_DOMAIN_END_MARKER)
    assert FAKE_SECRET not in out


@pytest.mark.parametrize("timed_out,stdout,expected", [
    (True, "", "TIMEOUT"),
    (False, '{"status": "PARTIAL_CATCHUP"}', "PARTIAL_CATCHUP"),
    (False, 'x\nHH_TIKTOK_WORKER_PHASE_JSON={"phase": "before_exit"}', "RESULT_UNPARSEABLE"),
    (False, '{"status": "PASS"}\nHH_TIKTOK_WORKER_PHASE_JSON={"phase": "before_exit"}', "PASS"),
])
def test_worker_status_hint(timed_out, stdout, expected):
    assert incremental._worker_status_hint(timed_out, stdout) == expected


def test_production_timeouts_unchanged():
    assert cycle.INGESTION_TIMEOUT_SECONDS == 3600
    assert cycle.RECONCILIATION_TIMEOUT_SECONDS == 6000
    assert incremental.TIKTOK_ORDERS_TIMEOUT_SECONDS == 900
    assert incremental.DEFAULT_WORKER_TIMEOUT_SECONDS == 600
