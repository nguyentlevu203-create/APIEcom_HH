#!/usr/bin/env python3
"""P6B.1 — Shopee historical backfill for 2026-09-01..2026-09-11.

Two gaps closed here, both discovered while investigating P6B:

1. FINANCE/ESCROW PERSISTENCE GAP (the primary P6B.1 objective): Shopee's
   get_escrow_detail response already returns every field HH's Net
   Sales/CM1 formula needs (see reports/HH_SHOPEE_PNL_FIELD_MAPPING_V1.md),
   but production ingestion (incr_worker.py's run_finance) only ever
   persisted the single blended settlement_amount. Fixed in incr_worker.py
   itself (same shared P3/P4 code path) to persist the full field set —
   this script backfills it historically using that SAME fixed function.

2. ORDER-DISCOVERY GAP (same root cause proven for TikTok): Shopee's order
   discovery (paginated_order_sns) filters by UPDATE_TIME, which misses
   orders created in the target window whose status later changed on a
   different day (see incr_worker.py comment: "catches status CHANGES on
   existing orders too" — correct for live incremental polling, wrong for
   reconstructing a historical date's true order population). This script
   discovers the TRUE order_sn population for 2026-09-01..09-11 by
   CREATE_TIME instead, backfills any orders missing from core.fact_order/
   core.fact_order_item, then fetches escrow for every order_sn in range.

Does NOT call control.etl_sync_state — kept separate from the normal P3
incremental watermark per Section 16. Logged as its own
control.etl_run_log row, run_type='historical_backfill_p6b1'.

    python3 p6b1_historical_backfill.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SHOPEE_DIR = Path(__file__).resolve().parent.parent
PIPELINES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pipelines"
sys.path.insert(0, str(SHOPEE_DIR))
sys.path.insert(0, str(PIPELINES_DIR))

from shopee_client import ShopeeClient  # noqa: E402
from keychain import get_secret, ACCOUNT_SHOP_ID  # noqa: E402
import incr_common as ic  # noqa: E402
from incr_worker import resolve_shopee_item_sku, shopee_check  # noqa: E402

VN_TZ = timezone(timedelta(hours=7))
WINDOW_START = datetime(2026, 9, 1, 0, 0, tzinfo=VN_TZ)
WINDOW_END = datetime(2026, 9, 12, 0, 0, tzinfo=VN_TZ)  # exclusive, covers through 09-11


def simple_log(prefix):
    def _log(msg):
        print(f"[{prefix}] {msg}", flush=True)
    return _log


def discover_order_sns_by_create_time(client, log):
    order_sns = []
    cursor = ""
    page = 0
    while True:
        page += 1
        params = {
            "time_range_field": "create_time",
            "time_from": int(WINDOW_START.timestamp()), "time_to": int(WINDOW_END.timestamp()),
            "page_size": 100, "response_optional_fields": "order_status",
        }
        if cursor:
            params["cursor"] = cursor

        def call():
            return shopee_check(client.get("/api/v2/order/get_order_list", params))
        resp = ic.with_backoff(call, "shopee p6b1 order_list (create_time)", log)
        body = resp.get("response", {})
        page_rows = body.get("order_list", [])
        order_sns.extend([o["order_sn"] for o in page_rows])
        log(f"order_list (create_time) page {page}: rows={len(page_rows)}")
        more = body.get("more", False)
        cursor = body.get("next_cursor", "")
        if not more or not cursor:
            break
        time.sleep(0.3)
    return order_sns


def fetch_order_details(client, order_sns, log):
    details = []
    for i in range(0, len(order_sns), 50):
        batch = order_sns[i:i + 50]

        def call(batch=batch):
            return shopee_check(client.get("/api/v2/order/get_order_detail", {
                "order_sn_list": ",".join(batch),
                "response_optional_fields": (
                    "order_status,total_amount,currency,item_list,payment_method,"
                    "cancel_by,cancel_reason,create_time,update_time"
                ),
            }))
        resp = ic.with_backoff(call, "shopee p6b1 order_detail", log)
        details.extend(resp.get("response", {}).get("order_list", []))
        time.sleep(0.3)
    return details


def upsert_orders(cur, etl_run_id, shop_id, details):
    order_ctr, item_ctr = ic.Counters(), ic.Counters()
    for od in details:
        sn = od["order_sn"]
        bd = ic.ts_from_epoch(od.get("create_time"))
        bd = bd.astimezone(VN_TZ).date() if bd else None
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
                "SHOPEE", shop_id, sn, od.get("order_status"),
                ic.ts_from_epoch(od.get("create_time")), ic.ts_from_epoch(od.get("update_time")),
                od.get("currency"), od.get("total_amount"), bd,
                "SHOPEE", sn, ic.ts_from_epoch(od.get("create_time")), ic.ts_from_epoch(od.get("update_time")),
                "/api/v2/order/get_order_detail (p6b1_historical_backfill, create_time-filtered)", shop_id, etl_run_id,
            ),
        )
        order_ctr.record(cur.fetchone()[0])

        for it in od.get("item_list", []):
            order_item_id = str(it.get("line_item_id"))
            qty = it.get("model_quantity_purchased")
            unit_price = it.get("model_discounted_price")
            item_amount = (qty * unit_price) if (qty is not None and unit_price is not None) else None
            resolved_sku, _evidence = resolve_shopee_item_sku(it)
            item_id = str(it["item_id"]) if it.get("item_id") is not None else None
            model_id = str(it["model_id"]) if it.get("model_id") is not None else None
            item_sku = (it.get("item_sku") or "").strip() or None
            model_sku = (it.get("model_sku") or "").strip() or None
            cur.execute(
                """
                INSERT INTO core.fact_order_item (
                    channel, shop_id, order_id, order_item_id, sku, product_name,
                    qty, unit_price, item_amount, business_date,
                    item_id, model_id, item_sku, model_sku,
                    source_system, source_record_id, source_created_at, source_updated_at,
                    source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, shop_id, order_id, order_item_id) DO UPDATE SET
                    qty = EXCLUDED.qty, unit_price = EXCLUDED.unit_price,
                    item_amount = EXCLUDED.item_amount, source_updated_at = EXCLUDED.source_updated_at,
                    sku = EXCLUDED.sku, item_id = EXCLUDED.item_id, model_id = EXCLUDED.model_id,
                    item_sku = EXCLUDED.item_sku, model_sku = EXCLUDED.model_sku,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    "SHOPEE", shop_id, sn, order_item_id, resolved_sku, it.get("item_name"),
                    qty, unit_price, item_amount, bd,
                    item_id, model_id, item_sku, model_sku,
                    "SHOPEE", order_item_id, ic.ts_from_epoch(od.get("create_time")), ic.ts_from_epoch(od.get("update_time")),
                    "/api/v2/order/get_order_detail (p6b1_historical_backfill)", shop_id, etl_run_id,
                ),
            )
            item_ctr.record(cur.fetchone()[0])
    return {"order": order_ctr.as_dict(), "order_item": item_ctr.as_dict()}


