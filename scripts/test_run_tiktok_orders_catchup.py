"""
P11 RECOVERY B — unit tests for scripts/run_tiktok_orders_catchup.py and
.github/workflows/tiktok-orders-catchup.yml. Pure: fake DB watermark,
fake worker stdout, fake clock. No DB, no HTTP, no GitHub.

Run with: python3 -m pytest scripts/test_run_tiktok_orders_catchup.py -v
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tiktok_orders_catchup as rc  # noqa: E402
import run_production_cycle as cycle  # noqa: E402

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "tiktok-orders-catchup.yml").read_text(encoding="utf-8")
PROD_WORKFLOW = (ROOT / ".github" / "workflows" / "p3-incremental.yml").read_text(encoding="utf-8")
INCREMENTAL_SRC = (ROOT / "pipelines" / "incremental.py").read_text(encoding="utf-8")
WORKER_SRC = (ROOT / "integrations" / "tiktok_shop" / "pilot_reporting" / "incr_worker.py").read_text(encoding="utf-8")

WM0 = datetime(2026, 9, 30, 11, 35, 40, 105253, tzinfo=UTC)
END = WM0 + timedelta(hours=10)  # 3 chunks of 4h (+5min overlap) — 4h, 4h, ~2h05m
FAKE_TOKEN = "FAKE-ACCESS-zzzzzzzzzzzzzzzz"


class FakeEnv:
    """Fake sync_state + run log + worker. Each scripted pass is a list of
    chunk ends to commit, plus the worker's final status."""

    def __init__(self, passes, watermark=WM0, clock_step=300.0, corrupt_watermark=False):
        self.watermark = watermark
        self.passes = list(passes)
        self.clock = 0.0
        self.clock_step = clock_step
        self.corrupt_watermark = corrupt_watermark
        self.run_log = {}
        self.worker_calls = []
        self.echoed = []

    def read_watermark(self):
        return self.watermark

    def start_run(self):
        rid = f"run-{len(self.run_log) + 1}"
        self.run_log[rid] = ("running", 0, "")
        return rid

    def finish_run(self, rid, status, rows, note):
        self.run_log[rid] = (status, rows, note)

    def now(self):
        return self.clock

    def run_worker(self, start, end, rid):
        self.worker_calls.append((start, end, rid))
        self.clock += self.clock_step
        script = self.passes.pop(0)
        lines, last = [], None
        for chunk_end in script.get("commit", []):
            last = chunk_end
            self.watermark = chunk_end
            lines.append(rc.CHUNK_MARKER + json.dumps({
                "chunk_start": start.isoformat(), "chunk_end": chunk_end.isoformat(),
                "orders_inserted": 2, "orders_updated": 1, "items_inserted": 3, "items_updated": 0, "committed": True}))
        if self.corrupt_watermark and last:
            self.watermark = last - timedelta(hours=1)
        if script.get("timed_out"):
            return -15, "\n".join(lines), "", True
        n_commit = len(script.get("commit", []))
        lines.append(rc.CATCHUP_MARKER + json.dumps({
            "status": script["status"], "chunks_attempted": n_commit + (1 if script.get("error") else 0),
            "chunks_completed": n_commit, "last_committed_end": last.isoformat() if last else None,
            "error_class": script.get("error_class")}))
        final = {"status": script["status"]}
        if script.get("error"):
            final["error"] = script["error"]
        lines.append(json.dumps(final))
        return (1 if script["status"] == "FAIL" else 0), "\n".join(lines), script.get("stderr", ""), False

    def go(self, budget=5400):
        return rc.run_catchup(
            requested_end=END, budget_seconds=budget, read_watermark=self.read_watermark,
            start_run=self.start_run, finish_run=self.finish_run, run_worker=self.run_worker,
            now_monotonic=self.now, echo=self.echoed.append,
        )


C1, C2, C3 = WM0 + timedelta(hours=3, minutes=55), WM0 + timedelta(hours=7, minutes=55), END


def test_pass_backlog_cleared_in_one_pass():
    env = FakeEnv([{"status": "PASS", "commit": [C1, C2, C3]}])
    s = env.go()["summary"]
    assert s["status"] == "PASS" and s["catchup_complete"] is True
    assert s["chunks_committed"] == 3 and s["last_committed_end"] == C3.isoformat()
    assert s["before_watermark"] == WM0.isoformat() and s["after_watermark"] == END.isoformat()
    assert s["requested_start"] == (WM0 - timedelta(minutes=5)).isoformat()
    assert env.run_log["run-1"][0] == "success"
    # worker window = watermark - 5min overlap .. fixed requested_end
    assert env.worker_calls[0][:2] == (WM0 - timedelta(minutes=5), END)


