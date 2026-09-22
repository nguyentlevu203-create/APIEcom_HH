#!/usr/bin/env python3
"""P9.2 — full production cycle orchestrator.

Runs the complete chain for one scheduled cycle:

    API -> CORE (pipelines/incremental.py, per-domain isolated)
        -> GOLD (BASE_GOLD then PNL_ENRICHMENT scripts, in dependency order)
        -> reconciliation (pipelines/reconcile.py, D-1/D-3/D-7 re-check)
        -> source coverage (mart.v_ai_source_coverage, read-only, always live)
        -> production health check (scripts/production_healthcheck.py)

This does NOT reimplement any ETL/Gold logic — it only sequences the
existing, already-verified scripts as separate subprocesses, exactly as
_p6c1_orchestrator.py already established the pattern for the Gold/PNL
chain (that script is now superseded/incomplete for the current P8.5
metric set — this orchestrator supersedes it with the correct, current
script list, still calling each real script unchanged).

Per-source isolation: pipelines/incremental.py already isolates each
domain's fetch/UPSERT (one domain's exception does not stop another —
see run_domain() in that file) and returns a per-domain status dict.
This orchestrator preserves that isolation contract at the cycle level
too: a Gold-script failure is recorded and the cycle continues to the
remaining steps rather than aborting, so a partial cycle still leaves
whatever WAS computable in a correct, verifiable state.

Writes one JSON log file per run under artifacts/v0/cycle_logs/ and
prints a final GREEN/YELLOW/RED verdict.

Usage: python3 scripts/run_production_cycle.py
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_V0 = ROOT / "artifacts" / "v0"
LOG_DIR = ARTIFACTS_V0 / "cycle_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOCK_PATH = LOG_DIR / "production_cycle.lock"

sys.path.insert(0, str(ROOT / "pipelines"))
import incr_common as ic  # noqa: E402

PY = sys.executable

# P11-TER.3 — explicit, per-stage timeouts. Previously every subprocess
# in this file (ingestion, each Gold script, reconciliation) shared one
# hidden 1800s default in run_subprocess(), even though a single Gold-
# chain-adjacent domain (Shopee/TikTok finance) is separately allowed up
# to FINANCE_TIMEOUT_SECONDS=3600s by pipelines/incremental.py itself —
# proven live: the 1800s cap killed the whole ingestion subprocess mid
# TikTok-finance-fetch, well before that domain's own, larger timeout
# ever had a chance to fire. Each stage now gets its own bound sized to
# what that stage actually does; none of them is "no timeout".
INGESTION_TIMEOUT_SECONDS = 3600  # covers realistic multi-day catch-up across all domains
GOLD_SCRIPT_TIMEOUT_SECONDS = 600  # one Gold/PNL script, DB-only, no external API calls
RECONCILIATION_TIMEOUT_SECONDS = 1800  # D-1/D-3/D-7 re-check across all domains
HEALTHCHECK_TIMEOUT_SECONDS = 120  # unchanged — already its own explicit bound


class AlreadyRunningError(Exception):
    pass


def acquire_single_instance_lock():
    """P9.2 Section 2 — single-instance guard.

    Uses an OS advisory file lock (fcntl.flock, LOCK_EX | LOCK_NB) on a
    fixed lock file, held for the process's entire lifetime (the file
    descriptor is kept open in a module-level variable, never closed
    until exit). Chosen over a PID file specifically because flock has
    no stale-lock problem: the kernel releases the lock automatically
    the instant the holding process exits for ANY reason (clean exit,
    crash, kill -9, power loss) - there is no PID file to go stale and
    no cleanup step that can be skipped. A PID file, by contrast, can
    survive its process (e.g. after a hard crash) and then requires
    guessing whether a live PID belongs to the same job or was reused
    by an unrelated process - real failure modes this lock has none of.

    - Second concurrent invocation: flock raises BlockingIOError
      immediately (non-blocking) -> this function raises
      AlreadyRunningError -> main() prints ALREADY_RUNNING and exits 0
      (clean, not a failure) without starting incremental.py or any
      Gold script - no duplicate API cycle can start.
    - Never kills or inspects any other process - purely declines to
      proceed if the lock is held, exactly as required ("no destructive
      kill of an unknown process").
    - Scoped to the whole cycle only - does not wrap or alter
      per-domain error isolation inside pipelines/incremental.py, which
      remains entirely untouched.
    """
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunningError(f"another production cycle is already running (lock held: {LOCK_PATH})")
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n".encode())
    return fd  # keep open for the life of the process; GC/exit releases it

# P11-QUATER — Gold-chain scripts used to live only under artifacts/v0/,
# which .gitignore excludes wholesale as generated/proprietary output. A
# clean GitHub Actions checkout never had them, so every Gold script
# failed instantly with "No such file or directory" regardless of the
# ingestion-timeout fix — an independent defect from P11-TER's
# containment/timeout work. The 13 files these 7 entrypoints need
# (transitively) are pure code with no embedded secrets or business
# data (verified file by file) and now live in version control under
# scripts/gold/, referenced here by an explicit ROOT-derived path — not
# an implicit cwd=artifacts/v0 — per scripts/test_gold_chain_portability.py.
#
# One real gap remains, deliberately NOT resolved by this move:
# scripts/gold/_p5a_keymap.json (SKU/EAN/product-name/role mapping) is
# still required at runtime and still NOT committed — it's genuine
# product-catalog business data with no Neon database equivalent
# (unlike COGS $ values, which already live in core.dim_cogs and were
# confirmed dead weight for this call path, not read from any CSV here
# at all). See the P11-QUATER report for the full dependency
# classification. Until that's resolved, Gold scripts will fail
# cleanly on FileNotFoundError for that one file, not silently produce
# wrong numbers.
GOLD_DIR = ROOT / "scripts" / "gold"

# Gold-chain scripts, in required dependency order. Each is BASE_GOLD or
# PNL_ENRICHMENT ownership-guarded already (see _gold_ownership.py) —
# this list only sequences them, never edits their logic.
GOLD_CHAIN = [
    ("BASE_GOLD: orders/units/GMV/cancel/refund/ads/inventory/cogs", "_p6a_gold_build.py"),
    ("BASE_GOLD: hh_operational_packaging_cost", "_p8_5_operational_packaging.py"),
    ("PNL_ENRICHMENT: Shopee net_sales/gm1/cm1", "_p6c1_shopee_pnl_extend.py"),
    ("PNL_ENRICHMENT: TikTok net_sales/gm1/cm1 + tiktok_*_fee_actual", "_p6b3_pnl_extend.py"),
    ("BASE_GOLD+PNL: Shopee affiliate volume + commission (order-level precedence)", "_p6c4_affiliate_gold.py"),
    ("PNL_ENRICHMENT: Shopee cm2_known/cm2", "_p6c5_shopee_cm2.py"),
    ("PNL_ENRICHMENT: TikTok cm2_known/cm2", "_p6b4_tiktok_cm2.py"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_subprocess(label: str, args: list[str], cwd: Path, timeout: float) -> dict:
    """P11-TER.3/4 — timeout is always explicit per call site (no shared
    default) and containment is delegated to
    incr_common.run_contained_subprocess(), which runs the child in its
    own process group and terminates that whole group on timeout instead
    of leaking a grandchild worker process (see that function's
    docstring for the proven-live failure mode this replaces)."""
    started_at = now_iso()
    try:
        r, timed_out = ic.run_contained_subprocess(args, cwd, timeout)
    except Exception as e:  # noqa: BLE001
        return {
            "label": label, "started_at": started_at, "finished_at": now_iso(),
            "status": "EXCEPTION", "returncode": None, "stdout_tail": None, "stderr_tail": str(e),
        }
    finished_at = now_iso()
    if timed_out:
        return {
            "label": label, "started_at": started_at, "finished_at": finished_at,
            "status": "TIMEOUT", "returncode": r.returncode,
            "stdout_tail": (r.stdout or "")[-2000:],
            "stderr_tail": f"subprocess exceeded {timeout}s timeout, process group terminated",
        }
    status = "SUCCESS" if r.returncode == 0 else "FAILED"
    return {
        "label": label, "started_at": started_at, "finished_at": finished_at,
        "status": status, "returncode": r.returncode,
        "stdout_tail": r.stdout[-4000:], "stderr_tail": r.stderr[-2000:] if r.returncode != 0 else None,
    }


def finalize_orphaned_running_rows(cycle_started_at: str, reason: str) -> int:
    """P11-TER.5 — after this cycle's own ingestion/reconciliation
    subprocess is timeout-killed, any control.etl_run_log row still
    'running' that THIS cycle started (started_at >= cycle_started_at)
    can never be finalized by its own dead worker process anymore — we
    just proved that process tree is gone. Finalize using the
    established 'fail' status (never invents new vocabulary). Scoped
    strictly to started_at >= cycle_started_at so it can never touch the
    5 pre-existing historical orphan rows (stale since 2026-09-14,
    explicitly out of scope for this checkpoint) or any row belonging to
    a different, still-legitimately-running cycle."""
    conn = ic.get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """UPDATE control.etl_run_log
           SET status='fail', finished_at=now(), rows_processed=0,
               error_message=%s
           WHERE status='running' AND started_at >= %s
           RETURNING etl_run_id;""",
        (reason, cycle_started_at),
    )
    finalized = cur.fetchall()
    conn.close()
    return len(finalized)


def run_ingestion() -> dict:
    """API -> CORE. Per-domain isolated inside incremental.py itself."""
    result = run_subprocess(
        "incremental_ingestion", [PY, "pipelines/incremental.py"], ROOT, INGESTION_TIMEOUT_SECONDS,
    )
    per_domain = {}
    if result["stdout_tail"]:
        try:
            # incremental.py prints one JSON object with a result per "CHANNEL/domain" key.
            start = result["stdout_tail"].find("{")
            if start >= 0:
                per_domain = json.loads(result["stdout_tail"][start:])
        except Exception:  # noqa: BLE001
            per_domain = {}
    result["per_domain"] = per_domain
    return result


def run_gold_chain() -> list[dict]:
    steps = []
    for label, script in GOLD_CHAIN:
        steps.append(run_subprocess(
            label, [PY, str(GOLD_DIR / script)], GOLD_DIR, GOLD_SCRIPT_TIMEOUT_SECONDS,
        ))
    return steps


def run_reconciliation() -> dict:
    return run_subprocess(
        "reconciliation (D-1/D-3/D-7 re-check)", [PY, "pipelines/reconcile.py"], ROOT,
        RECONCILIATION_TIMEOUT_SECONDS,
    )


def run_source_coverage_snapshot() -> dict:
    """Read-only — mart.v_ai_source_coverage is always live, nothing to
    'update'; this just captures its state at cycle-end for the log."""
    import keyring
    import psycopg2

    # Phase 6C scheduler-migration — see D2: env var (GitHub Secret) takes
    # precedence over Keychain; unset on the Mac local/production path.
    url = os.environ.get("HH_NEONDB_OWNER_DATABASE_URL") or keyring.get_password("HH_ECOM_NEON", "neondb_owner_database_url")
    conn = psycopg2.connect(url)
    del url
    cur = conn.cursor()
    cur.execute(
        "SELECT platform, source_name, coverage_status, latest_db_date, last_error_message "
        "FROM mart.v_ai_source_coverage ORDER BY 1,2;"
    )
    rows = [
        {"platform": p, "source": s, "status": st, "latest_db_date": str(d) if d else None, "last_error": e}
        for p, s, st, d, e in cur.fetchall()
    ]
    conn.close()
    return {"label": "source_coverage_snapshot", "captured_at": now_iso(), "rows": rows}


def run_healthcheck() -> dict:
    r = subprocess.run([PY, "scripts/production_healthcheck.py"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    return {"label": "production_healthcheck", "exit_code": r.returncode, "stdout": r.stdout}


def main():
    try:
        acquire_single_instance_lock()
    except AlreadyRunningError as e:
        print(f"ALREADY_RUNNING: {e}")
        print("Exiting cleanly (0) - not an error, just declining to start a second concurrent cycle.")
        return 0

    cycle_started_at = now_iso()
    log: dict = {"cycle_started_at": cycle_started_at}

    print(f"=== PRODUCTION CYCLE START {cycle_started_at} ===")

    print("--- STEP 1: API -> CORE (incremental ingestion) ---")
    ingestion = run_ingestion()
    log["ingestion"] = ingestion
    print(f"ingestion status: {ingestion['status']}")
    if ingestion["status"] == "TIMEOUT":
        # P11-TER.5 — we just terminated incremental.py's whole process
        # group (see run_contained_subprocess), so any row it started
        # this cycle that's still 'running' is now provably orphaned.
        n = finalize_orphaned_running_rows(cycle_started_at, "ORCHESTRATOR_TIMEOUT")
        print(f"finalized {n} orphaned etl_run_log row(s) from this cycle's ingestion timeout")

    print("--- STEP 2: CORE -> GOLD (chain, dependency order) ---")
    gold_steps = run_gold_chain()
    log["gold_chain"] = gold_steps
    for s in gold_steps:
        print(f"  {s['label']}: {s['status']}")

    print("--- STEP 3: reconciliation ---")
    recon = run_reconciliation()
    log["reconciliation"] = recon
    print(f"reconciliation status: {recon['status']}")
    if recon["status"] == "TIMEOUT":
        n = finalize_orphaned_running_rows(cycle_started_at, "ORCHESTRATOR_TIMEOUT")
        print(f"finalized {n} orphaned etl_run_log row(s) from this cycle's reconciliation timeout")

    print("--- STEP 4: source coverage snapshot ---")
    try:
        coverage = run_source_coverage_snapshot()
        log["source_coverage"] = coverage
        print(f"source_coverage rows captured: {len(coverage['rows'])}")
    except Exception as e:  # noqa: BLE001
        log["source_coverage"] = {"label": "source_coverage_snapshot", "status": "EXCEPTION", "error": str(e)}
        print(f"source_coverage EXCEPTION: {e}")

    print("--- STEP 5: production health check ---")
    try:
        health = run_healthcheck()
        log["healthcheck"] = health
        print(health["stdout"][-1500:])
    except Exception as e:  # noqa: BLE001
        log["healthcheck"] = {"label": "production_healthcheck", "status": "EXCEPTION", "error": str(e)}
        print(f"healthcheck EXCEPTION: {e}")

    # Overall cycle verdict:
    # RED   = any internal Gold-chain script FAILED/TIMEOUT/EXCEPTION, or healthcheck overall RED
    # YELLOW = ingestion had per-domain failures that are known external issues (e.g. Shopee returns),
    #          or healthcheck overall YELLOW, but no internal Gold/reconciliation failure
    # GREEN  = everything succeeded
    gold_failed = any(s["status"] != "SUCCESS" for s in gold_steps)
    healthcheck_overall = "UNKNOWN"
    if isinstance(log.get("healthcheck"), dict) and "stdout" in log["healthcheck"]:
        for line in log["healthcheck"]["stdout"].splitlines():
            if line.startswith("OVERALL:"):
                healthcheck_overall = line.split(":", 1)[1].strip()

    if gold_failed or recon["status"] not in ("SUCCESS",) or healthcheck_overall == "RED":
        verdict = "RED"
    elif healthcheck_overall == "YELLOW":
        verdict = "YELLOW"
    elif healthcheck_overall == "GREEN":
        verdict = "GREEN"
    else:
        verdict = "YELLOW"  # unknown healthcheck output — be conservative, never claim GREEN blindly

    log["cycle_finished_at"] = now_iso()
    log["cycle_verdict"] = verdict
    log["gold_chain_all_success"] = not gold_failed
    log["healthcheck_overall"] = healthcheck_overall

    log_path = LOG_DIR / f"cycle_{cycle_started_at.replace(':', '-')}.json"
    log_path.write_text(json.dumps(log, indent=2, default=str))

    print(f"=== PRODUCTION CYCLE END — VERDICT: {verdict} ===")
    print(f"Log written to {log_path}")
    return 0 if verdict != "RED" else 1


if __name__ == "__main__":
    sys.exit(main())