def backfill_finance(conn, cur, etl_run_id, shop_id, order_sns, log):
    client = ShopeeClient()
    ctr = ic.Counters()
    for i, sn in enumerate(order_sns, start=1):
        def call(sn=sn):
            resp = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": sn})
            if resp.get("_http_status") in ic.TRANSIENT_HTTP:
                raise ic.TransientHTTPError(str(resp.get("_http_status")))
            return resp
        e = ic.with_backoff(call, f"shopee p6b1 escrow {sn}", log)
        err = e.get("error", "")
        http = e.get("_http_status")
        order_income = e.get("response", {}).get("order_income") if not err and http == 200 else None
        detail = {}
        if err or http != 200:
            settlement_type, settlement_amount = "api_error", None
        elif order_income:
            settlement_type = "escrow_estimate"
            settlement_amount = order_income.get("escrow_amount_after_adjustment") or order_income.get("escrow_amount")
            detail = {k: order_income.get(k) for k in (
                "order_original_price", "seller_discount", "voucher_from_seller", "voucher_from_shopee",
                "seller_return_refund", "drc_adjustable_refund", "seller_lost_compensation",
                "commission_fee", "service_fee", "seller_transaction_fee",
                "actual_shipping_fee", "shopee_shipping_rebate", "buyer_paid_shipping_fee",
                "order_ams_commission_fee", "ads_escrow_top_up_fee_or_technical_support_fee",
            )}
        else:
            settlement_type, settlement_amount = "api_error", None

        cur.execute(
            """
            INSERT INTO core.fact_settlement (
                channel, shop_id, order_id, settlement_id, settlement_amount,
                settlement_type, settlement_time, business_date,
                order_original_price, seller_discount, voucher_from_seller, voucher_from_shopee,
                seller_return_refund, drc_adjustable_refund, seller_lost_compensation,
                commission_fee, service_fee, seller_transaction_fee,
                actual_shipping_fee, shopee_shipping_rebate, buyer_paid_shipping_fee,
                order_ams_commission_fee, ads_escrow_top_up_fee_or_technical_support_fee,
                source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, settlement_id) DO UPDATE SET
                settlement_amount = EXCLUDED.settlement_amount, settlement_type = EXCLUDED.settlement_type,
                order_original_price = EXCLUDED.order_original_price, seller_discount = EXCLUDED.seller_discount,
                voucher_from_seller = EXCLUDED.voucher_from_seller, voucher_from_shopee = EXCLUDED.voucher_from_shopee,
                seller_return_refund = EXCLUDED.seller_return_refund, drc_adjustable_refund = EXCLUDED.drc_adjustable_refund,
                seller_lost_compensation = EXCLUDED.seller_lost_compensation,
                commission_fee = EXCLUDED.commission_fee, service_fee = EXCLUDED.service_fee,
                seller_transaction_fee = EXCLUDED.seller_transaction_fee,
                actual_shipping_fee = EXCLUDED.actual_shipping_fee, shopee_shipping_rebate = EXCLUDED.shopee_shipping_rebate,
                buyer_paid_shipping_fee = EXCLUDED.buyer_paid_shipping_fee,
                order_ams_commission_fee = EXCLUDED.order_ams_commission_fee,
                ads_escrow_top_up_fee_or_technical_support_fee = EXCLUDED.ads_escrow_top_up_fee_or_technical_support_fee,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "SHOPEE", shop_id, sn, sn, settlement_amount, settlement_type, ic.vn_date(WINDOW_END),
                detail.get("order_original_price"), detail.get("seller_discount"),
                detail.get("voucher_from_seller"), detail.get("voucher_from_shopee"),
                detail.get("seller_return_refund"), detail.get("drc_adjustable_refund"),
                detail.get("seller_lost_compensation"), detail.get("commission_fee"), detail.get("service_fee"),
                detail.get("seller_transaction_fee"), detail.get("actual_shipping_fee"),
                detail.get("shopee_shipping_rebate"), detail.get("buyer_paid_shipping_fee"),
                detail.get("order_ams_commission_fee"), detail.get("ads_escrow_top_up_fee_or_technical_support_fee"),
                "SHOPEE", e.get("request_id"), "/api/v2/payment/get_escrow_detail (p6b1_historical_backfill)",
                shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
        if i % 50 == 0:
            conn.commit()  # avoid holding one long transaction across ~1000 API calls (Neon reliability)
            log(f"finance backfill: {i}/{len(order_sns)} (committed)")
        time.sleep(0.15)
    conn.commit()
    return {"settlement": ctr.as_dict()}


def main():
    log = simple_log("P6B1_SHOPEE")
    shop_id = get_secret(ACCOUNT_SHOP_ID)
    client = ShopeeClient()

    log("discovering true order population by CREATE_TIME (not update_time)...")
    order_sns = discover_order_sns_by_create_time(client, log)
    order_sns = sorted(set(order_sns))
    log(f"discovered {len(order_sns)} distinct order_sns created 2026-09-01..2026-09-11")

    conn = ic.get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT order_id FROM core.fact_order WHERE channel='SHOPEE' AND order_id = ANY(%s);", (order_sns,))
    already_in_db = {r[0] for r in cur.fetchall()}
    missing_sns = [sn for sn in order_sns if sn not in already_in_db]
    log(f"already in core.fact_order: {len(already_in_db)}; missing: {len(missing_sns)}")

    etl_run_id = ic.start_run_log(cur, "SHOPEE", "historical_backfill_p6b1", shop_id,
                                   run_type="historical_backfill_p6b1")
    conn.commit()

    result = {"discovered": len(order_sns), "already_in_db": len(already_in_db), "missing": len(missing_sns)}
    try:
        if missing_sns:
            log(f"fetching order_detail for {len(missing_sns)} missing orders...")
            details = fetch_order_details(client, missing_sns, log)
            order_result = upsert_orders(cur, etl_run_id, shop_id, details)
            conn.commit()
            result["order_backfill"] = order_result
        else:
            result["order_backfill"] = {"skipped": "no missing orders"}

        log(f"backfilling finance/escrow for all {len(order_sns)} orders in range...")
        finance_result = backfill_finance(conn, cur, etl_run_id, shop_id, order_sns, log)
        result["finance_backfill"] = finance_result

        ic.finish_run_log("SHOPEE", "historical_backfill_p6b1", etl_run_id, "success",
                           rows_processed=len(missing_sns) + len(order_sns))
        result["status"] = "success"
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        ic.finish_run_log("SHOPEE", "historical_backfill_p6b1", etl_run_id, "fail", error_message=str(e)[:2000])
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
