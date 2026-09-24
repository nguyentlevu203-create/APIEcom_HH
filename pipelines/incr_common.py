#!/usr/bin/env python3
"""
Platform-agnostic helpers shared by the incremental coordinator
(pipelines/incremental.py) and both per-platform workers
(integrations/shopee/pilot_reporting/incr_worker.py,
integrations/tiktok_shop/pilot_reporting/incr_worker.py).

Deliberately imports NOTHING from either integrations/shopee/ or
integrations/tiktok_shop/ — both of those directories define a bare
`config.py` / `keychain.py` module, so importing both platforms' client
code into one process causes a sys.path module-name collision (the
second platform's `config` shadows the first's). Each worker runs as
its own subprocess with only its own platform directory on sys.path —
this module only needs psycopg2/keyring/datetime, which never collide.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import keyring
import psycopg2

VN_TZ = timezone(timedelta(hours=7))
OVERLAP_SECONDS = 5 * 60
BOOTSTRAP_LOOKBACK_SECONDS = 24 * 60 * 60  # first-ever run per domain: last 24h, not full history
TRANSIENT_HTTP = (429, 500, 502, 503, 504)
BACKOFF_DELAYS = (5, 15, 45)

CADENCE_MINUTES = {
    ("SHOPEE", "orders"): 30,
    ("SHOPEE", "returns"): 30,
    ("SHOPEE", "product_inventory"): 30,   # combined domain — see incremental.py docstring / P3 report §7
    ("SHOPEE", "finance"): 120,
    ("SHOPEE", "ads"): 60,
    ("SHOPEE", "affiliate_ams"): 120,  # P8.4 Section D — commissions settle/change after order date
    ("SHOPEE", "account_health"): 720,  # Phase 6A — shop-level snapshot, no per-order granularity; 2x/day is ample
    ("TIKTOK", "orders"): 30,
    ("TIKTOK", "returns"): 30,
    ("TIKTOK", "product_inventory"): 30,
    ("TIKTOK", "finance"): 120,
    ("TIKTOK", "affiliate"): 120,
    ("TIKTOK", "product_analytics"): 60,
    ("TIKTOK", "live"): 60,
    ("TIKTOK", "shop_traffic"): 60,
}


def get_db_conn():
    # Phase 6C scheduler-migration — GitHub Actions runners have no macOS
    # Keychain, so an explicit secret-backed env var takes precedence
    # when present (set from a GitHub Secret in the workflow; see
    # .github/workflows/p3-incremental.yml). Unset on the Mac local/
    # production path, so behavior there is completely unchanged.
    url = os.environ.get("HH_ETL_WRITER_DATABASE_URL")
    if not url:
        # Phase 6B rehearsal only: HH_ECOM_DB_URL_OVERRIDE redirects every
        # write in this process (including finish_run_log's own connection)
        # to an isolated Neon branch. Unset in the normal/production path,
        # so default behavior is unchanged — this is the only mechanism by
        # which rehearsal code is permitted to run without ever touching
        # the production database.
        url = os.environ.get("HH_ECOM_DB_URL_OVERRIDE") or keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    if not url:
        raise RuntimeError("hh_etl_writer_database_url missing from Keychain")
    conn = psycopg2.connect(
        url, keepalives=1, keepalives_idle=20, keepalives_interval=10, keepalives_count=3,
    )
    del url
    conn.autocommit = False
    return conn


def reconnect(old_conn):
    try:
        old_conn.close()
    except Exception:  # noqa: BLE001
        pass
    new_conn = get_db_conn()
    return new_conn, new_conn.cursor()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def vn_date(dt: datetime):
    return dt.astimezone(VN_TZ).date()


def ts_from_epoch(epoch) -> Optional[datetime]:
    if epoch in (None, "", "None"):
        return None
    try:
        epoch = int(epoch)
    except (TypeError, ValueError):
        return None
    if epoch == 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def dec(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


class Counters:
    def __init__(self) -> None:
        self.received = 0
        self.inserted = 0
        self.updated = 0

    def record(self, was_inserted: bool) -> None:
        self.received += 1
        if was_inserted:
            self.inserted += 1
        else:
            self.updated += 1

    def as_dict(self) -> dict:
        return {
            "received": self.received, "inserted": self.inserted,
            "updated": self.updated, "unchanged": 0,
            # ON CONFLICT DO UPDATE always executes on a matching key —
            # value-identical "unchanged" rows are not distinguishable
            # from "updated" by this mechanism (same documented
            # limitation as the P2A/P2B/P2D reports). Reported as 0, not
            # fabricated as a real measured count.
        }


MAX_RETRY_AFTER_SECONDS = 120  # cap a server-supplied Retry-After so one bad header can't stall a run


class TransientHTTPError(RuntimeError):
    """retry_after: seconds from the server's Retry-After header (429/503),
    when the caller was able to read it. None means 'no header, use the
    default backoff table' — every existing raise site keeps working
    unchanged."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def with_backoff(fn, label: str, log):
    """Bounded backoff for transient failures only (Section 9) — a real
    error is raised immediately, never retried forever. Default delays
    are 5s/15s/45s; a 429/503 that carries a Retry-After header overrides
    the next delay with that value (capped at MAX_RETRY_AFTER_SECONDS,
    still bounded to len(BACKOFF_DELAYS) total retries — no infinite
    loop)."""
    import requests
    last_exc = None
    for attempt, default_delay in enumerate((0,) + BACKOFF_DELAYS, start=1):
        if default_delay:
            retry_after = getattr(last_exc, "retry_after", None)
            if retry_after is not None:
                delay = min(max(retry_after, 0), MAX_RETRY_AFTER_SECONDS)
                log(f"{label}: transient failure, retry {attempt-1}/{len(BACKOFF_DELAYS)} "
                    f"after {delay}s (server Retry-After)")
            else:
                delay = default_delay
                log(f"{label}: transient failure, retry {attempt-1}/{len(BACKOFF_DELAYS)} after {delay}s")
            time.sleep(delay)
        try:
            return fn()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            continue
        except TransientHTTPError as e:
            last_exc = e
            continue
    raise last_exc  # noqa: RSE102


