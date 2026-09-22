"""
P11-TER.2/.4 — unit tests for the shared retry/backoff and process-group
containment helpers in pipelines/incr_common.py. No DB, no network.

Run with: python3 -m pytest pipelines/test_incr_common.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_common as ic  # noqa: E402


# =====================================================================
# with_backoff — Retry-After override (P11-TER.2)
# =====================================================================

def test_with_backoff_success_first_try_no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(ic.time, "sleep", lambda s: calls.append(s))
    result = ic.with_backoff(lambda: "ok", "label", lambda msg: None)
    assert result == "ok"
    assert calls == []  # first attempt has a 0 delay entry, never actually sleeps


def test_with_backoff_uses_default_table_when_no_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ic.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ic.TransientHTTPError("500")  # no retry_after
        return "ok"

    assert ic.with_backoff(flaky, "label", lambda msg: None) == "ok"
    assert sleeps == list(ic.BACKOFF_DELAYS[:2])


def test_with_backoff_honors_server_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ic.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def rate_limited():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ic.TransientHTTPError("429", retry_after=7)
        return "ok"

    assert ic.with_backoff(rate_limited, "label", lambda msg: None) == "ok"
    assert sleeps == [7]  # overrides the default 5s table entry, not added to it


def test_with_backoff_caps_retry_after_at_max(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ic.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def wildly_long_retry_after():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ic.TransientHTTPError("429", retry_after=99999)
        return "ok"

    ic.with_backoff(wildly_long_retry_after, "label", lambda msg: None)
    assert sleeps == [ic.MAX_RETRY_AFTER_SECONDS]


def test_with_backoff_exhausts_retry_budget_and_raises(monkeypatch):
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)

    def always_rate_limited():
        raise ic.TransientHTTPError("429", retry_after=3)

    with pytest.raises(ic.TransientHTTPError):
        ic.with_backoff(always_rate_limited, "label", lambda msg: None)


def test_with_backoff_real_error_not_retried():
    def real_error():
        raise RuntimeError("not a transient error")

    with pytest.raises(RuntimeError):
        ic.with_backoff(real_error, "label", lambda msg: None)


# =====================================================================
# run_contained_subprocess — process-group timeout containment (P11-TER.4)
# =====================================================================

def test_run_contained_subprocess_normal_completion():
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, "-c", "print('hello')"], Path.cwd(), timeout=10,
    )
    assert not timed_out
    assert proc.returncode == 0
    assert "hello" in proc.stdout


def test_run_contained_subprocess_nonzero_exit_is_not_a_timeout():
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, "-c", "import sys; sys.exit(2)"], Path.cwd(), timeout=10,
    )
    assert not timed_out
    assert proc.returncode == 2


def test_run_contained_subprocess_kills_grandchild_on_timeout(tmp_path):
    """The exact failure mode P11-TER.4 fixes: a plain
    subprocess.run(timeout=...) only kills its direct child, leaking any
    process THAT child spawned. Here the direct child spawns a
    long-sleeping grandchild and writes its PID to a file before
    blocking forever itself; after the timeout we assert the grandchild
    is also dead, not just the direct child."""
    pid_file = tmp_path / "grandchild.pid"
    script = f"""
import os, subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c",
    "import time; time.sleep(60)"])
with open({str(pid_file)!r}, "w") as f:
    f.write(str(p.pid))
time.sleep(60)
"""
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, "-c", script], tmp_path, timeout=1,
    )
    assert timed_out
    grandchild_pid = int(pid_file.read_text().strip())
    time.sleep(0.5)  # let the SIGKILL actually land
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)  # signal 0 = existence check only


def test_run_contained_subprocess_captures_output_before_kill(tmp_path):
    script = "import sys, time; print('before'); sys.stdout.flush(); time.sleep(30)"
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, "-c", script], tmp_path, timeout=1,
    )
    assert timed_out
    assert "before" in proc.stdout
