#!/usr/bin/env python3
"""
P3 — single incremental-ingestion entrypoint for Shopee + TikTok.

    python3 pipelines/incremental.py [--force] [--only SHOPEE:orders,TIKTOK:orders,...]

This coordinator process does NOT import shopee_client or tiktok_client
directly — integrations/shopee/ and integrations/tiktok_shop/ each
define a bare `config.py` / `keychain.py`, which collide if both are on
sys.path in the same interpreter. Instead, for each due domain, this
process (a) reads/writes control.etl_sync_state and control.etl_run_log
directly via psycopg2 (no platform-specific import needed for that),
then (b) invokes the correct platform's incr_worker.py as a SEPARATE
subprocess (each with only its own platform directory on sys.path,
exactly like every previous P2A/P2B/P2D script), which does the actual
fetch + normalize + UPSERT + advances sync_state itself, atomically, in
its own DB transaction — then (c) this process just logs the run
outcome and aggregates the JSON result the worker printed to stdout.

Workers:
  integrations/shopee/pilot_reporting/incr_worker.py
  integrations/tiktok_shop/pilot_reporting/incr_worker.py

Per-domain state machine (Section 8), split across coordinator (1-3, 11)
and worker (4-10):
  1. is this domain due (cadence) or --force?
  2. read control.etl_sync_state for (source_system, domain, shop_id)
  3. compute incremental window (previous boundary - 5min overlap;
     bootstrap to a 24h lookback if no prior state — Section 3)
  4. worker: refresh token securely if necessary (delegated to
     bootstrap_session() / shopee_client.py's own retry-on-401 — not
     reimplemented here)
  5. worker: call API (bounded exponential backoff on
     429/500/502/503/504/timeout — Section 9)
  6. worker: paginate completely
  7. worker: validate response
  8. worker: normalize
  9. worker: UPSERT
  10. worker: commit, and update control.etl_sync_state in the SAME
      transaction (so a commit failure can never leave sync state
      advanced without the data it claims to cover)
  11. coordinator: log run (control.etl_run_log; the worker only does
      the data-transaction, the coordinator owns the running/success/
      fail run-log row so a worker crash before it can print JSON is
      still recorded as FAIL, not silently lost)

On failure: worker rolls back its own transaction and does not advance
sync_state (still holds the OLD value) — the domain stays due next
cycle. The coordinator marks control.etl_run_log FAIL and moves on to
the next domain; one domain failing never blocks another.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incr_common as ic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOPEE_WORKER = ROOT / "integrations" / "shopee" / "pilot_reporting" / "incr_worker.py"
TIKTOK_WORKER = ROOT / "integrations" / "tiktok_shop" / "pilot_reporting" / "incr_worker.py"

SHOPEE_ONLY_DOMAINS = {"orders", "returns", "finance", "ads", "product_inventory", "affiliate_ams", "account_health"}
TIKTOK_ONLY_DOMAINS = {"orders", "returns", "finance", "affiliate", "product_analytics", "live", "product_inventory", "shop_traffic"}

WORKER_BY_SYSTEM = {"SHOPEE": SHOPEE_WORKER, "TIKTOK": TIKTOK_WORKER}

# P8.4 Section A — finance backfills after a multi-day gap can involve
# hundreds of per-order settlement calls and legitimately exceed the
# default worker timeout; every other domain keeps the original 600s.
DEFAULT_WORKER_TIMEOUT_SECONDS = 600
FINANCE_TIMEOUT_SECONDS = int(os.environ.get("FINANCE_TIMEOUT_SECONDS", "3600"))
DOMAIN_TIMEOUT_SECONDS = {"finance": FINANCE_TIMEOUT_SECONDS}


def get_shop_ids() -> dict:
    """Reads shop identifiers without importing either platform's client
    module — Shopee's via Keychain directly (same service/key
    convention as integrations/shopee/keychain.py), TikTok's from its
    already-written, non-secret shop_info.json."""
    # Read via keychain.py's own ACCOUNT_SHOP_ID / get_secret in a tiny
    # subprocess rather than importing shopee's keychain.py into this
    # process (which would pull in shopee's config.py and collide with
    # tiktok_shop's config.py the moment TikTok's worker path is also
    # touched in-process).
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


def run_domain(source_system: str, domain: str, shop_id: str, force: bool) -> dict:
    conn = ic.get_db_conn()
    cur = conn.cursor()
    sync_row = ic.get_sync_state(cur, source_system, domain, shop_id)
    cadence = ic.CADENCE_MINUTES[(source_system, domain)]
    now = ic.now_utc()

    if not force and not ic.is_due(sync_row, cadence, now):
        conn.close()
        return {
            "status": "NOT_DUE", "cadence_minutes": cadence,
            "last_synced_at": str(sync_row["last_synced_at"]) if sync_row else None,
        }

    window_start, window_note = ic.compute_window(sync_row, now)
    etl_run_id = ic.start_run_log(cur, source_system, domain, shop_id)
    conn.commit()
    conn.close()

    worker = WORKER_BY_SYSTEM[source_system]
    worker_timeout = DOMAIN_TIMEOUT_SECONDS.get(domain, DEFAULT_WORKER_TIMEOUT_SECONDS)
    proc, timed_out = ic.run_contained_subprocess(
        [sys.executable, str(worker), domain, window_start.isoformat(), now.isoformat(), etl_run_id],
        worker.parent, worker_timeout,
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")

    if timed_out:
        # P11-TER.4/5 — a plain subprocess.run(timeout=...) here would
        # raise TimeoutExpired uncaught, crashing this whole process mid
        # loop: the in-flight domain's run-log row would stay 'running'
        # forever AND every domain still queued after it would silently
        # never run. run_contained_subprocess() already terminated the
        # worker's full process group, so the domain is a normal FAIL.
        error = f"DOMAIN_WORKER_TIMEOUT: worker exceeded {worker_timeout}s, process group terminated"
        ic.finish_run_log(source_system, domain, etl_run_id, "fail", 0, error)
        return {
            "status": "FAIL", "window_start": window_start.isoformat(), "window_note": window_note,
            "cadence_minutes": cadence, "etl_run_id": etl_run_id, "error": error,
        }

    worker_result = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            worker_result = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue

    if proc.returncode == 0 and worker_result and worker_result.get("status") == "PASS":
        rows_processed = _sum_received(worker_result.get("result", {}))
        ic.finish_run_log(source_system, domain, etl_run_id, "success", rows_processed)
        return {
            "status": "PASS", "window_start": window_start.isoformat(), "window_note": window_note,
            "cadence_minutes": cadence, "etl_run_id": etl_run_id, "result": worker_result.get("result"),
        }

    error = (worker_result or {}).get("error") or proc.stderr[-2000:] or f"worker exit code {proc.returncode}"
    ic.finish_run_log(source_system, domain, etl_run_id, "fail", 0, error)
    return {
        "status": "FAIL", "window_start": window_start.isoformat(), "window_note": window_note,
        "cadence_minutes": cadence, "etl_run_id": etl_run_id, "error": error,
    }


def _sum_received(result: dict) -> int:
    total = 0
    for v in result.values():
        if isinstance(v, dict) and "received" in v:
            total += v["received"]
    return total


def run_tiktok_ads_skip() -> dict:
    ic.simple_log("TIKTOK/ads")("SKIP - NO_PERMISSION / SEPARATE_ADS_API (no Marketing scope). No API call made.")
    return {"status": "NO_PERMISSION / SEPARATE_ADS_API", "rows": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Bypass cadence due-gating (testing only)")
    ap.add_argument("--only", default="", help="Comma-separated CHANNEL:domain filter, e.g. SHOPEE:orders,TIKTOK:orders")
    args = ap.parse_args()

    only = set()
    if args.only:
        for item in args.only.split(","):
            ch, dom = item.split(":")
            only.add((ch.strip().upper(), dom.strip()))

    shop_ids = get_shop_ids()
    results = {}

    all_domains = (
        [("SHOPEE", d) for d in SHOPEE_ONLY_DOMAINS] + [("TIKTOK", d) for d in TIKTOK_ONLY_DOMAINS]
    )
    for source_system, domain in all_domains:
        if only and (source_system, domain) not in only:
            continue
        shop_id = shop_ids.get(source_system)
        if not shop_id:
            results[f"{source_system}/{domain}"] = {"status": "FAIL", "error": f"no shop_id for {source_system}"}
            continue
        results[f"{source_system}/{domain}"] = run_domain(source_system, domain, shop_id, args.force)

    if not only or ("TIKTOK", "ads") in only:
        results["TIKTOK/ads"] = run_tiktok_ads_skip()

    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