PROCESS_GROUP_KILL_GRACE_SECONDS = 10


def run_contained_subprocess(args: list[str], cwd, timeout: float):
    """P11-TER.4 — run a subprocess (and anything it spawns) in its own
    POSIX process group/session so a timeout can be contained cleanly.

    Plain subprocess.run(timeout=...) only signals the direct child;
    if that child has itself spawned a worker subprocess (as both
    pipelines/incremental.py and pipelines/reconcile.py do, one level
    down, per due domain), the grandchild is orphaned and keeps running
    unsupervised — proven live: a killed cycle left an in-flight TikTok
    finance worker running with no caller left to record its outcome.

    On timeout: SIGTERM the whole group, give it
    PROCESS_GROUP_KILL_GRACE_SECONDS to exit, then SIGKILL if still
    alive. Never raises subprocess.TimeoutExpired — callers must check
    the returned `timed_out` flag instead, so a timeout is always an
    explicit, handled outcome rather than an uncaught exception that
    would crash the caller and skip every subsequent domain.

    Returns (CompletedProcess-like with .returncode/.stdout/.stderr,
    timed_out: bool).
    """
    proc = subprocess.Popen(
        args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr), False
    except subprocess.TimeoutExpired:
        pass

    def _signal_group(sig):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            pass  # already exited between the timeout firing and the signal

    _signal_group(signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=PROCESS_GROUP_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(signal.SIGKILL)
        try:
            stdout, stderr = proc.communicate(timeout=PROCESS_GROUP_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr), True


def simple_log(prefix: str):
    def _log(msg: str) -> None:
        print(f"{now_utc().isoformat()} [{prefix}] {msg}")
    return _log


# ---------------------------------------------------------------------
# P11-SEXTUS — domain-status/verdict semantics, shared by
# pipelines/incremental.py (which produces per-domain results) and
# scripts/run_production_cycle.py (which must not let a due domain's
# real failure disappear behind a green subprocess exit code).
# ---------------------------------------------------------------------

# Only the status strings run_domain()/run_tiktok_ads_skip() (and the
# "no shop_id" branch in incremental.py's main()) actually produce as of
# this checkpoint. Deliberately NOT including a "NO_DATA_DUE" status —
# no current code path emits it. Anything not in this set — including a
# status this code has never produced before — is treated as a domain
# failure: fail-closed, an allowlist rather than a denylist.
REQUIRED_OK_STATUSES = {"PASS", "NOT_DUE", "NO_PERMISSION / SEPARATE_ADS_API", "SOURCE_NOT_READY"}

# P11-FIX-4 — the source itself reports that nothing in the requested
# range is published yet (currently only Shopee AMS, whose reports lag
# ~1 day). Nothing written, watermark unchanged, retried next cycle.
# OK for the cycle verdict: prolonged lag is caught by
# mart.v_ai_source_coverage (SOURCE_LAGGING, then STALE -> RED for a
# required source), not by failing every early-morning cycle.
SOURCE_NOT_READY_STATUS = "SOURCE_NOT_READY"

# P11-LAST-MILE — a required domain that made durable, committed
# progress but stopped at its internal soft deadline with backlog still
# remaining (currently only TIKTOK/orders' per-chunk catch-up). Not data
# loss and not a hard failure — but also NOT "fully caught up", so it is
# deliberately NOT in REQUIRED_OK_STATUSES: the cycle must never report
# it as a healthy required-domain sync. Reported separately from real
# failures so the cycle reason says which one it was.
PARTIAL_CATCHUP_STATUS = "PARTIAL_CATCHUP"


def is_domain_ok(status: Optional[str]) -> bool:
    return status in REQUIRED_OK_STATUSES


def classify_domain_error(domain_result: dict) -> Optional[str]:
    """Best-effort error_class for observability/logging only. Never
    feeds back into the PASS/FAIL decision — that's is_domain_ok() alone,
    based on the actual "status" field the domain result already
    carries. Returns None for a domain that isn't a failure at all."""
    if is_domain_ok(domain_result.get("status")):
        return None
    if domain_result.get("status") == PARTIAL_CATCHUP_STATUS:
        return PARTIAL_CATCHUP_STATUS
    error = str(domain_result.get("error") or "")
    if "DOMAIN_WORKER_TIMEOUT" in error:
        return "DOMAIN_WORKER_TIMEOUT"
    if "429" in error:
        return "SOURCE_RATE_LIMITED"
    return "SOURCE_FAILURE"


def compute_ingestion_verdict(results: dict) -> dict:
    """Pure aggregation over incremental.py's already-built per-domain
    results dict (built by main()'s existing per-domain loop, which is
    completely untouched and keeps running every domain regardless of
    an earlier one's outcome). This only decides, once every domain has
    already had its turn, whether the process as a whole should report
    success or failure."""
    failed_domains = sorted(
        domain for domain, result in results.items()
        if not is_domain_ok(result.get("status")) and result.get("status") != PARTIAL_CATCHUP_STATUS
    )
    partial_catchup_domains = sorted(
        domain for domain, result in results.items()
        if result.get("status") == PARTIAL_CATCHUP_STATUS
    )
    return {
        "process_status": "FAIL" if (failed_domains or partial_catchup_domains) else "PASS",
        "failed_domains": failed_domains,
        "partial_catchup_domains": partial_catchup_domains,
    }


# ---------------------------------------------------------------------
# Sync-state helpers
# ---------------------------------------------------------------------

def get_sync_state(cur, source_system: str, domain: str, shop_id: str) -> Optional[dict]:
    cur.execute(
        """SELECT last_synced_at, last_business_date, status FROM control.etl_sync_state
           WHERE source_system=%s AND source_endpoint=%s AND source_shop_id=%s;""",
        (source_system, domain, shop_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"last_synced_at": row[0], "last_business_date": row[1], "status": row[2]}


def compute_window(sync_row: Optional[dict], end: datetime) -> tuple[datetime, str]:
    if sync_row and sync_row["last_synced_at"]:
        start = sync_row["last_synced_at"] - timedelta(seconds=OVERLAP_SECONDS)
        return start, "incremental (prior boundary - 5min overlap)"
    start = end - timedelta(seconds=BOOTSTRAP_LOOKBACK_SECONDS)
    return start, "bootstrap (no prior sync_state - 24h lookback, not full history)"


def is_due(sync_row: Optional[dict], cadence_minutes: int, now: datetime) -> bool:
    if not sync_row or not sync_row["last_synced_at"]:
        return True
    return (now - sync_row["last_synced_at"]) >= timedelta(minutes=cadence_minutes)


def upsert_sync_state(cur, source_system: str, domain: str, shop_id: str,
                       last_synced_at: datetime, last_business_date, status: str) -> None:
    cur.execute(
        """
        INSERT INTO control.etl_sync_state
            (source_system, source_endpoint, source_shop_id, last_synced_at,
             last_business_date, status, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (source_system, source_endpoint, source_shop_id) DO UPDATE SET
            last_synced_at = EXCLUDED.last_synced_at,
            last_business_date = EXCLUDED.last_business_date,
            status = EXCLUDED.status,
            updated_at = now();
        """,
        (source_system, domain, shop_id, last_synced_at, last_business_date, status),
    )


def start_run_log(cur, source_system: str, domain: str, shop_id: str,
                   run_type: str = "incremental", business_date=None) -> str:
    etl_run_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO control.etl_run_log
               (etl_run_id, source_system, source_endpoint, source_shop_id, run_type, business_date, status)
           VALUES (%s,%s,%s,%s,%s,%s,'running');""",
        (etl_run_id, source_system, domain, shop_id, run_type, business_date),
    )
    return etl_run_id


def finish_run_log(source_system: str, domain: str, etl_run_id: str, status: str,
                    rows_processed: int = 0, error_message: str = "") -> None:
    conn = get_db_conn()
    conn.autocommit = True
    conn.cursor().execute(
        """UPDATE control.etl_run_log
           SET status=%s, finished_at=now(), rows_processed=%s, error_message=NULLIF(%s,'')
           WHERE etl_run_id=%s;""",
        (status, rows_processed, error_message[:2000], etl_run_id),
    )
    conn.close()


# ---------------------------------------------------------------------
# P4 — reconciliation helpers
# ---------------------------------------------------------------------

def _num(v):
    if v is None:
        return None
    return float(v)


def write_recon_result(cur, business_date, recon_type: str, channel: str, shop_id: str,
                        expected_value, actual_value, status: str, details: dict) -> None:
    """UPSERT one audit.reconciliation_result row (unique key: business_date,
    recon_type, channel, shop_id — matches the P1 table's own constraint,
    so a second P4 run over the same window updates in place rather than
    accumulating duplicate audit rows; control.etl_run_log is the place a
    new row per execution is expected, per Section 14).

    expected_value = the value now confirmed by the API — which equals
    the DB value immediately AFTER this run's UPSERT, since the UPSERT
    writes exactly what the API returned (no separate "pre-upsert API
    snapshot" is needed; reading the just-written row back is the same
    number, not an approximation).
    actual_value = the DB value as it stood BEFORE this run touched it —
    the prior P2/P3/P4 state.
    variance = expected - actual: the size of the drift this pass found
    and corrected.
    """
    ev, av = _num(expected_value), _num(actual_value)
    variance = (ev - av) if (ev is not None and av is not None) else None
    cur.execute(
        """
        INSERT INTO audit.reconciliation_result
            (business_date, recon_type, channel, shop_id, expected_value, actual_value,
             variance, status, details, checked_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (business_date, recon_type, channel, shop_id) DO UPDATE SET
            expected_value = EXCLUDED.expected_value,
            actual_value = EXCLUDED.actual_value,
            variance = EXCLUDED.variance,
            status = EXCLUDED.status,
            details = EXCLUDED.details,
            checked_at = now();
        """,
        (business_date, recon_type, channel, shop_id, ev, av, variance, status,
         json.dumps(details, default=str)),
    )


def classify_diff(before, after, is_new_date: bool) -> str:
    """Section 7 classification. Only ever returns NO_CHANGE,
    DB_MISSING_ROW, or EXPECTED_LATE_SOURCE_UPDATE from a numeric diff
    alone — the ETL-error classes (MAPPING_ERROR / TRANSFORMATION_ERROR /
    SOURCE_API_ERROR / UNEXPLAINED_DIFFERENCE) are never inferred here;
    a caller assigns those explicitly only when it has independent
    evidence (e.g. the domain's own API call failed this run) via
    `override_status` on recon_row()."""
    b = 0 if before in (None, "") else before
    a = 0 if after in (None, "") else after
    if a == b:
        return "NO_CHANGE"
    return "DB_MISSING_ROW" if is_new_date else "EXPECTED_LATE_SOURCE_UPDATE"


def classify_from_counts(inserted: int, updated: int) -> Optional[str]:
    """Precise, row-level classification derived directly from the
    handler's own UPSERT counters for this reconcile call — preferred
    over the coarser 'is this business_date new at all' heuristic,
    because a business_date can be PARTIALLY covered already (e.g. a
    prior P3 rolling-window pull) without every row in it being new.
    inserted>0 means at least some rows genuinely did not exist before
    this pull (DB_MISSING_ROW dominates); inserted==0 and updated>0
    means every touched row already existed and was refreshed
    (EXPECTED_LATE_SOURCE_UPDATE). Returns None when there is nothing to
    classify from (both zero) — caller falls back to the diff-based
    default."""
    if inserted > 0:
        return "DB_MISSING_ROW"
    if updated > 0:
        return "EXPECTED_LATE_SOURCE_UPDATE"
    return None


def recon_row(cur, business_date, recon_type: str, channel: str, shop_id: str,
              before, after, definition: str, date_basis: str, status_basis: str,
              currency, etl_run_id: str, is_new_date: bool,
              override_status: Optional[str] = None, extra_details: Optional[dict] = None) -> dict:
    """Classify, write the audit.reconciliation_result row, and return a
    plain dict summary (used to build the P4 CSV without a second DB
    read). override_status (typically from classify_from_counts()) is
    only applied when before != after — a metric that did not move is
    always NO_CHANGE regardless of what the handler's counters say
    (UPSERT always 'touches' a matching row even when its value is
    identical, so a nonzero updated count alone never overrides a true
    zero-diff metric)."""
    if _num(before) == _num(after):
        status = "NO_CHANGE"
    else:
        status = override_status or classify_diff(before, after, is_new_date)
    details = {
        "SOURCE": channel, "DATE_BASIS": date_basis, "STATUS_BASIS": status_basis,
        "CURRENCY": currency, "METRIC_DEFINITION": definition,
        "API_VALUE_BEFORE_UPSERT": after,  # see write_recon_result docstring
        "DB_VALUE_BEFORE_UPSERT": before,
        "DB_VALUE_AFTER_UPSERT": after,
        "RECONCILIATION_RUN_ID": etl_run_id,
        "IS_NEW_BUSINESS_DATE": is_new_date,
    }
    if extra_details:
        details.update(extra_details)
    write_recon_result(cur, business_date, recon_type, channel, shop_id, after, before, status, details)
    diff = (_num(after) - _num(before)) if (_num(after) is not None and _num(before) is not None) else None
    return {
        "business_date": str(business_date), "recon_type": recon_type, "channel": channel,
        "shop_id": shop_id, "before": before, "after": after, "difference": diff,
        "status": status, "definition": definition, "date_basis": date_basis,
        "status_basis": status_basis, "currency": currency, "etl_run_id": etl_run_id,
    }