def test_partial_passes_continue_until_cleared():
    env = FakeEnv([{"status": "PARTIAL_CATCHUP", "commit": [C1]},
                   {"status": "PARTIAL_CATCHUP", "commit": [C2]},
                   {"status": "PASS", "commit": [C3]}])
    s = env.go()["summary"]
    assert s["status"] == "PASS" and s["passes"] == 3 and s["chunks_committed"] == 3
    # each pass resumes from the DB watermark just committed, same requested_end
    assert [c[0] for c in env.worker_calls] == [WM0 - timedelta(minutes=5), C1 - timedelta(minutes=5),
                                                 C2 - timedelta(minutes=5)]
    assert all(c[1] == END for c in env.worker_calls)
    assert "PARTIAL_CATCHUP" in env.run_log["run-1"][2]


def test_partial_when_budget_runs_out_with_progress_exits_zero(monkeypatch):
    env = FakeEnv([{"status": "PARTIAL_CATCHUP", "commit": [C1]},
                   {"status": "PARTIAL_CATCHUP", "commit": [C2]}], clock_step=800)
    result = env.go(budget=rc.PASS_RESERVE_SECONDS + 900)  # room for exactly one more pass after the first
    s = result["summary"]
    assert s["status"] == "PARTIAL_CATCHUP"
    assert s["stopped_by_soft_deadline"] is True and s["catchup_complete"] is False
    assert s["chunks_committed"] == 2 and s["after_watermark"] == C2.isoformat() == s["last_committed_end"]


def test_zero_progress_with_backlog_fails():
    env = FakeEnv([{"status": "FAIL", "commit": [], "error_class": "NO_PROGRESS",
                    "error": "TikTok orders: no chunk completed out of 3 requested"}])
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["chunks_committed"] == 0
    assert s["after_watermark"] == WM0.isoformat()
    assert env.run_log["run-1"][0] == "fail"


def test_budget_too_small_for_any_pass_is_zero_progress_fail():
    env = FakeEnv([])
    s = env.go(budget=60)["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "NO_PROGRESS" and env.worker_calls == []


def test_chunk_failure_after_progress_is_fail_and_keeps_progress():
    env = FakeEnv([{"status": "FAIL", "commit": [C1], "error_class": "OperationalError",
                    "error": "server closed the connection unexpectedly"}])
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "OperationalError"
    assert s["chunks_committed"] == 1 and s["after_watermark"] == C1.isoformat()


def test_token_persistence_failure_fails_closed_and_is_named():
    env = FakeEnv([{"status": "FAIL", "commit": [],
                    "error": "TOKEN_STATE_PERSISTENCE_FAILED: write to TIKTOK_TOKENS_JSON failed after retries"}])
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "TOKEN_STATE_PERSISTENCE_FAILED"
    assert env.run_log["run-1"][2].startswith("TOKEN_STATE_PERSISTENCE_FAILED")


def test_auth_or_refresh_failure_fails():
    env = FakeEnv([{"status": "FAIL", "commit": [], "error": "REAUTHORIZATION REQUIRED — refresh_token has expired."}])
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "TOKEN_REFRESH_FAILURE"


def test_worker_timeout_fails_but_counts_committed_chunks():
    env = FakeEnv([{"timed_out": True, "commit": [C1]}])
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "WORKER_TIMEOUT"
    assert s["chunks_committed"] == 1 and s["last_committed_end"] == C1.isoformat()


def test_unparseable_worker_output_fails():
    env = FakeEnv([])
    env.run_worker = lambda start, end, rid: (1, "Traceback garbage", "boom", False)
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "WORKER_RESULT_UNPARSEABLE"


def test_watermark_inconsistency_fails():
    env = FakeEnv([{"status": "PARTIAL_CATCHUP", "commit": [C1]}], corrupt_watermark=True)
    s = env.go()["summary"]
    assert s["status"] == "FAIL" and s["error_class"] == "WATERMARK_INCONSISTENT"
    assert env.run_log["run-1"][0] == "fail"


def test_watermark_must_not_move_without_committed_chunk():
    env = FakeEnv([])

    def worker(start, end, rid):
        env.watermark = WM0 + timedelta(hours=1)  # moved, yet no chunk marker
        return 1, json.dumps({"status": "FAIL", "error": "x"}), "", False

    env.run_worker = worker
    assert env.go()["summary"]["error_class"] == "WATERMARK_INCONSISTENT"


def test_db_failure_and_missing_watermark_fail():
    env = FakeEnv([])
    env.read_watermark = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert env.go()["summary"]["error_class"] == "DB_FAILURE"
    env2 = FakeEnv([], watermark=None)
    assert env2.go()["summary"]["error_class"] == "NO_WATERMARK"


def test_already_current_is_pass_without_worker():
    env = FakeEnv([], watermark=END + timedelta(minutes=1))
    s = env.go()["summary"]
    assert s["status"] == "PASS" and env.worker_calls == []


def test_marker_has_required_safe_fields_and_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rc, "LOG_DIR", tmp_path)
    monkeypatch.setattr(rc, "_load_shop_id", lambda: "fake-shop")
    for status, code in (("PASS", 0), ("PARTIAL_CATCHUP", 0), ("FAIL", 1)):
        fake = {"summary": {"status": status, "error_class": None}, "passes": [], "chunks": []}
        monkeypatch.setattr(rc, "_make_deps", lambda shop_id: (None, None, None, None))
        monkeypatch.setattr(rc, "run_catchup", lambda **kw: fake)
        assert rc.main(["--budget-minutes", "30"]) == code
    out = capsys.readouterr().out
    assert out.count(rc.RESULT_MARKER) == 3


