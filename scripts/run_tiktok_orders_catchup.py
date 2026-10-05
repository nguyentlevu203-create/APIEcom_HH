#!/usr/bin/env python3
"""
P11 RECOVERY B — dedicated, manual-only TikTok/orders catch-up.

Advances ONLY control.etl_sync_state(TIKTOK, orders). No Shopee, no other
TikTok domain, no GOLD, no reconciliation, no healthcheck.

Reuses the production incremental path unchanged: each pass runs
integrations/tiktok_shop/pilot_reporting/incr_worker.py `orders` exactly
as pipelines/incremental.py does (exact 4h chunks, one transaction per
chunk with the watermark advanced in that same transaction, 780s internal
soft deadline, NetworkError retry, bootstrap_session() token refresh ->
TIKTOK_TOKENS_JSON durable persistence from token_persistence.py). This
script only repeats bounded passes against one fixed requested_end until
the backlog is cleared, a pass fails, or the job budget runs out, and
checks after every pass that the DB watermark equals what the worker
says it committed.

Exit semantics (THIS entrypoint only — the production cycle still treats
PARTIAL_CATCHUP:TIKTOK/orders as RED):
  PASS             backlog cleared                                exit 0
  PARTIAL_CATCHUP  >=1 chunk committed, budget ran out, no error  exit 0
  FAIL             any pass failed (auth, token refresh,
                   TOKEN_STATE_PERSISTENCE_FAILED, DB, API, chunk
                   write, timeout, unparseable result), watermark
                   inconsistency, or zero progress with backlog     exit 1

Prints one `HH_TIKTOK_ORDERS_CATCHUP_ONLY_JSON=` marker (safe fields only,
never raw errors or credentials) and writes the same summary plus per-pass
and per-chunk progress to artifacts/v0/catchup_logs/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipelines"))
import incr_common as ic  # noqa: E402

TIKTOK_WORKER = ROOT / "integrations" / "tiktok_shop" / "pilot_reporting" / "incr_worker.py"
SHOP_INFO_PATH = ROOT / "integrations" / "tiktok_shop" / "shop_info.json"
LOG_DIR = ROOT / "artifacts" / "v0" / "catchup_logs"

RESULT_MARKER = "HH_TIKTOK_ORDERS_CATCHUP_ONLY_JSON="
CHUNK_MARKER = "HH_TIKTOK_ORDERS_CHUNK_JSON="
CATCHUP_MARKER = "HH_TIKTOK_ORDERS_CATCHUP_JSON="
RUN_TYPE = "tiktok_orders_catchup_only"

# Same external kill as pipelines/incremental.py TIKTOK_ORDERS_TIMEOUT_SECONDS
# (the worker's own 780s soft deadline stays below it).
PASS_TIMEOUT_SECONDS = 900
# Never start a pass that could outlive the job budget.
PASS_RESERVE_SECONDS = PASS_TIMEOUT_SECONDS + 60
DEFAULT_BUDGET_MINUTES = 90
MIN_BUDGET_MINUTES, MAX_BUDGET_MINUTES = 16, 180

STATUS_PASS = "PASS"
STATUS_PARTIAL = ic.PARTIAL_CATCHUP_STATUS
STATUS_FAIL = "FAIL"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def parse_worker_stdout(stdout: str) -> tuple[list[dict], Optional[dict], Optional[dict]]:
    """(committed chunk markers, catch-up meta marker, final worker JSON line)."""
    chunks, catchup, final = [], None, None
    for line in stdout.splitlines():
        try:
            if line.startswith(CHUNK_MARKER):
                chunks.append(json.loads(line[len(CHUNK_MARKER):]))
            elif line.startswith(CATCHUP_MARKER):
                catchup = json.loads(line[len(CATCHUP_MARKER):])
        except ValueError:
            continue
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            final = parsed
        break
    return chunks, catchup, final


def classify_pass_error(timed_out: bool, final: Optional[dict], catchup: Optional[dict]) -> str:
    error = str((final or {}).get("error") or "")
    if ic.TOKEN_STATE_PERSISTENCE_FAILED in error:
        return ic.TOKEN_STATE_PERSISTENCE_FAILED
    if timed_out:
        return "WORKER_TIMEOUT"
    if final is None:
        return "WORKER_RESULT_UNPARSEABLE"
    if catchup and catchup.get("error_class"):
        return str(catchup["error_class"])
    if "REAUTHORIZATION" in error or "refresh" in error.lower():
        return "TOKEN_REFRESH_FAILURE"
    return "WORKER_FAIL"


def run_catchup(
    *,
    requested_end: datetime,
    budget_seconds: float,
    read_watermark: Callable[[], Optional[datetime]],
    start_run: Callable[[], str],
    finish_run: Callable[[str, str, int, str], None],
    run_worker: Callable[[datetime, datetime, str], tuple[int, str, str, bool]],
    now_monotonic: Callable[[], float] = time.monotonic,
    echo: Callable[[str], None] = print,
) -> dict:
    """Pure orchestration with every side effect injected."""
    t0 = now_monotonic()
    summary = {
        "before_watermark": None, "requested_start": None, "requested_end": _iso(requested_end),
        "chunks_attempted": 0, "chunks_committed": 0, "last_committed_end": None,
        "after_watermark": None, "catchup_complete": False, "stopped_by_soft_deadline": False,
        "status": None, "error_class": None, "passes": 0, "elapsed_seconds": 0.0,
    }
    passes: list[dict] = []
    chunk_log: list[dict] = []

    def finish(status, error_class=None):
        summary["status"], summary["error_class"] = status, error_class
        summary["elapsed_seconds"] = round(now_monotonic() - t0, 3)
        summary["passes"] = len(passes)
        return {"summary": summary, "passes": passes, "chunks": chunk_log}

    try:
        before = read_watermark()
    except Exception:  # noqa: BLE001
        return finish(STATUS_FAIL, "DB_FAILURE")
    if before is None:
        return finish(STATUS_FAIL, "NO_WATERMARK")
    summary["before_watermark"] = summary["after_watermark"] = _iso(before)
    summary["requested_start"] = _iso(before - timedelta(seconds=ic.OVERLAP_SECONDS))
    if before >= requested_end:
        summary["catchup_complete"] = True
        return finish(STATUS_PASS)

    watermark = before
    while True:
        if watermark >= requested_end:
            summary["catchup_complete"] = True
            return finish(STATUS_PASS)
        if (now_monotonic() - t0) + PASS_RESERVE_SECONDS > budget_seconds:
            summary["stopped_by_soft_deadline"] = True
            if summary["chunks_committed"] > 0:
                return finish(STATUS_PARTIAL)
            return finish(STATUS_FAIL, "NO_PROGRESS")

        window_start = watermark - timedelta(seconds=ic.OVERLAP_SECONDS)
        pass_t0 = now_monotonic()
        try:
            etl_run_id = start_run()
        except Exception:  # noqa: BLE001
            return finish(STATUS_FAIL, "DB_FAILURE")
        returncode, stdout, stderr, timed_out = run_worker(window_start, requested_end, etl_run_id)
        echo(stdout.rstrip("\n"))
        if stderr:
            echo(stderr.rstrip("\n"))
        chunks, catchup, final = parse_worker_stdout(stdout or "")
        chunk_log.extend(chunks)
        committed = len(chunks)
        worker_status = (final or {}).get("status")
        summary["chunks_attempted"] += int((catchup or {}).get("chunks_attempted") or committed)
        summary["chunks_committed"] += committed

        try:
            after = read_watermark()
        except Exception:  # noqa: BLE001
            finish_run(etl_run_id, "fail", 0, "DB_FAILURE reading watermark after catch-up pass")
            return finish(STATUS_FAIL, "DB_FAILURE")
        summary["after_watermark"] = _iso(after)

        last_end = (catchup or {}).get("last_committed_end") or (chunks[-1]["chunk_end"] if chunks else None)
        if committed:
            summary["last_committed_end"] = last_end
        expected = datetime.fromisoformat(last_end) if committed else watermark
        pass_rec = {
            "etl_run_id": etl_run_id, "window_start": _iso(window_start), "window_end": _iso(requested_end),
            "worker_status": worker_status, "timed_out": timed_out, "chunks_committed": committed,
            "watermark_before": _iso(watermark), "watermark_after": _iso(after),
            "duration_seconds": round(now_monotonic() - pass_t0, 3), "error_class": None,
        }
        passes.append(pass_rec)

        if after is None or after != expected:
            pass_rec["error_class"] = "WATERMARK_INCONSISTENT"
            finish_run(etl_run_id, "fail", 0,
                       f"WATERMARK_INCONSISTENT: expected {_iso(expected)}, sync_state has {_iso(after)}")
            return finish(STATUS_FAIL, "WATERMARK_INCONSISTENT")

        rows = sum(int(c.get("orders_inserted", 0)) + int(c.get("orders_updated", 0))
                   + int(c.get("items_inserted", 0)) + int(c.get("items_updated", 0)) for c in chunks)

        if not timed_out and returncode == 0 and worker_status == STATUS_PASS:
            finish_run(etl_run_id, "success", rows, "")
            watermark = after
            continue
        if not timed_out and returncode == 0 and worker_status == STATUS_PARTIAL and committed > 0:
            finish_run(etl_run_id, "success", rows,
                       f"{STATUS_PARTIAL}: committed {committed} chunk(s) through {last_end}, backlog remains")
            watermark = after
            continue

        error_class = classify_pass_error(timed_out, final, catchup)
        pass_rec["error_class"] = error_class
        raw_error = str((final or {}).get("error") or (stderr or "")[-2000:] or f"worker exit code {returncode}")
        finish_run(etl_run_id, "fail", rows, f"{error_class}: {raw_error}")
        return finish(STATUS_FAIL, error_class)


# ---------------------------------------------------------------------
# Real side effects
# ---------------------------------------------------------------------

def _load_shop_id() -> str:
    if not SHOP_INFO_PATH.exists():
        raise RuntimeError("shop_info.json missing")
    shop_id = json.loads(SHOP_INFO_PATH.read_text(encoding="utf-8")).get("shop_id")
    if not shop_id:
        raise RuntimeError("shop_id missing from shop_info.json")
    return str(shop_id)


def _make_deps(shop_id: str):
    def read_watermark():
        conn = ic.get_db_conn()
        try:
            row = ic.get_sync_state(conn.cursor(), "TIKTOK", "orders", shop_id)
        finally:
            conn.close()
        return row["last_synced_at"] if row else None

    def start_run():
        conn = ic.get_db_conn()
        try:
            etl_run_id = ic.start_run_log(conn.cursor(), "TIKTOK", "orders", shop_id, run_type=RUN_TYPE)
            conn.commit()
        finally:
            conn.close()
        return etl_run_id

    def finish_run(etl_run_id, status, rows, note):
        ic.finish_run_log("TIKTOK", "orders", etl_run_id, status, rows, note)

    def run_worker(window_start, window_end, etl_run_id):
        proc, timed_out = ic.run_contained_subprocess(
            [sys.executable, str(TIKTOK_WORKER), "orders", window_start.isoformat(), window_end.isoformat(), etl_run_id],
            TIKTOK_WORKER.parent, PASS_TIMEOUT_SECONDS,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", timed_out

    return read_watermark, start_run, finish_run, run_worker


def write_log(result: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"tiktok_orders_catchup_{ic.now_utc().strftime('%Y-%m-%dT%H-%M-%SZ')}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--budget-minutes", type=int, default=DEFAULT_BUDGET_MINUTES)
    args = parser.parse_args(argv)
    if not MIN_BUDGET_MINUTES <= args.budget_minutes <= MAX_BUDGET_MINUTES:
        parser.error(f"--budget-minutes must be within {MIN_BUDGET_MINUTES}..{MAX_BUDGET_MINUTES}")

    requested_end = ic.now_utc()
    try:
        shop_id = _load_shop_id()
    except Exception:  # noqa: BLE001
        result = {"summary": {"status": STATUS_FAIL, "error_class": "NO_SHOP_ID",
                              "requested_end": _iso(requested_end)}, "passes": [], "chunks": []}
    else:
        read_watermark, start_run, finish_run, run_worker = _make_deps(shop_id)
        result = run_catchup(
            requested_end=requested_end, budget_seconds=args.budget_minutes * 60,
            read_watermark=read_watermark, start_run=start_run, finish_run=finish_run, run_worker=run_worker,
        )
    summary = result["summary"]
    write_log(result)
    print(f"{RESULT_MARKER}{json.dumps(summary, default=str)}", flush=True)
    return 0 if summary["status"] in (STATUS_PASS, STATUS_PARTIAL) else 1


if __name__ == "__main__":
    sys.exit(main())
