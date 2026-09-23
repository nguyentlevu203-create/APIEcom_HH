#!/usr/bin/env python3
"""
P4 — historical reconciliation for Shopee + TikTok, business dates
D-1 / D-3 / D-7 (computed at runtime from Asia/Ho_Chi_Minh "today", never
hard-coded).

    python3 pipelines/reconcile.py [--windows D1,D3,D7] [--only SHOPEE:orders,...]

Same coordinator/worker split as pipelines/incremental.py, for the same
reason (integrations/shopee/ and integrations/tiktok_shop/ each define a
bare `config.py` — cannot share one Python process). Each domain's actual
fetch/normalize/UPSERT logic is the SAME handler function P3 already
uses (run_orders, run_finance, run_returns, run_affiliate,
run_product_analytics, run_live, run_ads in the two incr_worker.py
files) — reconcile.py adds no new API-calling code, only:
  - runtime D-1/D-3/D-7 date computation (Section 6)
  - a before/after DB snapshot bracketing the same handler call
  - audit.reconciliation_result writes (Section 12)
  - explicitly SKIPPING control.etl_sync_state (Section 2 — P4 must
    never move the live P3 incremental watermark)

Datasets reconciled (Section 4):
  SHOPEE:  orders (+ items), finance, returns, ads, affiliate_ams (P8.4 -
           added to the sanctioned D-1/D-3/D-7 schedule; commissions can
           settle/change after order date, so this is the appropriate
           re-check cadence rather than a one-way incremental watermark)
  TIKTOK:  orders (+ items), finance, returns, affiliate,
           product_analytics, live
  TikTok Ads: SKIPPED, same as P3 (see run_tiktok_ads_skip() in
  incremental.py — not duplicated here).
  Product/SKU/Inventory: NOT reconciled — neither platform's API exposes
  a historical snapshot (Section 4 explicitly forbids treating current
  state as historical).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_common as ic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOPEE_WORKER = ROOT / "integrations" / "shopee" / "pilot_reporting" / "incr_worker.py"
TIKTOK_WORKER = ROOT / "integrations" / "tiktok_shop" / "pilot_reporting" / "incr_worker.py"
WORKER_BY_SYSTEM = {"SHOPEE": SHOPEE_WORKER, "TIKTOK": TIKTOK_WORKER}

SHOPEE_RECON_DOMAINS = ["orders", "finance", "returns", "ads", "affiliate_ams"]
TIKTOK_RECON_DOMAINS = ["orders", "finance", "returns", "affiliate", "product_analytics", "live"]


def today_hh() -> "datetime.date":
    """Section 6: TODAY_HH computed at runtime from Asia/Ho_Chi_Minh —
    never a hard-coded date."""
    return ic.vn_date(ic.now_utc())


def day_window_utc(business_date) -> tuple[datetime, datetime]:
    """[business_date 00:00:00, business_date+1 00:00:00) in Asia/Ho_Chi_Minh,
    returned as UTC-aware datetimes — the exclusive-upper-bound convention
    every range-filtered domain (orders/returns/finance/affiliate) already
    expects. Domains that need a single calendar date instead of a range
    (ads/product_analytics/live) receive business_date explicitly, not
    derived from window_end — see the `target_date` parameters added to
    incr_worker.py for P4."""
    start_local = datetime(business_date.year, business_date.month, business_date.day, 0, 0, 0, tzinfo=ic.VN_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def get_shop_ids() -> dict:
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'integrations/shopee'); "
         "from keychain import get_secret, ACCOUNT_SHOP_ID; print(get_secret(ACCOUNT_SHOP_ID) or '')"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    shopee_shop_id = out.stdout.strip() or None
    tiktok_shop_info_path = ROOT / "integrations" / "tiktok_shop" / "shop_info.json"
    tiktok_shop_id = None
    if tiktok_shop_info_path.exists():
        tiktok_shop_id = json.loads(tiktok_shop_info_path.read_text()).get("shop_id")
    return {"SHOPEE": shopee_shop_id, "TIKTOK": tiktok_shop_id}


RECON_DOMAIN_TIMING_MARKER = "HH_RECON_DOMAIN_TIMING_JSON="
RECON_WINDOW_START_MARKER = "HH_RECON_WINDOW_START_JSON="
RECON_WINDOW_END_MARKER = "HH_RECON_WINDOW_END_JSON="
RECON_RESULT_MARKER = "HH_RECONCILIATION_RESULT_JSON="


def _emit_domain_timing(window_label: str, business_date, source_system: str, domain: str,
                         result: dict, duration_seconds: float, timed_out: bool, sink: list | None = None) -> dict:
    """P11-RECON-OBS — one flushed marker line per domain, immediately
    after it finishes. This process can itself be killed by the outer
    orchestrator's 3600s stage timeout (proven live, 2026-09-23) before
    it ever reaches the final summary — flushing here, per domain, is
    what lets already-completed timing survive that kill instead of
    being lost with the rest of the process's buffered state. Reuses
    incr_common.classify_domain_error() for a safe, non-secret-bearing
    error class — never the raw exception/error text. `sink`, when
    given, also collects this same payload for main()'s final summary —
    never recomputed differently, always the exact thing that was
    flushed."""
    rows_processed = len(result.get("recon_rows") or []) if result.get("status") == "PASS" else 0
    payload = {
        "window": window_label,
        "business_date": business_date.isoformat(),
        "source_system": source_system,
        "domain": domain,
        "status": result.get("status"),
        "duration_seconds": round(duration_seconds, 3),
        "timed_out": timed_out,
        "rows_processed": rows_processed,
        "etl_run_id": result.get("etl_run_id"),
        "error_class": ic.classify_domain_error(result),
    }
    print(f"{RECON_DOMAIN_TIMING_MARKER}{json.dumps(payload, default=str)}", flush=True)
    if sink is not None:
        sink.append(payload)
    return payload


def run_reconcile_domain(window_label: str, source_system: str, domain: str, shop_id: str, business_date,
                          timings_sink: list | None = None) -> dict:
    t0 = time.monotonic()
    window_start, window_end = day_window_utc(business_date)

    conn = ic.get_db_conn()
    cur = conn.cursor()
    etl_run_id = ic.start_run_log(cur, source_system, domain, shop_id,
                                   run_type="reconciliation", business_date=business_date)
    conn.commit()
    conn.close()

    worker = WORKER_BY_SYSTEM[source_system]
    recon_timeout = 900
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, str(worker), domain, window_start.isoformat(), window_end.isoformat(),
         etl_run_id, "reconcile", business_date.isoformat()],
        worker.parent, recon_timeout,
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")

    if timed_out:
        # P11-TER.5 — proven live: this exact gap left a TIKTOK/finance
        # reconciliation row 'running' forever. A bare subprocess.run(
        # timeout=...) raised TimeoutExpired here uncaught, which crashed
        # this whole process mid-loop (exit code 1, no traceback captured
        # by the orchestrator) — every domain still queued after the one
        # that hit this timeout silently never ran, with no record of
        # why. run_contained_subprocess() already terminated the
        # worker's full process group, so this domain is now a normal,
        # isolated FAIL and the loop continues to the next domain.
        error = f"DOMAIN_WORKER_TIMEOUT: worker exceeded {recon_timeout}s, process group terminated"
        ic.finish_run_log(source_system, domain, etl_run_id, "fail", 0, error)
        result = {
            "status": "FAIL", "business_date": business_date.isoformat(),
            "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
            "etl_run_id": etl_run_id, "error": error,
        }
        _emit_domain_timing(window_label, business_date, source_system, domain, result,
                             time.monotonic() - t0, timed_out=True, sink=timings_sink)
        return result

    worker_result = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            worker_result = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue

    if proc.returncode == 0 and worker_result and worker_result.get("status") == "PASS":
        rows_processed = len(worker_result.get("recon_rows") or [])
        ic.finish_run_log(source_system, domain, etl_run_id, "success", rows_processed)
        result = {
            "status": "PASS", "business_date": business_date.isoformat(),
            "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
            "etl_run_id": etl_run_id, "recon_rows": worker_result.get("recon_rows"),
            "worker_result": worker_result.get("result"),
        }
        _emit_domain_timing(window_label, business_date, source_system, domain, result,
                             time.monotonic() - t0, timed_out=False, sink=timings_sink)
        return result

    error = (worker_result or {}).get("error") or proc.stderr[-2000:] or f"worker exit code {proc.returncode}"
    ic.finish_run_log(source_system, domain, etl_run_id, "fail", 0, error)
    result = {
        "status": "FAIL", "business_date": business_date.isoformat(),
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "etl_run_id": etl_run_id, "error": error,
    }
    _emit_domain_timing(window_label, business_date, source_system, domain, result,
                         time.monotonic() - t0, timed_out=False, sink=timings_sink)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="D1,D3,D7", help="Comma list of D1,D3,D7 to run (default all three)")
    ap.add_argument("--only", default="", help="Comma-separated CHANNEL:domain filter")
    ap.add_argument("--dates", default="",
                     help="P8.4 Section C — comma-separated explicit YYYY-MM-DD business dates to "
                          "reconcile instead of the D1/D3/D7 relative offsets (e.g. for backfilling "
                          "specific historical gaps). Reuses the exact same run_reconcile_domain() "
                          "path — no new fetch/UPSERT logic.")
    args = ap.parse_args()

    only = set()
    if args.only:
        for item in args.only.split(","):
            ch, dom = item.split(":")
            only.add((ch.strip().upper(), dom.strip()))

    today = today_hh()
    if args.dates:
        dates = {d: datetime.strptime(d, "%Y-%m-%d").date() for d in args.dates.split(",")}
    else:
        offsets = {"D1": 1, "D3": 3, "D7": 7}
        windows_requested = [w.strip().upper() for w in args.windows.split(",")]
        dates = {w: today - timedelta(days=offsets[w]) for w in windows_requested if w in offsets}

    shop_ids = get_shop_ids()
    results: dict = {"TODAY_HH": today.isoformat(), "dates": {k: v.isoformat() for k, v in dates.items()}}

    overall_t0 = time.monotonic()
    all_domain_timings: list[dict] = []
    window_timings: list[dict] = []

    for window_label, business_date in dates.items():
        results[window_label] = {}
        window_t0 = time.monotonic()
        print(f"{RECON_WINDOW_START_MARKER}"
              f"{json.dumps({'window': window_label, 'business_date': business_date.isoformat()}, default=str)}",
              flush=True)
        completed_count = 0
        failed_count = 0
        for source_system, domains in (("SHOPEE", SHOPEE_RECON_DOMAINS), ("TIKTOK", TIKTOK_RECON_DOMAINS)):
            shop_id = shop_ids.get(source_system)
            if not shop_id:
                results[window_label][source_system] = {"status": "FAIL", "error": f"no shop_id for {source_system}"}
                failed_count += 1
                continue
            for domain in domains:
                if only and (source_system, domain) not in only:
                    continue
                key = f"{source_system}/{domain}"
                domain_result = run_reconcile_domain(
                    window_label, source_system, domain, shop_id, business_date, timings_sink=all_domain_timings,
                )
                results[window_label][key] = domain_result
                if domain_result.get("status") == "PASS":
                    completed_count += 1
                else:
                    failed_count += 1

        window_end_payload = {
            "window": window_label, "business_date": business_date.isoformat(),
            "elapsed_seconds": round(time.monotonic() - window_t0, 3),
            "completed_domain_count": completed_count, "failed_domain_count": failed_count,
        }
        print(f"{RECON_WINDOW_END_MARKER}{json.dumps(window_end_payload, default=str)}", flush=True)
        window_timings.append(window_end_payload)

    print(json.dumps(results, indent=2, default=str))

    # P11-RECON-OBS Q4 — bounded, structured summary; never duplicates
    # full worker stdout. domain_timings here are the exact same
    # payloads _emit_domain_timing() already flushed per domain via
    # timings_sink, not recomputed differently.
    total_success = sum(1 for t in all_domain_timings if t["status"] == "PASS")
    slowest = sorted(all_domain_timings, key=lambda t: t["duration_seconds"], reverse=True)[:5]
    summary = {
        "total_elapsed_seconds": round(time.monotonic() - overall_t0, 3),
        "total_domains_attempted": len(all_domain_timings),
        "total_domains_success": total_success,
        "total_domains_failed": len(all_domain_timings) - total_success,
        "window_timings": window_timings,
        "domain_timings": all_domain_timings,
        "slowest_domains": slowest,
    }
    print(f"{RECON_RESULT_MARKER}{json.dumps(summary, default=str)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