def test_summary_has_required_safe_fields_and_no_raw_error():
    env = FakeEnv([{"status": "FAIL", "commit": [C1], "error_class": "TikTokAPIError",
                    "error": f"bad response access_token={FAKE_TOKEN}"}])
    result = env.go()
    payload = json.dumps(result)
    assert FAKE_TOKEN not in payload, "marker/artifact must never carry raw worker errors"
    for key in ("before_watermark", "requested_start", "requested_end", "chunks_attempted", "chunks_committed",
                "last_committed_end", "after_watermark", "catchup_complete", "stopped_by_soft_deadline",
                "status", "elapsed_seconds"):
        assert key in result["summary"]


def test_budget_bounds_enforced():
    with pytest.raises(SystemExit):
        rc.main(["--budget-minutes", "500"])


def test_pass_timeout_matches_production_worker_timeout_and_soft_deadline_margin():
    prod = int(re.search(r"^TIKTOK_ORDERS_TIMEOUT_SECONDS = (\d+)", INCREMENTAL_SRC, re.M).group(1))
    soft = int(re.search(r"^TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS = (\d+)", WORKER_SRC, re.M).group(1))
    assert rc.PASS_TIMEOUT_SECONDS == prod and soft < rc.PASS_TIMEOUT_SECONDS


def test_workflow_is_manual_only_and_scoped():
    active = "\n".join(line for line in WORKFLOW.splitlines() if not line.lstrip().startswith("#"))
    assert "workflow_dispatch" in active
    assert "schedule" not in active and "cron" not in active
    assert "group: p3-incremental-production-cycle" in active
    assert "cancel-in-progress: false" in active
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", active))
    assert secrets == {"HH_ETL_WRITER_DATABASE_URL", "TIKTOK_APP_SECRET", "TIKTOK_TOKENS_JSON",
                       "TIKTOK_SHOP_INFO_JSON", "GH_SECRETS_WRITER_TOKEN"}
    assert "umask 077" in active and "chmod 600" in active
    cleanup = active.split("Clean up provisioned token files", 1)[1]
    assert "if: always()" in cleanup.split("- name:", 1)[0]
    assert "run_tiktok_orders_catchup.py" in active
    for forbidden in ("run_production_cycle", "reconcile", "gold", "SHOPEE"):
        assert forbidden not in active


def test_production_workflow_schedule_still_off_and_partial_still_red():
    active = "\n".join(line for line in PROD_WORKFLOW.splitlines() if not line.lstrip().startswith("#"))
    assert "schedule" not in active
    ingestion = {"status": "FAILED", "ingestion_domain_results": {}, "ingestion_failed_domains": [],
                 "ingestion_partial_catchup_domains": ["TIKTOK/orders"]}
    verdict, reasons = cycle.compute_cycle_verdict(
        ingestion, [], {"status": "SUCCESS", "reconciliation_failed_domains": []}, "GREEN")
    assert verdict == "RED" and "INGESTION_DOMAIN_PARTIAL_CATCHUP:TIKTOK/orders" in reasons
