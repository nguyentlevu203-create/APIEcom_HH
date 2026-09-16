#!/usr/bin/env python3
"""P6B.1 — TikTok historical backfill for 2026-09-01..2026-09-11.

Root cause of the gap P6B found (19/62 orders missing from core.fact_order
for 2026-09-01): P3's incremental worker and P4's reconciliation both
discover candidate orders via TikTok's order/search filtered by
UPDATE_TIME within the target window. That is correct for catching status
CHANGES on already-known orders, but wrong for discovering the TRUE
population of orders CREATED on a historical date — any order whose
create_time is on that date but whose update_time later drifted to a
different day (e.g. DELIVERED confirmed days later, or CANCELLED days
later) is invisible to any update_time-windowed query that only covers
the original date. Confirmed empirically: the first 5 orders inspected
from the 2026-09-01 historical CSV all have create_time=09-01 but
update_time on 09-04/09-05.

Fix (this script only — a dedicated, separate backfill path, per Section
16 kept apart from the normal incremental watermark):
  1. Discover the TRUE order population for 2026-09-01..09-11 by querying
     order/202309/orders/search with CREATE_TIME_GE/LT (not update_time).
  2. Fetch full order_detail for every discovered order_id (same call/
     fields as incr_worker.py's run_orders — reused verbatim).
  3. Backfill finance (finance_order_statement_transactions) for every
     order_id in the range — refreshes settlement status for orders that
     may have settled since P6B's snapshot.
  4. UPSERT into core.fact_order / core.fact_order_item / core.fact_settlement
     using the exact same INSERT/ON CONFLICT statements incr_worker.py
     already uses (no schema change, no new write path).
  5. Does NOT call control.etl_sync_state — the P3 incremental watermark
     is untouched. Logged as its own control.etl_run_log row,
     run_type='historical_backfill_p6b1'.

    python3 p6b1_historical_backfill.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

PILOT_DIR = Path(__file__).resolve().parent
PIPELINES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pipelines"
sys.path.insert(0, str(PIPELINES_DIR))
sys.path.insert(0, str(PILOT_DIR))

import pilot_common  # noqa: E402
import collect as tt_collect  # noqa: E402
import incr_common as ic  # noqa: E402

WINDOW_START = datetime(2026, 9, 1, 0, 0, tzinfo=ic.VN_TZ)
WINDOW_END = datetime(2026, 9, 12, 0, 0, tzinfo=ic.VN_TZ)  # exclusive, covers through 09-11
SHOP_ID = None  # filled from shop_info at runtime


def simple_log(prefix):
    def _log(msg):
        print(f"[{prefix}] {msg}", flush=True)
    return _log


def discover_orders_by_create_time(session, log):
    rows, status = tt_collect.paginate(
        _Log(log), session, "orders", "p6b1_backfill", "p6b1_orders_by_create_time",
        query_base={"page_size": "50"},
        body_base={
            "create_time_ge": int(WINDOW_START.timestamp()),
            "create_time_lt": int(WINDOW_END.timestamp()),
        },
        max_pages=200,
    )
    if status.startswith("FAIL"):
        raise RuntimeError(f"order discovery failed: {status}")
    return [r["id"] for r in rows if r.get("id")]


class _Log:
    def __init__(self, log_fn):
        self._log = log_fn

    def log(self, msg):
        self._log(msg)


def fetch_order_details(session, order_ids, log):
    details = []
    for i in range(0, len(order_ids), 50):
        chunk = order_ids[i:i + 50]

        def call(chunk=chunk):
            return session.client.read_domain(
                "order_detail", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"),
                extra_query={"ids": ",".join(chunk)},
            )
        resp = ic.with_backoff(call, "tiktok p6b1 order_detail", log)
        if resp.ok:
            details.extend(resp.data.get("orders", []))
        else:
            raise RuntimeError(f"order_detail failed: code={resp.code} message={resp.message}")
        time.sleep(0.15)
    return details


def upsert_orders(cur, etl_run_id, shop_id, details, log):
    order_ctr, item_ctr = ic.Counters(), ic.Counters()
    for o in details:
        oid = o.get("id")
        payment = o.get("payment") or {}
        bd = ic.ts_from_epoch(o.get("create_time"))
        bd = bd.astimezone(ic.VN_TZ).date() if bd else None
        if bd is None:
            continue
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
                "/order/202309/orders (p6b1_historical_backfill, create_time-filtered)", shop_id, etl_run_id,
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
                    "/order/202309/orders (p6b1_historical_backfill)", shop_id, etl_run_id,
                ),
            )
            item_ctr.record(cur.fetchone()[0])
    return {"order": order_ctr.as_dict(), "order_item": item_ctr.as_dict()}


def backfill_finance(conn, cur, etl_run_id, shop_id, order_ids, log):
    session = pilot_common.bootstrap_session()
    ctr = ic.Counters()
    for i, oid in enumerate(order_ids, start=1):
        def call(oid=oid):
            resp = session.client.read_domain(
                "finance_order_statement_transactions", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"), path_params={"order_id": oid},
            )
            # A bulk backfill of ~1000 calls can trigger TikTok's downstream
            # rate limit (code=36009002) — confirmed transient (an
            # immediate live re-check of an affected order succeeded) — so
            # retry it via with_backoff instead of recording a hard UNKNOWN.
            if not resp.ok:
                raise ic.TransientHTTPError(f"code={resp.code} message={resp.message}")
            return resp
        resp = ic.with_backoff(call, f"tiktok p6b1 finance {oid}", log)
        has_real = bool(resp.data.get("sku_transactions"))
        if has_real:
            d = resp.data
            status, settlement_amount = "MATCHED", ic.dec(d.get("settlement_amount"))
            revenue, fee, ship, currency = (
                ic.dec(d.get("revenue_amount")), ic.dec(d.get("fee_and_tax_amount")),
                ic.dec(d.get("shipping_cost_amount")), d.get("currency"),
            )
        elif resp.ok:
            status, settlement_amount, revenue, fee, ship, currency = "NOT_SETTLED_YET", None, None, None, None, None
        else:
            status, settlement_amount, revenue, fee, ship, currency = "UNKNOWN", None, None, None, None, None

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
                "TIKTOK", shop_id, oid, oid, settlement_amount, status,
                ic.vn_date(ic.now_utc()), currency, revenue, fee, ship,
                "TIKTOK", oid, "/finance/202501/orders/{order_id}/statement_transactions (p6b1_historical_backfill)",
                shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
        if i % 50 == 0:
            conn.commit()  # avoid holding one long transaction across ~1000 API calls (Neon reliability)
            log(f"finance backfill: {i}/{len(order_ids)} (committed)")
        time.sleep(0.1)
    conn.commit()
    return {"settlement": ctr.as_dict()}


def main():
    log = simple_log("P6B1_TIKTOK")
    session = pilot_common.bootstrap_session()
    shop_id = session.shop_info.get("shop_id")

    log("discovering true order population by CREATE_TIME (not update_time)...")
    order_ids = discover_orders_by_create_time(session, log)
    log(f"discovered {len(order_ids)} distinct order_ids created 2026-09-01..2026-09-11")

    conn = ic.get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT order_id FROM core.fact_order WHERE channel='TIKTOK' AND order_id = ANY(%s);", (order_ids,))
    already_in_db = {r[0] for r in cur.fetchall()}
    missing_ids = [oid for oid in order_ids if oid not in already_in_db]
    log(f"already in core.fact_order: {len(already_in_db)}; missing: {len(missing_ids)}")

    etl_run_id = ic.start_run_log(cur, "TIKTOK", "historical_backfill_p6b1", shop_id,
                                   run_type="historical_backfill_p6b1")
    conn.commit()

    result = {"discovered": len(order_ids), "already_in_db": len(already_in_db), "missing": len(missing_ids)}
    try:
        if missing_ids:
            log(f"fetching order_detail for {len(missing_ids)} missing orders...")
            details = fetch_order_details(session, missing_ids, log)
            order_result = upsert_orders(cur, etl_run_id, shop_id, details, log)
            conn.commit()
            result["order_backfill"] = order_result
        else:
            result["order_backfill"] = {"skipped": "no missing orders"}

        log(f"backfilling finance for all {len(order_ids)} orders in range...")
        finance_result = backfill_finance(conn, cur, etl_run_id, shop_id, order_ids, log)
        result["finance_backfill"] = finance_result

        ic.finish_run_log("TIKTOK", "historical_backfill_p6b1", etl_run_id, "success",
                           rows_processed=len(missing_ids) + len(order_ids))
        result["status"] = "success"
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        ic.finish_run_log("TIKTOK", "historical_backfill_p6b1", etl_run_id, "fail", error_message=str(e)[:2000])
        result["status"] = "fail"
        result["error"] = str(e)
        conn.close()
        print(json.dumps(result, indent=2, default=str))
        raise

    conn.close()
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
