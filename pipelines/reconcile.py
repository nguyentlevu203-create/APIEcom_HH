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


def run_reconcile_domain(source_system: str, domain: str, shop_id: str, business_date) -> dict:
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
        return {
            "status": "FAIL", "business_date": business_date.isoformat(),
            "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
            "etl_run_id": etl_run_id, "error": error,
        }

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
        return {
            "status": "PASS", "business_date": business_date.isoformat(),
            "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
            "etl_run_id": etl_run_id, "recon_rows": worker_result.get("recon_rows"),
            "worker_result": worker_result.get("result"),
        }

    error = (worker_result or {}).get("error") or proc.stderr[-2000:] or f"worker exit code {proc.returncode}"
    ic.finish_run_log(source_system, domain, etl_run_id, "fail", 0, error)
    return {
        "status": "FAIL", "business_date": business_date.isoformat(),
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "etl_run_id": etl_run_id, "error": error,
    }


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

    for window_label, business_date in dates.items():
        results[window_label] = {}
        for source_system, domains in (("SHOPEE", SHOPEE_RECON_DOMAINS), ("TIKTOK", TIKTOK_RECON_DOMAINS)):
            shop_id = shop_ids.get(source_system)
            if not shop_id:
                results[window_label][source_system] = {"status": "FAIL", "error": f"no shop_id for {source_system}"}
                continue
            for domain in domains:
                if only and (source_system, domain) not in only:
                    continue
                key = f"{source_system}/{domain}"
                results[window_label][key] = run_reconcile_domain(source_system, domain, shop_id, business_date)

    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
