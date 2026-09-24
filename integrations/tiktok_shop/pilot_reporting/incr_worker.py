#!/usr/bin/env python3
"""
P3 — TikTok incremental worker. Invoked as its own subprocess by
pipelines/incremental.py (kept out of the same process as the Shopee
client — both integrations/shopee/ and integrations/tiktok_shop/ define
a bare `config.py`, which collide if both are on sys.path at once).

    python3 incr_worker.py <domain> <window_start_iso> <window_end_iso> <etl_run_id>

Reuses the existing, unmodified TikTokShopClient / bootstrap_session() /
collect.py's paginate() exactly as collect.py and p2b/p2d do. Prints one
JSON result line to stdout; exit code 0 = success, 1 = failure.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

PILOT_DIR = Path(__file__).resolve().parent
PIPELINES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pipelines"
sys.path.insert(0, str(PIPELINES_DIR))
sys.path.insert(0, str(PILOT_DIR))  # pilot_common.py inserts INTEGRATION_DIR itself for tiktok_client/etc.

import pilot_common  # noqa: E402
import collect as tt_collect  # noqa: E402
import incr_common as ic  # noqa: E402
from tiktok_client import NetworkError  # noqa: E402  (pilot_common put INTEGRATION_DIR on sys.path)
from canonical_normalizer import (  # noqa: E402
    normalize_tiktok_finance_sku, aggregate_tiktok_finance_skus,
    assign_occurrence_indices, audit_unknown_nonzero_components,
    decide_settlement_write,
    TIKTOK_SHIPPING_FORMULA_FIELDS, TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS,
    TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS, TIKTOK_REVENUE_FORMULA_FIELDS,
    TIKTOK_REVENUE_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_REVENUE_KNOWN_NESTED_FIELDS,
)


def tiktok_check(status: str, label: str) -> None:
    """PASS_EMPTY is a legitimate 'no rows yet' result (e.g. LIVE data
    latency) — never raised. Only a FAIL_* status is a real failure."""
    if status.startswith("FAIL"):
        raise RuntimeError(f"TikTok {label}: {status}")


class _Log:
    def __init__(self, log_fn):
        self._log = log_fn

    def log(self, msg: str) -> None:
        self._log(msg)


def _with_backoff(call, label, log):
    """ic.with_backoff() only retries requests' Timeout/ConnectionError
    and TransientHTTPError — but tiktok_client wraps every requests
    exception as its own NetworkError (a plain RuntimeError), so a single
    ReadTimeout used to fail the whole domain on the first try. Proven
    live (run 35948968552): TIKTOK/finance D3 reconciliation died on one
    statement_transactions ReadTimeout. Re-raised as TransientHTTPError
    so the existing bounded 5s/15s/45s retry applies; still raises after
    the last retry."""
    def wrapped():
        try:
            return call()
        except NetworkError as e:
            raise ic.TransientHTTPError(str(e)) from None
    return ic.with_backoff(wrapped, label, log)


def _label() -> str:
    return f"incr_{ic.now_utc().strftime('%Y%m%dT%H%M%S')}"


# P11-HEAL / P11-LAST-MILE — TikTok/orders backlog catch-up.
#
# Proven live (runs 35816592219 and 35832785885, 2026-09-23): both runs
# started TikTok/orders at the exact same watermark
# (2026-09-17T09:57:12Z) and both ended DOMAIN_WORKER_TIMEOUT at 900s —
# zero durable progress. P11-HEAL had split the backlog into 1-day
# chunks, but every chunk still wrote into ONE transaction committed
# once at the very end by main(), so pipelines/incremental.py's external
# TIKTOK_ORDERS_TIMEOUT_SECONDS=900 SIGTERM/SIGKILL still rolled back
# every chunk that had already finished (the open transaction dies with
# the connection). And a single 24h chunk was itself not safely bounded
# below 900s.
#
# P11-LAST-MILE fix: each chunk is its own durable transaction on a
# fresh connection — fetch (API only, no DB held open across slow API
# calls — see the Neon connection-reliability note in incr_common),
# then write + advance control.etl_sync_state to exactly chunk_end +
# COMMIT, then emit a progress marker. A later failure or an external
# kill can only ever lose the chunk in flight, never an earlier one.
# Reconciliation reuses the same fetch/write logic via run_orders()
# over its single-day window, WITHOUT ever touching sync_state.
TIKTOK_ORDERS_CHUNK_SIZE = timedelta(hours=4)
# Self-imposed internal soft deadline — must stay comfortably below
# pipelines/incremental.py's TIKTOK_ORDERS_TIMEOUT_SECONDS=900 external
# kill, leaving margin for whatever chunk is in flight when the
# deadline is checked (chunks are never aborted mid-flight, only
# skipped before they start). Cross-file invariant pinned in
# test_orders_catchup.py.
TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS = 780

TIKTOK_ORDERS_CHUNK_MARKER = "HH_TIKTOK_ORDERS_CHUNK_JSON="
TIKTOK_ORDERS_CATCHUP_MARKER = "HH_TIKTOK_ORDERS_CATCHUP_JSON="

ORDERS_STATUS_PASS = "PASS"
ORDERS_STATUS_PARTIAL = ic.PARTIAL_CATCHUP_STATUS
ORDERS_STATUS_FAIL = "FAIL"


def _tiktok_orders_compute_chunks(window_start, window_end, chunk_size=TIKTOK_ORDERS_CHUNK_SIZE):
    """Pure — splits [window_start, window_end) into chunk_size-sized
    sub-windows, chronological order, no gaps or overlaps, exact
    timestamps (never rounded to dates), last chunk may be shorter. A
    normal steady-state incremental window (30min cadence, always <<
    chunk_size) always yields exactly one chunk spanning the whole
    window — identical behavior to before this fix."""
    if window_start >= window_end:
        return []
    chunks = []
    cur_start = window_start
    while cur_start < window_end:
        chunk_end = min(cur_start + chunk_size, window_end)
        chunks.append((cur_start, chunk_end))
        cur_start = chunk_end
    return chunks


def _emit_marker(prefix: str, payload: dict) -> None:
    print(f"{prefix}{json.dumps(payload, default=str)}", flush=True)


def _run_tiktok_orders_durable_catchup(chunks, fetch_chunk, write_chunk, advance_watermark, connect,
                                        log, deadline_monotonic, now_monotonic, emit=_emit_marker) -> dict:
    """Control flow for the incremental catch-up, with every side effect
    injected (no DB/HTTP/wall-clock coupling in tests).

    Per chunk: fetch_chunk(s, e) (API only) -> connect() a fresh
    connection -> write_chunk(cur, fetched, s, e) -> advance_watermark(
    cur, e) -> conn.commit() -> emit a progress marker. The watermark
    moves in the SAME transaction as that chunk's rows, so it can never
    claim coverage the DB doesn't hold, and each commit is independent
    of every later one.

    Never starts a chunk once the internal deadline has passed. A chunk
    that raises is rolled back alone and stops the catch-up; earlier
    committed chunks are untouched. Status:
      - every requested chunk committed          -> PASS
      - a chunk failed (any prior progress kept) -> FAIL
      - soft deadline after >=1 committed chunk  -> PARTIAL_CATCHUP
      - soft deadline before any chunk           -> FAIL (zero progress)
    """
    meta = {
        "status": None,
        "requested_window_start": chunks[0][0].isoformat() if chunks else None,
        "requested_window_end": chunks[-1][1].isoformat() if chunks else None,
        "last_committed_end": None,
        "chunks_total": len(chunks),
        "chunks_attempted": 0,
        "chunks_completed": 0,
        "catchup_complete": False,
        "stopped_by_soft_deadline": False,
        "error_class": None,
        "error": None,
        "totals": {"orders_seen": 0, "orders_inserted": 0, "orders_updated": 0,
                   "items_inserted": 0, "items_updated": 0},
    }
    if not chunks:
        meta.update(status=ORDERS_STATUS_FAIL, error_class="EMPTY_WINDOW",
                    error="TikTok orders: empty window, nothing to catch up")
        return meta

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        if now_monotonic() >= deadline_monotonic:
            log(f"internal catch-up time budget reached before chunk {i + 1}/{len(chunks)} "
                f"({chunk_start.isoformat()}..{chunk_end.isoformat()}) — stopping here, "
                f"{meta['chunks_completed']} chunk(s) already committed this run")
            meta["stopped_by_soft_deadline"] = True
            break

        meta["chunks_attempted"] += 1
        t0 = time.monotonic()
        conn = None
        try:
            fetched = fetch_chunk(chunk_start, chunk_end)
            conn = connect()
            cur = conn.cursor()
            stats = write_chunk(cur, fetched, chunk_start, chunk_end)
            advance_watermark(cur, chunk_end)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass  # a dead connection has already discarded the transaction
            log(f"chunk {i + 1}/{len(chunks)} ({chunk_start.isoformat()}..{chunk_end.isoformat()}) failed "
                f"({e}) — rolled back this chunk only, {meta['chunks_completed']} earlier chunk(s) stay committed")
            meta["error_class"] = type(e).__name__
            meta["error"] = str(e)
            break
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        meta["chunks_completed"] += 1
        meta["last_committed_end"] = chunk_end.isoformat()
        for k in meta["totals"]:
            meta["totals"][k] += stats.get(k, 0)
        emit(TIKTOK_ORDERS_CHUNK_MARKER, {
            "chunk_start": chunk_start.isoformat(),
            "chunk_end": chunk_end.isoformat(),
            "duration_seconds": round(time.monotonic() - t0, 3),
            "orders_seen": stats.get("orders_seen", 0),
            "orders_inserted": stats.get("orders_inserted", 0),
            "orders_updated": stats.get("orders_updated", 0),
            "items_inserted": stats.get("items_inserted", 0),
            "items_updated": stats.get("items_updated", 0),
            "committed": True,
        })

    if meta["chunks_completed"] == len(chunks):
        meta["status"] = ORDERS_STATUS_PASS
        meta["catchup_complete"] = True
    elif meta["error"] is not None:
        meta["status"] = ORDERS_STATUS_FAIL
    elif meta["chunks_completed"] > 0:
        meta["status"] = ORDERS_STATUS_PARTIAL
    else:
        meta.update(status=ORDERS_STATUS_FAIL, error_class="NO_PROGRESS",
                    error=f"TikTok orders: no chunk completed out of {len(chunks)} requested "
                          f"({chunks[0][0].isoformat()}..{chunks[-1][1].isoformat()})")

    emit(TIKTOK_ORDERS_CATCHUP_MARKER, {k: v for k, v in meta.items() if k != "error"})
    return meta


def _fetch_orders_window(session, window_start, window_end, log) -> tuple[list[dict], int]:
    """API only, no DB. Returns (order details, order ids seen)."""
    rows, status = tt_collect.paginate(
        _Log(log), session, "orders", _label(), "orders_incr",
        query_base={"page_size": "50"},
        body_base={
            "update_time_ge": int(window_start.timestamp()),
            "update_time_lt": int(window_end.timestamp()),
        },
    )
    tiktok_check(status, "orders")
    order_ids = [r["id"] for r in rows if r.get("id")]
    log(f"orders {window_start.isoformat()}..{window_end.isoformat()}: {len(order_ids)} ids, status={status}")

    details: list[dict] = []
    for i in range(0, len(order_ids), 50):
        id_batch = order_ids[i:i + 50]

        def call(id_batch=id_batch):
            return session.client.read_domain(
                "order_detail", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"),
                extra_query={"ids": ",".join(id_batch)},
            )
        resp = _with_backoff(call, "tiktok order_detail", log)
        if resp.ok:
            details.extend(resp.data.get("orders", []))
        else:
            raise RuntimeError(f"order_detail failed: code={resp.code} message={resp.message}")
    return details, len(order_ids)


def _write_orders(cur, etl_run_id, shop_id, details, window_end, order_ctr, item_ctr) -> None:
    """DB only, no API. UPSERTs into the caller's open transaction —
    never commits itself."""
    for o in details:
        oid = o.get("id")
        payment = o.get("payment") or {}
        bd = ic.ts_from_epoch(o.get("create_time"))
        bd = bd.astimezone(ic.VN_TZ).date() if bd else ic.vn_date(window_end)
        cur.execute(
            """
            INSERT INTO core.fact_order (
                channel, shop_id, order_id, order_status, order_create_time,
                order_update_time, currency, total_amount, business_date,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, order_id) DO UPDATE SET
                order_status = EXCLUDED.order_status,
                order_update_time = EXCLUDED.order_update_time,
                total_amount = EXCLUDED.total_amount,
                source_updated_at = EXCLUDED.source_updated_at,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "TIKTOK", shop_id, oid, o.get("status"),
                ic.ts_from_epoch(o.get("create_time")), ic.ts_from_epoch(o.get("update_time")),
                payment.get("currency"), payment.get("total_amount"), bd,
                "TIKTOK", oid, ic.ts_from_epoch(o.get("create_time")), ic.ts_from_epoch(o.get("update_time")),
                "/order/202309/orders", shop_id, etl_run_id,
            ),
        )
        order_ctr.record(cur.fetchone()[0])

        for li in o.get("line_items", []):
            qty = 1
            unit_price = ic.dec(li.get("sale_price"))
            platform_product_id = str(li["product_id"]) if li.get("product_id") is not None else None
            platform_sku_id = str(li["sku_id"]) if li.get("sku_id") is not None else None
            cur.execute(
                """
                INSERT INTO core.fact_order_item (
                    channel, shop_id, order_id, order_item_id, sku, product_name,
                    qty, unit_price, item_amount, business_date,
                    platform_product_id, platform_sku_id,
                    source_system, source_record_id, source_created_at, source_updated_at,
                    source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, shop_id, order_id, order_item_id) DO UPDATE SET
                    qty = EXCLUDED.qty, unit_price = EXCLUDED.unit_price,
                    item_amount = EXCLUDED.item_amount, source_updated_at = EXCLUDED.source_updated_at,
                    sku = EXCLUDED.sku, platform_product_id = EXCLUDED.platform_product_id,
                    platform_sku_id = EXCLUDED.platform_sku_id,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    "TIKTOK", shop_id, oid, li.get("id"), li.get("seller_sku") or None, li.get("product_name"),
                    qty, unit_price, unit_price, bd,
                    platform_product_id, platform_sku_id,
                    "TIKTOK", li.get("id"), None, None,
                    "/order/202309/orders", shop_id, etl_run_id,
                ),
            )
            item_ctr.record(cur.fetchone()[0])


def run_orders_window(cur, session, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    """One bounded window: fetch + write into the caller's transaction.
    No commit, no sync_state — the caller owns both. Shared by the
    reconciliation single-day handler (via run_orders) and, split into
    its fetch/write halves, the incremental durable catch-up."""
    order_ctr, item_ctr = ic.Counters(), ic.Counters()
    details, ids_seen = _fetch_orders_window(session, window_start, window_end, log)
    _write_orders(cur, etl_run_id, shop_id, details, window_end, order_ctr, item_ctr)
    return {"order_ids_count": ids_seen, "order": order_ctr.as_dict(), "order_item": item_ctr.as_dict()}


def run_orders(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    """Reconciliation path: one single-day window, one pass, written
    into the caller's transaction, never touches sync_state."""
    session = pilot_common.bootstrap_session()
    return run_orders_window(cur, session, etl_run_id, shop_id, window_start, window_end, log)


def run_orders_incremental(etl_run_id, shop_id, window_start, window_end, log,
                            connect=ic.get_db_conn) -> tuple[dict, dict]:
    """Incremental path: durable per-chunk catch-up. Owns its own
    connections and commits; the caller must NOT upsert sync_state or
    commit anything afterwards. Returns (result, catchup_meta)."""
    session = pilot_common.bootstrap_session()

    def fetch_chunk(chunk_start, chunk_end):
        return _fetch_orders_window(session, chunk_start, chunk_end, log)

    def write_chunk(cur, fetched, chunk_start, chunk_end):
        details, ids_seen = fetched
        chunk_orders, chunk_items = ic.Counters(), ic.Counters()
        _write_orders(cur, etl_run_id, shop_id, details, chunk_end, chunk_orders, chunk_items)
        return {
            "orders_seen": ids_seen,
            "orders_inserted": chunk_orders.inserted, "orders_updated": chunk_orders.updated,
            "items_inserted": chunk_items.inserted, "items_updated": chunk_items.updated,
        }

    def advance_watermark(cur, chunk_end):
        ic.upsert_sync_state(cur, "TIKTOK", "orders", shop_id, chunk_end, ic.vn_date(chunk_end), "success")

    chunks = _tiktok_orders_compute_chunks(window_start, window_end)
    deadline = time.monotonic() + TIKTOK_ORDERS_CHUNK_DEADLINE_SECONDS
    meta = _run_tiktok_orders_durable_catchup(
        chunks, fetch_chunk, write_chunk, advance_watermark, connect, log, deadline, time.monotonic,
    )
    # Totals cover committed chunks only — a rolled-back chunk's rows
    # were never counted (see _run_tiktok_orders_durable_catchup).
    t = meta["totals"]

    def _ctr(inserted, updated):
        return {"received": inserted + updated, "inserted": inserted, "updated": updated, "unchanged": 0}

    result = {
        "order_ids_count": t["orders_seen"],
        "order": _ctr(t["orders_inserted"], t["orders_updated"]),
        "order_item": _ctr(t["items_inserted"], t["items_updated"]),
    }
    return result, meta


def run_returns(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    session = pilot_common.bootstrap_session()
    rows, status = tt_collect.paginate(
        _Log(log), session, "return_refund", _label(), "returns_incr",
        query_base={"page_size": "50", "sort_field": "create_time", "sort_order": "DESC"},
        body_base={"create_time_ge": int(window_start.timestamp()), "create_time_lt": int(window_end.timestamp())},
    )
    tiktok_check(status, "returns")
    ctr = ic.Counters()
    for r in rows:
        return_id = r.get("id") or r.get("return_id")
        refund = r.get("refund_amount") or {}
        bd = ic.ts_from_epoch(r.get("create_time"))
        bd = bd.astimezone(ic.VN_TZ).date() if bd else ic.vn_date(window_end)
        cur.execute(
            """
            INSERT INTO core.fact_return_refund (
                channel, shop_id, order_id, return_id, return_status, refund_amount,
                return_reason, business_date, currency, source_system, source_record_id,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, return_id) DO UPDATE SET
                return_status = EXCLUDED.return_status, refund_amount = EXCLUDED.refund_amount,
                currency = EXCLUDED.currency, etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                # P8.6 fix: the real API field is refund_amount.refund_total
                # (confirmed live, e.g. {"refund_subtotal":"355300",
                # "refund_shipping_fee":"0","refund_tax":"0","refund_total":
                # "355300"}) — there is no "amount" key, so this always
                # wrote NULL before. refund_total is the sum HH's
                # "refund_amount" column is meant to represent.
                "TIKTOK", shop_id, r.get("order_id"), return_id, r.get("return_status"),
                refund.get("refund_total") if isinstance(refund, dict) else refund,
                r.get("return_reason") or r.get("reason_text"), bd,
                refund.get("currency") if isinstance(refund, dict) else None,
                "TIKTOK", return_id, "/return_refund/202602/returns/search", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return {"return": ctr.as_dict(), "status": status}


def run_finance(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    session = pilot_common.bootstrap_session()
    order_rows, order_status = tt_collect.paginate(
        _Log(log), session, "orders", _label(), "orders_for_finance_incr",
        query_base={"page_size": "50"},
        body_base={
            "update_time_ge": int(window_start.timestamp()),
            "update_time_lt": int(window_end.timestamp()),
        },
    )
    tiktok_check(order_status, "orders_for_finance")
    order_ids = [r["id"] for r in order_rows if r.get("id")]

    unsettled_rows, unsettled_status = tt_collect.paginate(
        _Log(log), session, "finance_unsettled", _label(), "finance_unsettled_incr",
        query_base={"page_size": "50", "sort_field": "order_create_time", "sort_order": "DESC"},
        max_pages=10,
    )
    tiktok_check(unsettled_status, "finance_unsettled")
    unsettled_by_order = {r.get("order_id"): r for r in unsettled_rows if r.get("order_id")}

    ctr = ic.Counters()
    sku_fee_ctr = ic.Counters()
    sku_tx_ctr = ic.Counters()
    for oid in order_ids:
        def call(oid=oid):
            return session.client.read_domain(
                "finance_order_statement_transactions", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"), path_params={"order_id": oid},
            )
        resp = _with_backoff(call, f"tiktok finance {oid}", log)
        has_real = resp.ok and bool(resp.data.get("sku_transactions"))

        # Phase 6C P4-bis — TRANSIENT_SETTLEMENT_REGRESSION guard. Proven
        # live: ic.with_backoff() only retries on Timeout/ConnectionError/
        # TransientHTTPError exceptions; a "not ok" response that doesn't
        # raise one of those (as happened for order 586086803325813933)
        # passes straight through, unretried, and the unconditional UPSERT
        # below would silently overwrite a confirmed-good MATCHED row with
        # UNKNOWN/NULL. Check BEFORE deciding anything only when this
        # result would NOT be a fresh MATCHED (decide_settlement_write
        # short-circuits to WRITE immediately for MATCHED, so this costs
        # nothing on the common path).
        if not has_real:
            cur.execute(
                "SELECT settlement_type, settlement_amount FROM core.fact_settlement "
                "WHERE channel='TIKTOK' AND shop_id=%s AND settlement_id=%s;",
                (shop_id, oid),
            )
            existing_row = cur.fetchone()
            existing = {"settlement_type": existing_row[0], "settlement_amount": existing_row[1]} if existing_row else None
            if decide_settlement_write(existing, "UNKNOWN" if not resp.ok else "NOT_SETTLED_YET") == "RETRY":
                resp = _with_backoff(call, f"tiktok finance RETRY {oid}", log)
                has_real = resp.ok and bool(resp.data.get("sku_transactions"))
                if not has_real:
                    retry_status = "UNKNOWN" if not resp.ok else "NOT_SETTLED_YET"
                    log(f"DQ_WARNING TRANSIENT_SETTLEMENT_REGRESSION order={oid} "
                        f"existing_type={existing['settlement_type']} existing_amount={existing['settlement_amount']} "
                        f"retry_status={retry_status} -- PRESERVING existing value, skipping write this cycle")
                    time.sleep(0.12)
                    continue  # preserve existing row untouched -- no write for this order this cycle

        if has_real:
            d = resp.data
            status, settlement_amount = "MATCHED", ic.dec(d.get("settlement_amount"))
            revenue, fee, ship, currency = (
                ic.dec(d.get("revenue_amount")), ic.dec(d.get("fee_and_tax_amount")),
                ic.dec(d.get("shipping_cost_amount")), d.get("currency"),
            )
            # P6B.3 — the same response already carries itemized per-SKU
            # fee_tax_breakdown fields (Fixed/Payment/VXP/Infrastructure/
            # Affiliate), proven real in P6B.2. business_date here uses
            # order_create_time from THIS SAME response — the locked
            # create_time contract — never the settlement check-date.
            order_bd = ic.vn_date(ic.ts_from_epoch(d.get("order_create_time"))) if d.get("order_create_time") else None

            # Phase 6C Gate 5 grain fix (Design 3 / H2) — TikTok can
            # return MULTIPLE sku_transaction entries sharing one
            # sku_id (e.g. a sale line + a separate refund-adjustment
            # line, each its own statement_id — proven live, order
            # 585474165265106454). Step 1: persist EVERY entry, lossless,
            # into the transaction-grain table BEFORE deriving anything
            # aggregate — never skip this even for a single-entry sku_id,
            # so this table is always the complete source of truth.
            valid_tx = [tx for tx in d.get("sku_transactions", []) if tx.get("sku_id")]
            for tx, content_hash, occurrence_index in assign_occurrence_indices(valid_tx):
                sku_id = tx.get("sku_id")
                norm_tx = normalize_tiktok_finance_sku(tx)
                st = norm_tx["structured"]
                cur.execute(
                    """
                    INSERT INTO core.fact_settlement_sku_transaction (
                        channel, shop_id, order_id, sku_id, statement_id, business_date,
                        quantity, settlement_amount, revenue_amount, shipping_cost_amount, fee_tax_amount,
                        fixed_fee, payment_fee, vxp_fee, infrastructure_fee, affiliate_fee,
                        affiliate_ads_commission_amount, affiliate_partner_commission_amount, tap_shop_ads_commission_amount,
                        sku_name, product_name,
                        fee_tax_breakdown_raw, revenue_breakdown_raw, shipping_cost_breakdown_raw,
                        content_hash, occurrence_index,
                        source_system, source_endpoint, source_updated_at, etl_run_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,
                        'TIKTOK', '/finance/202501/orders/{order_id}/statement_transactions', now(), %s)
                    ON CONFLICT (channel, shop_id, order_id, content_hash, occurrence_index) DO NOTHING
                    RETURNING id;
                    """,
                    (
                        "TIKTOK", shop_id, oid, sku_id, tx.get("statement_id"), order_bd,
                        ic.dec(tx.get("quantity")), ic.dec(tx.get("settlement_amount")),
                        ic.dec(tx.get("revenue_amount")), ic.dec(tx.get("shipping_cost_amount")), ic.dec(tx.get("fee_tax_amount")),
                        st["fixed_fee"], st["payment_fee"], st["vxp_fee"], st["infrastructure_fee"], st["affiliate_fee"],
                        st["affiliate_ads_commission_amount"], st["affiliate_partner_commission_amount"], st["tap_shop_ads_commission_amount"],
                        tx.get("sku_name"), tx.get("product_name"),
                        json.dumps(norm_tx["fee_tax_breakdown_raw"]) if norm_tx["fee_tax_breakdown_raw"] is not None else None,
                        json.dumps(norm_tx["revenue_breakdown_raw"]) if norm_tx["revenue_breakdown_raw"] is not None else None,
                        json.dumps(norm_tx["shipping_cost_breakdown_raw"]) if norm_tx["shipping_cost_breakdown_raw"] is not None else None,
                        content_hash, occurrence_index, etl_run_id,
                    ),
                )
                # ON CONFLICT DO NOTHING (append-only — see sql/061's
                # IMMUTABLE_EVENT_LEDGER semantics) returns no row on a
                # dedup skip; Counters.record(bool) is reused here as
                # "inserted vs already-present", not its usual
                # inserted-vs-updated meaning (this table is never
                # UPDATEd by this path).
                sku_tx_ctr.record(cur.fetchone() is not None)

                # C4 — unknown-nonzero-component guard: never silently
                # compute an incomplete total. Persisting raw above
                # already happened regardless of this check (safer than
                # failing ingestion over a still-unclassified field);
                # this only emits a DQ warning to the run log.
                for label, bd, formula, known_zero, known_nested in (
                    ("shipping", tx.get("shipping_cost_breakdown"), TIKTOK_SHIPPING_FORMULA_FIELDS,
                     TIKTOK_SHIPPING_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_SHIPPING_KNOWN_NESTED_FIELDS),
                    ("revenue", tx.get("revenue_breakdown"), TIKTOK_REVENUE_FORMULA_FIELDS,
                     TIKTOK_REVENUE_KNOWN_ALWAYS_ZERO_FIELDS, TIKTOK_REVENUE_KNOWN_NESTED_FIELDS),
                ):
                    unknown = audit_unknown_nonzero_components(bd, formula, known_zero, known_nested)
                    if unknown:
                        log(f"DQ_WARNING unknown_nonzero_component order={oid} sku={sku_id} "
                            f"statement={tx.get('statement_id')} kind={label} fields={unknown} "
                            f"-- computed total may be INCOMPLETE, raw was still persisted")

            # Step 2: derive the canonical order+sku aggregate from the
            # SAME in-memory tx list just persisted (no extra API/DB
            # round trip needed) — SUM across every real transaction for
            # that sku_id, never last-write-wins.
            by_sku: dict[str, list] = {}
            for tx in valid_tx:
                by_sku.setdefault(tx.get("sku_id"), []).append(tx)

            for sku_id, tx_group in by_sku.items():
                agg = aggregate_tiktok_finance_skus(tx_group)
                s = agg["structured"]
                # C2 — fact_settlement_sku_fee's raw columns cannot
                # losslessly represent an aggregate of >1 source entry as
                # a single flat object. They are explicitly INFORMATIONAL
                # ONLY here (the entry with the largest |settlement_amount|
                # in the group) — full lossless fidelity lives in
                # core.fact_settlement_sku_transaction, not in this column.
                representative = max(
                    tx_group,
                    key=lambda t: abs(ic.dec(t.get("settlement_amount")) or Decimal(0)),
                )
                fee_tax_raw = json.dumps(representative.get("fee_tax_breakdown")) if representative.get("fee_tax_breakdown") is not None else None
                revenue_raw = json.dumps(representative.get("revenue_breakdown")) if representative.get("revenue_breakdown") is not None else None
                shipping_raw = json.dumps(representative.get("shipping_cost_breakdown")) if representative.get("shipping_cost_breakdown") is not None else None
                cur.execute(
                    """
                    INSERT INTO core.fact_settlement_sku_fee (
                        channel, shop_id, order_id, sku_id, statement_id, business_date,
                        revenue_amount, fixed_fee, payment_fee, vxp_fee, infrastructure_fee, affiliate_fee,
                        affiliate_ads_commission_amount,
                        fee_tax_breakdown_raw, revenue_breakdown_raw, shipping_cost_breakdown_raw,
                        currency, finance_state, value_basis, source_system, source_record_id,
                        source_endpoint, source_updated_at, etl_run_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,'MATCHED','API_ACTUAL','TIKTOK',%s,
                        '/finance/202501/orders/{order_id}/statement_transactions', now(), %s)
                    ON CONFLICT (channel, shop_id, order_id, sku_id) DO UPDATE SET
                        statement_id = EXCLUDED.statement_id, business_date = EXCLUDED.business_date,
                        revenue_amount = EXCLUDED.revenue_amount, fixed_fee = EXCLUDED.fixed_fee,
                        payment_fee = EXCLUDED.payment_fee, vxp_fee = EXCLUDED.vxp_fee,
                        infrastructure_fee = EXCLUDED.infrastructure_fee, affiliate_fee = EXCLUDED.affiliate_fee,
                        affiliate_ads_commission_amount = EXCLUDED.affiliate_ads_commission_amount,
                        fee_tax_breakdown_raw = EXCLUDED.fee_tax_breakdown_raw,
                        revenue_breakdown_raw = EXCLUDED.revenue_breakdown_raw,
                        shipping_cost_breakdown_raw = EXCLUDED.shipping_cost_breakdown_raw,
                        currency = EXCLUDED.currency, source_updated_at = now(), etl_run_id = EXCLUDED.etl_run_id
                    RETURNING (xmax = 0) AS inserted;
                    """,
                    (
                        "TIKTOK", shop_id, oid, sku_id, representative.get("statement_id"), order_bd,
                        agg["computed_revenue"], s["fixed_fee"], s["payment_fee"], s["vxp_fee"],
                        s["infrastructure_fee"], s["affiliate_fee"], s["affiliate_ads_commission_amount"],
                        fee_tax_raw, revenue_raw, shipping_raw,
                        currency, f"{oid}:{sku_id}", etl_run_id,
                    ),
                )
                sku_fee_ctr.record(cur.fetchone()[0])
        elif resp.ok:
            status, settlement_amount, revenue, fee, ship, currency = "NOT_SETTLED_YET", None, None, None, None, None
        else:
            status = "NOT_SETTLED_YET" if oid in unsettled_by_order else "UNKNOWN"
            settlement_amount = revenue = fee = ship = currency = None

        cur.execute(
            """
            INSERT INTO core.fact_settlement (
                channel, shop_id, order_id, settlement_id, settlement_amount,
                settlement_type, settlement_time, business_date,
                currency, revenue_amount, fee_and_tax_amount, shipping_cost_amount,
                source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, settlement_id) DO UPDATE SET
                settlement_amount = EXCLUDED.settlement_amount, settlement_type = EXCLUDED.settlement_type,
                currency = EXCLUDED.currency, revenue_amount = EXCLUDED.revenue_amount,
                fee_and_tax_amount = EXCLUDED.fee_and_tax_amount, shipping_cost_amount = EXCLUDED.shipping_cost_amount,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "TIKTOK", shop_id, oid, oid, settlement_amount, status, ic.vn_date(window_end),
                currency, revenue, fee, ship,
                "TIKTOK", oid, "/finance/202501/orders/{order_id}/statement_transactions", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
        time.sleep(0.12)
    return {
        "settlement": ctr.as_dict(), "settlement_sku_fee": sku_fee_ctr.as_dict(),
        "settlement_sku_transaction": sku_tx_ctr.as_dict(),
    }


def run_affiliate(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    session = pilot_common.bootstrap_session()
    rows, status = tt_collect.paginate(
        _Log(log), session, "affiliate", _label(), "affiliate_incr",
        query_base={"page_size": "50"},
        body_base={"create_time_ge": int(window_start.timestamp()), "create_time_lt": int(window_end.timestamp())},
    )
    tiktok_check(status, "affiliate")
    ctr = ic.Counters()
    for r in rows:
        order_id = r.get("id")
        bd = ic.ts_from_epoch(r.get("create_time"))
        bd = bd.astimezone(ic.VN_TZ).date() if bd else ic.vn_date(window_end)
        for sku in r.get("skus", []):
            est_base = sku.get("estimated_commission_base") or {}
            est_comm = sku.get("estimated_paid_shop_ads_commission") or {}
            settlement_status = sku.get("settlement_status")
            est_comm_amount = ic.dec(est_comm.get("amount"))
            settled = est_comm_amount if settlement_status == "SETTLED" else None
            source_record_id = f"{order_id}:{sku.get('sku_id')}:{sku.get('content_id')}"
            cur.execute(
                """
                INSERT INTO core.fact_affiliate_daily (
                    channel, shop_id, order_id, sku_id, content_id, product_id,
                    creator_username, content_type, commission_model, quantity, currency,
                    affiliate_attributed_gmv, estimated_commission, validated_commission,
                    settled_commission, settlement_status, business_date,
                    source_system, source_record_id, source_created_at, source_updated_at,
                    source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
                ON CONFLICT (channel, shop_id, order_id, sku_id, content_id) DO UPDATE SET
                    affiliate_attributed_gmv = EXCLUDED.affiliate_attributed_gmv,
                    estimated_commission = EXCLUDED.estimated_commission,
                    settled_commission = EXCLUDED.settled_commission,
                    settlement_status = EXCLUDED.settlement_status,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    "TIKTOK", shop_id, order_id, sku.get("sku_id"), sku.get("content_id"), sku.get("product_id"),
                    sku.get("creator_username"), sku.get("content_type"), sku.get("commission_model"),
                    sku.get("quantity"), est_base.get("currency"),
                    ic.dec(est_base.get("amount")), est_comm_amount, settled, settlement_status, bd,
                    "TIKTOK", source_record_id, ic.ts_from_epoch(r.get("create_time")),
                    "/affiliate_seller/202410/orders/search", shop_id, etl_run_id,
                ),
            )
            ctr.record(cur.fetchone()[0])
    return {"affiliate": ctr.as_dict(), "status": status}


def run_product_analytics(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=None) -> dict:
    session = pilot_common.bootstrap_session()
    # target_date: see run_ads()'s identical note in the Shopee worker —
    # a P4 reconcile window's window_end is the exclusive start of the
    # NEXT day, not "now", so it cannot be used to derive the target date.
    base_date = target_date or ic.vn_date(window_end)
    today = base_date.strftime("%Y-%m-%d")
    next_day = (base_date + timedelta(days=1)).strftime("%Y-%m-%d")
    rows, status = tt_collect.paginate(
        _Log(log), session, "product_analytics", _label(), "product_analytics_incr",
        query_base={"start_date_ge": today, "end_date_lt": next_day, "currency": "LOCAL", "page_size": "100"},
    )
    tiktok_check(status, "product_analytics")
    ctr = ic.Counters()
    for r in rows:
        product_id = r.get("id")
        if not product_id:
            continue
        tp = r.get("total_performance") or {}
        gmv = tp.get("gmv") or {}
        cur.execute(
            """
            INSERT INTO core.fact_product_analytics_daily (
                channel, shop_id, product_id, sku_id, business_date,
                impressions, clicks, ctr, attributed_orders, click_to_order_rate,
                gmv_amount, gmv_currency, items_sold,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
            ON CONFLICT (channel, shop_id, product_id, business_date) DO UPDATE SET
                impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks, ctr = EXCLUDED.ctr,
                attributed_orders = EXCLUDED.attributed_orders, click_to_order_rate = EXCLUDED.click_to_order_rate,
                gmv_amount = EXCLUDED.gmv_amount, gmv_currency = EXCLUDED.gmv_currency,
                items_sold = EXCLUDED.items_sold, etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "TIKTOK", shop_id, product_id, base_date,
                tp.get("product_impressions"), tp.get("product_clicks"), ic.dec(tp.get("ctr")),
                tp.get("orders"), ic.dec(tp.get("click_order_rate")),
                ic.dec(gmv.get("amount")), gmv.get("currency"), tp.get("items_sold"),
                "TIKTOK", product_id, ic.now_utc(),
                "/analytics/202605/shop_products/performance", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return {"product_analytics": ctr.as_dict(), "status": status}


def run_shop_traffic(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=None) -> dict:
    """P8.5 — real shop-level traffic (visitors), proven live via
    GET /analytics/202510/shop/performance/{date}/performance_per_hour
    (scope data.shop_analytics.public.read, GRANTED). Requires
    shop_cipher (from session.shop_info), not just shop_id — the earlier
    HH_TIKTOK_OPEN_API_SETUP_REPORT.md PASS_EMPTY classification for this
    endpoint was stale/wrong, traced to that missing parameter, not a
    real empty-data condition. 'visitors' is TikTok's own field name —
    never conflated with impressions/clicks/views/sessions."""
    session = pilot_common.bootstrap_session()
    base_date = target_date or ic.vn_date(window_end)
    date_str = base_date.strftime("%Y-%m-%d")
    resp = session.client.read_domain(
        "analytics", session.access_token,
        shop_cipher=session.shop_info.get("shop_cipher"),
        path_params={"date": date_str},
    )
    if not resp.ok:
        raise RuntimeError(f"TikTok shop_traffic {date_str}: {resp.code} {resp.message}")

    perf = (resp.data or {}).get("performance") or {}
    overall = perf.get("overall") or {}
    intervals = perf.get("intervals") or []
    ENDPOINT = "/analytics/202510/shop/performance/{date}/performance_per_hour"

    daily_ctr = ic.Counters()
    ov_gmv = overall.get("gmv") or {}
    cur.execute(
        """
        INSERT INTO core.fact_shop_traffic_daily (
            channel, shop_id, business_date, visitors, customers,
            gmv_amount, gmv_currency, items_sold, value_basis,
            source_system, source_endpoint, source_shop_id, etl_run_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'API_OVERALL',%s,%s,%s,%s)
        ON CONFLICT (channel, shop_id, business_date) DO UPDATE SET
            visitors = EXCLUDED.visitors, customers = EXCLUDED.customers,
            gmv_amount = EXCLUDED.gmv_amount, gmv_currency = EXCLUDED.gmv_currency,
            items_sold = EXCLUDED.items_sold, etl_run_id = EXCLUDED.etl_run_id
        RETURNING (xmax = 0) AS inserted;
        """,
        (
            "TIKTOK", shop_id, base_date, overall.get("visitors"), overall.get("customers"),
            ic.dec(ov_gmv.get("amount")), ov_gmv.get("currency"), overall.get("items_sold"),
            "TIKTOK", ENDPOINT, shop_id, etl_run_id,
        ),
    )
    daily_ctr.record(cur.fetchone()[0])

    hourly_ctr = ic.Counters()
    for interval in intervals:
        if "index" not in interval:
            continue
        gmv = interval.get("gmv") or {}
        cur.execute(
            """
            INSERT INTO core.fact_shop_traffic_hourly (
                channel, shop_id, business_date, interval_index, visitors, customers,
                gmv_amount, gmv_currency, items_sold, source_system, source_endpoint,
                source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, business_date, interval_index) DO UPDATE SET
                visitors = EXCLUDED.visitors, customers = EXCLUDED.customers,
                gmv_amount = EXCLUDED.gmv_amount, gmv_currency = EXCLUDED.gmv_currency,
                items_sold = EXCLUDED.items_sold, etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "TIKTOK", shop_id, base_date, interval.get("index"), interval.get("visitors"),
                interval.get("customers"), ic.dec(gmv.get("amount")), gmv.get("currency"),
                interval.get("items_sold"), "TIKTOK", ENDPOINT, shop_id, etl_run_id,
            ),
        )
        hourly_ctr.record(cur.fetchone()[0])
    return {"shop_traffic_daily": daily_ctr.as_dict(), "shop_traffic_hourly": hourly_ctr.as_dict()}


def _rate_to_fraction(v):
    """Normalize a rate field to a 0..1 fraction regardless of whether the
    source endpoint formatted it as a bare decimal ("0.0096", as in
    video_analytics) or a percent string ("6.18%", as in live_analytics'
    session-grain payload) — P7.1 probe evidence, both forms observed live.
    Never guesses: returns None for anything that isn't parseable."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        num = ic.dec(s[:-1])
        return (num / 100) if num is not None else None
    return ic.dec(s)


def run_live(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=None,
             account_type="ALL") -> dict:
    """account_type is ALWAYS passed explicitly by every caller (P7.1 fix —
    never rely on the column DEFAULT). 'ALL' is the authoritative total LIVE
    slice and is what P3/P4 have always queried (no account_type filter was
    ever sent before P7.1, which is TikTok's own ALL default — this is a
    label correction, not a behavior change). 'AFFILIATE_ACCOUNTS' is a
    subset/drilldown of ALL, never to be summed with it — see
    core.fact_live_daily's table comment and mart.v_ai_live_daily."""
    session = pilot_common.bootstrap_session()
    # target_date: see run_ads()'s identical note in the Shopee worker.
    base_date = target_date or ic.vn_date(window_end)
    today = base_date.strftime("%Y-%m-%d")
    next_day = (base_date + timedelta(days=1)).strftime("%Y-%m-%d")
    rows, status = tt_collect.paginate(
        _Log(log), session, "live_analytics", _label(), "live_analytics_incr",
        query_base={"start_date_ge": today, "end_date_lt": next_day, "currency": "LOCAL",
                    "page_size": "100", "account_type": account_type},
    )
    tiktok_check(status, "live")  # raises only on FAIL_* — PASS_EMPTY (latency) is not a failure
    ctr = ic.Counters()
    latency_status = "READY" if status == "PASS" else "LATENCY_DATA_NOT_YET_AVAILABLE"
    for r in rows:
        live_id = r.get("id")
        if not live_id:
            continue
        ip = r.get("interaction_performance") or {}
        sp = r.get("sales_performance") or {}
        gmv = sp.get("gmv") or {}
        start_time = ic.ts_from_epoch(r.get("start_time"))
        end_time = ic.ts_from_epoch(r.get("end_time"))
        duration_seconds = int((end_time - start_time).total_seconds()) if start_time and end_time else None
        bd = start_time.astimezone(ic.VN_TZ).date() if start_time else base_date
        cur.execute(
            """
            INSERT INTO core.fact_live_daily (
                channel, shop_id, live_id, business_date, available_data_date,
                title, username, start_time, end_time, account_type, duration_seconds,
                viewers, views, product_impressions, product_clicks, sku_orders,
                customers, items_sold, likes, comments, shares, new_followers,
                avg_viewing_duration_secs, click_through_rate, click_to_order_rate,
                gmv_amount, gmv_currency, latency_status, latest_available_date, source_api_version,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
            ON CONFLICT (channel, shop_id, live_id, account_type) DO UPDATE SET
                viewers = EXCLUDED.viewers, views = EXCLUDED.views,
                product_impressions = EXCLUDED.product_impressions, product_clicks = EXCLUDED.product_clicks,
                sku_orders = EXCLUDED.sku_orders, customers = EXCLUDED.customers,
                items_sold = EXCLUDED.items_sold, likes = EXCLUDED.likes,
                comments = EXCLUDED.comments, shares = EXCLUDED.shares,
                new_followers = EXCLUDED.new_followers,
                avg_viewing_duration_secs = EXCLUDED.avg_viewing_duration_secs,
                click_through_rate = EXCLUDED.click_through_rate,
                click_to_order_rate = EXCLUDED.click_to_order_rate,
                duration_seconds = EXCLUDED.duration_seconds,
                gmv_amount = EXCLUDED.gmv_amount, gmv_currency = EXCLUDED.gmv_currency,
                latency_status = EXCLUDED.latency_status,
                latest_available_date = EXCLUDED.latest_available_date,
                source_api_version = EXCLUDED.source_api_version,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "TIKTOK", shop_id, live_id, bd, base_date,
                r.get("title") or None, r.get("username") or None, start_time, end_time,
                account_type, duration_seconds,
                ip.get("viewers"), ip.get("views"), ip.get("product_impressions"), ip.get("product_clicks"),
                sp.get("sku_orders"), sp.get("customers"), sp.get("items_sold"),
                ip.get("likes"), ip.get("comments"), ip.get("shares"), ip.get("new_followers"),
                ic.dec(ip.get("avg_viewing_duration")),
                _rate_to_fraction(ip.get("click_through_rate")),
                _rate_to_fraction(sp.get("click_to_order_rate")),
                ic.dec(gmv.get("amount")), gmv.get("currency"), latency_status,
                # latest_available_date lives on the top-level response, not
                # per-row — paginate()'s current return contract (rows list +
                # status only) doesn't surface it here. Left NULL rather than
                # fabricated; a follow-up can thread it through if needed.
                None, "202509",
                "TIKTOK", live_id, ic.now_utc(),
                "/analytics/202509/shop_lives/performance", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return {"live": ctr.as_dict(), "status": status, "latency_status": latency_status, "account_type": account_type}


def run_product_inventory(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    session = pilot_common.bootstrap_session()
    rows, status = tt_collect.paginate(
        _Log(log), session, "product", _label(), "products_incr",
        query_base={"page_size": "100"}, body_base={"status": "ALL"},
    )
    tiktok_check(status, "product_inventory")
    product_ctr, inv_ctr = ic.Counters(), ic.Counters()
    now = ic.now_utc()
    inv_date = ic.vn_date(now)
    for p in rows:
        for sku in p.get("skus", []):
            sku_code = (sku.get("seller_sku") or "").strip()
            if not sku_code:
                continue
            cur.execute(
                """
                INSERT INTO core.dim_product (
                    sku, product_name, is_active, source_system, source_record_id, source_updated_at, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sku) DO UPDATE SET
                    product_name = EXCLUDED.product_name, is_active = EXCLUDED.is_active,
                    source_updated_at = EXCLUDED.source_updated_at, etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (sku_code, p.get("title"), (sku.get("status_info") or {}).get("status") == "ACTIVATE",
                 "TIKTOK", sku.get("id") or p.get("id"), now, etl_run_id),
            )
            product_ctr.record(cur.fetchone()[0])

            inventory = sku.get("inventory") or []
            qty_available = sum(int(i.get("quantity") or 0) for i in inventory)
            cur.execute(
                """
                INSERT INTO core.fact_inventory_snapshot (
                    channel, warehouse, sku, snapshot_at, business_date,
                    qty_on_hand, qty_reserved, qty_available,
                    source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, warehouse, sku, snapshot_at) DO UPDATE SET
                    qty_on_hand = EXCLUDED.qty_on_hand, qty_available = EXCLUDED.qty_available,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                ("TIKTOK", "ALL", sku_code, now, inv_date, qty_available, None, qty_available,
                 "TIKTOK", sku.get("id") or p.get("id"), "/product/202502/products/search", shop_id, etl_run_id),
            )
            inv_ctr.record(cur.fetchone()[0])
    return {"dim_product": product_ctr.as_dict(), "fact_inventory_snapshot": inv_ctr.as_dict(), "status": status}


HANDLERS = {
    "orders": run_orders, "returns": run_returns, "finance": run_finance,
    "affiliate": run_affiliate, "product_analytics": run_product_analytics,
    "live": run_live, "product_inventory": run_product_inventory,
    "shop_traffic": run_shop_traffic,
}


# ---------------------------------------------------------------------
# P4 — reconciliation snapshots (before/after) + row writers
# ---------------------------------------------------------------------

def snapshot_orders(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT count(*), COALESCE(sum(total_amount),0),
                  count(*) FILTER (WHERE order_status='CANCELLED')
           FROM core.fact_order WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;""",
        (shop_id, business_date),
    )
    order_count, gmv, cancel_count = cur.fetchone()
    cur.execute(
        "SELECT count(*) FROM core.fact_order_item WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    units = cur.fetchone()[0]  # 1 unit per line_item, see P2B field mapping
    return {"order_count": order_count, "gmv": gmv, "cancel_count": cancel_count, "units": units}


def snapshot_finance(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT settlement_type, count(*) FROM core.fact_settlement
           WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s GROUP BY settlement_type;""",
        (shop_id, business_date),
    )
    by_type = dict(cur.fetchall())
    total = sum(by_type.values())
    return {
        "total": total, "matched": by_type.get("MATCHED", 0),
        "not_settled_yet": by_type.get("NOT_SETTLED_YET", 0), "unknown": by_type.get("UNKNOWN", 0),
    }


def snapshot_returns(cur, shop_id, business_date) -> int:
    cur.execute(
        "SELECT count(*) FROM core.fact_return_refund WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    return cur.fetchone()[0]


def snapshot_affiliate(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT count(*), COALESCE(sum(affiliate_attributed_gmv),0),
                  COALESCE(sum(estimated_commission),0), COALESCE(sum(settled_commission),0)
           FROM core.fact_affiliate_daily WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;""",
        (shop_id, business_date),
    )
    count, gmv, est_comm, settled_comm = cur.fetchone()
    return {"count": count, "gmv": gmv, "est_comm": est_comm, "settled_comm": settled_comm}


def snapshot_product_analytics(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT count(*), COALESCE(sum(impressions),0), COALESCE(sum(clicks),0), COALESCE(sum(attributed_orders),0)
           FROM core.fact_product_analytics_daily WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;""",
        (shop_id, business_date),
    )
    count, impressions, clicks, attributed_orders = cur.fetchone()
    return {"count": count, "impressions": impressions, "clicks": clicks, "attributed_orders": attributed_orders}


def snapshot_live(cur, shop_id, business_date) -> int:
    cur.execute(
        "SELECT count(*) FROM core.fact_live_daily WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    return cur.fetchone()[0]


def reconcile_domain(cur, domain, shop_id, business_date, window_start, window_end, etl_run_id, log):
    """Runs the SAME handler used for incremental ingestion, bracketed by
    a before/after DB snapshot, and writes audit.reconciliation_result
    rows. Returns (recon_row_summaries, worker_result)."""
    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE channel='TIKTOK' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    is_new_date = cur.fetchone()[0] == 0
    date_basis = "create_time-derived business_date (Asia/Ho_Chi_Minh)"
    recon_rows = []

    if domain == "orders":
        before = snapshot_orders(cur, shop_id, business_date)
        result = run_orders(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_orders(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["order"]["inserted"], result["order"]["updated"])
        extra = {"ROWS_INSERTED": result["order"]["inserted"], "ROWS_UPDATED": result["order"]["updated"]}
        recon_rows.append(ic.recon_row(cur, business_date, "orders_count", "TIKTOK", shop_id,
            before["order_count"], after["order_count"], "count(*) core.fact_order WHERE channel=TIKTOK AND business_date=D",
            date_basis, "all statuses", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_units", "TIKTOK", shop_id,
            before["units"], after["units"], "count(line_items) core.fact_order_item WHERE business_date=D (1 unit/line_item)",
            date_basis, "all statuses", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_gmv", "TIKTOK", shop_id,
            before["gmv"], after["gmv"],
            "sum(payment.total_amount) core.fact_order WHERE business_date=D — Platform Order GMV all statuses, NOT settlement, NOT HH Net Sales",
            date_basis, "all statuses", "VND", etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_cancel_count", "TIKTOK", shop_id,
            before["cancel_count"], after["cancel_count"], "count(*) WHERE order_status='CANCELLED'",
            date_basis, "CANCELLED", None, etl_run_id, is_new_date, override, extra))
    elif domain == "finance":
        before = snapshot_finance(cur, shop_id, business_date)
        result = run_finance(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_finance(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["settlement"]["inserted"], result["settlement"]["updated"])
        extra = {"ROWS_INSERTED": result["settlement"]["inserted"], "ROWS_UPDATED": result["settlement"]["updated"]}
        recon_rows.append(ic.recon_row(cur, business_date, "settlement_count", "TIKTOK", shop_id,
            before["total"], after["total"], "count(*) core.fact_settlement WHERE business_date=D",
            "business_date=D", "all settlement_type", None, etl_run_id, is_new_date, override, extra))
        # Section 8: NOT_SETTLED -> SETTLED (MATCHED) transitions are the
        # specific thing this metric exists to catch; settlement_amount
        # stays NULL (never 0) on NOT_SETTLED_YET rows — verified in DQ (§13).
        recon_rows.append(ic.recon_row(cur, business_date, "settlement_matched_count", "TIKTOK", shop_id,
            before["matched"], after["matched"], "count(*) WHERE settlement_type='MATCHED'",
            "business_date=D", "MATCHED", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "settlement_not_settled_count", "TIKTOK", shop_id,
            before["not_settled_yet"], after["not_settled_yet"], "count(*) WHERE settlement_type='NOT_SETTLED_YET'",
            "business_date=D", "NOT_SETTLED_YET", None, etl_run_id, is_new_date, override, extra))
    elif domain == "returns":
        before = snapshot_returns(cur, shop_id, business_date)
        result = run_returns(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_returns(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["return"]["inserted"], result["return"]["updated"])
        # Section 9: TikTok zero returns is valid only if the real API
        # confirmed zero for this date — result["status"] is collect.py's
        # own paginate() status (PASS_EMPTY = a genuine, complete, empty
        # response; FAIL_* would have raised inside run_returns already).
        recon_rows.append(ic.recon_row(cur, business_date, "returns_count", "TIKTOK", shop_id,
            before, after, "count(*) core.fact_return_refund WHERE business_date=D (create_time window)",
            "create_time (Asia/Ho_Chi_Minh)", "all", None, etl_run_id, is_new_date, override,
            extra_details={"API_STATUS": result.get("status")}))
    elif domain == "affiliate":
        before = snapshot_affiliate(cur, shop_id, business_date)
        result = run_affiliate(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_affiliate(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["affiliate"]["inserted"], result["affiliate"]["updated"])
        extra = {"ROWS_INSERTED": result["affiliate"]["inserted"], "ROWS_UPDATED": result["affiliate"]["updated"]}
        recon_rows.append(ic.recon_row(cur, business_date, "affiliate_count", "TIKTOK", shop_id,
            before["count"], after["count"], "count(*) core.fact_affiliate_daily WHERE business_date=D",
            date_basis, "all settlement_status", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "affiliate_estimated_commission", "TIKTOK", shop_id,
            before["est_comm"], after["est_comm"], "sum(estimated_commission) WHERE business_date=D",
            date_basis, "all", "VND", etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "affiliate_settled_commission", "TIKTOK", shop_id,
            before["settled_comm"], after["settled_comm"],
            "sum(settled_commission) WHERE business_date=D — only settlement_status='SETTLED' lines, NULL elsewhere (never substituted)",
            date_basis, "SETTLED", "VND", etl_run_id, is_new_date, override, extra))
    elif domain == "product_analytics":
        before = snapshot_product_analytics(cur, shop_id, business_date)
        result = run_product_analytics(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=business_date)
        after = snapshot_product_analytics(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["product_analytics"]["inserted"], result["product_analytics"]["updated"])
        extra = {"ROWS_INSERTED": result["product_analytics"]["inserted"], "ROWS_UPDATED": result["product_analytics"]["updated"]}
        recon_rows.append(ic.recon_row(cur, business_date, "product_analytics_count", "TIKTOK", shop_id,
            before["count"], after["count"], "count(*) core.fact_product_analytics_daily WHERE business_date=D",
            "requested Ads-Analytics date=D", "all", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "product_analytics_attributed_orders", "TIKTOK", shop_id,
            before["attributed_orders"], after["attributed_orders"],
            "sum(attributed_orders) WHERE business_date=D — Product-Analytics attribution, NOT platform order count",
            "requested Ads-Analytics date=D", "all", None, etl_run_id, is_new_date, override, extra))
    elif domain == "live":
        before = snapshot_live(cur, shop_id, business_date)
        result = run_live(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=business_date,
                           account_type="ALL")
        after = snapshot_live(cur, shop_id, business_date)
        # Section 11: only override to SOURCE_LATENCY when the API itself
        # reported it — never inferred from a zero count alone (a
        # historical date can legitimately have zero LIVE sessions).
        override = (
            "SOURCE_LATENCY" if result.get("latency_status") == "LATENCY_DATA_NOT_YET_AVAILABLE"
            else ic.classify_from_counts(result["live"]["inserted"], result["live"]["updated"])
        )
        recon_rows.append(ic.recon_row(cur, business_date, "live_session_count", "TIKTOK", shop_id,
            before, after, "count(*) core.fact_live_daily WHERE business_date=D",
            "requested LIVE-Analytics date=D", "all", None, etl_run_id, is_new_date,
            override_status=override, extra_details={"API_STATUS": result.get("status")}))
    else:
        raise ValueError(f"P4 reconciliation not implemented for TikTok domain {domain!r}")

    return recon_rows, result


def main() -> int:
    domain = sys.argv[1]
    window_start = datetime.fromisoformat(sys.argv[2])
    window_end = datetime.fromisoformat(sys.argv[3])
    etl_run_id = sys.argv[4]
    mode = sys.argv[5] if len(sys.argv) > 5 else "incremental"
    business_date_arg = sys.argv[6] if len(sys.argv) > 6 else None

    shop_info = pilot_common.load_shop_info() or {}
    shop_id = shop_info.get("shop_id")
    if not shop_id:
        print(json.dumps({"status": "FAIL", "error": "TikTok shop_id not found (shop_info.json)"}))
        return 1

    log = ic.simple_log(f"TIKTOK/{domain}")

    if mode != "reconcile" and domain == "orders":
        # P11-LAST-MILE — durable per-chunk catch-up: every chunk commits
        # its own rows + sync_state on its own connection, so nothing is
        # left for this function to commit (and nothing earlier can be
        # rolled back by a later failure or an external kill).
        try:
            result, meta = run_orders_incremental(etl_run_id, shop_id, window_start, window_end, log)
        except Exception as e:  # noqa: BLE001 — e.g. bootstrap_session() failed before any chunk
            print(json.dumps({"status": "FAIL", "error": str(e)}, default=str))
            return 1
        catchup = {k: v for k, v in meta.items() if k not in ("status", "error", "totals")}
        out = {"status": meta["status"], "result": result, "catchup": catchup}
        if meta["status"] == ORDERS_STATUS_FAIL:
            out["error"] = meta["error"]
        print(json.dumps(out, default=str))
        return 1 if meta["status"] == ORDERS_STATUS_FAIL else 0

    conn = ic.get_db_conn()
    cur = conn.cursor()
    try:
        if mode == "reconcile":
            business_date = datetime.strptime(business_date_arg, "%Y-%m-%d").date()
            recon_rows, result = reconcile_domain(cur, domain, shop_id, business_date, window_start, window_end, etl_run_id, log)
            # P4 deliberately does NOT touch control.etl_sync_state — see
            # Section 2 ("do not damage P3 watermarks").
            conn.commit()
            conn.close()
            print(json.dumps({"status": "PASS", "result": result, "recon_rows": recon_rows}, default=str))
            return 0

        result = HANDLERS[domain](cur, etl_run_id, shop_id, window_start, window_end, log)
        ic.upsert_sync_state(cur, "TIKTOK", domain, shop_id, window_end, ic.vn_date(window_end), "success")
        conn.commit()
        conn.close()
        print(json.dumps({"status": "PASS", "result": result}, default=str))
        return 0
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        conn.close()
        print(json.dumps({"status": "FAIL", "error": str(e)}, default=str))
        return 1


if __name__ == "__main__":
    sys.exit(main())
