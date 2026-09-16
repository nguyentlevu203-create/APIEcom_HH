#!/usr/bin/env python3
"""
P2A — Shopee -> Neon PostgreSQL (hh_ecom) controlled load for business
date 2026-09-08.

Reuses the existing, proven ShopeeClient (GET-only, HMAC-signed) as-is —
no changes to shopee_client.py. This script is an adapter/ETL layer on
top of it: fetch (via the same endpoints already proven in
pull_2026_09_08.py / complete_finance_2026_09_08.py), transform, and
UPSERT into hh_ecom via hh_etl_writer.

Security:
- DB credentials come only from the macOS Keychain (service
  HH_ECOM_NEON, key hh_etl_writer_database_url) via `keyring`.
- Shopee credentials come only from the existing keychain.py mechanism
  used by ShopeeClient — never read or printed directly here.
- Nothing in this file ever prints a password, full DATABASE_URL,
  Partner Key, access_token, or refresh_token.

Run twice in a row to test UPSERT idempotency (see __main__).
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import keyring
import psycopg2
import psycopg2.extras

SHOPEE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHOPEE_DIR))
from shopee_client import ShopeeClient  # noqa: E402
from keychain import get_secret, ACCOUNT_SHOP_ID  # noqa: E402
from incr_worker import resolve_shopee_item_sku  # noqa: E402 -- single source of truth, see P5C

PILOT_DIR = Path(__file__).resolve().parent
RAW_DIR = PILOT_DIR / "raw" / "p2a_2026-09-08"
RAW_DIR.mkdir(parents=True, exist_ok=True)

VN_TZ = timezone(timedelta(hours=7))
BUSINESS_DATE_STR = "2026-09-08"
DAY_START = datetime(2026, 9, 8, 0, 0, 0, tzinfo=VN_TZ)
DAY_END = datetime(2026, 9, 9, 0, 0, 0, tzinfo=VN_TZ)
TS_START = int(DAY_START.timestamp())
TS_END = int(DAY_END.timestamp())

CHANNEL = "SHOPEE"
SOURCE_SYSTEM = "SHOPEE"


def vn_date_from_epoch(epoch: Optional[int]):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(VN_TZ).date()


def ts_from_epoch(epoch: Optional[int]):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def save_raw(name: str, data: Any) -> None:
    with open(RAW_DIR / name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def get_db_conn():
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    if not url:
        raise RuntimeError("hh_etl_writer_database_url missing from Keychain")
    # TCP keepalives: the run spends long stretches (order/escrow/item
    # pagination against Shopee) with the DB socket otherwise fully idle,
    # which Neon's connection layer has been observed to drop
    # ("SSL connection has been closed unexpectedly") — keepalives keep
    # the socket alive across those gaps.
    conn = psycopg2.connect(
        url,
        keepalives=1,
        keepalives_idle=20,
        keepalives_interval=10,
        keepalives_count=3,
    )
    del url
    return conn


# =====================================================================
# FETCH — reusing proven endpoints from pull_2026_09_08.py /
# complete_finance_2026_09_08.py, but with FULL pagination/population
# (no sampling, no arbitrary caps).
# =====================================================================

def fetch_all_order_sns(client: ShopeeClient) -> list[str]:
    order_sns: list[str] = []
    cursor = ""
    pages = []
    while True:
        params = {
            "time_range_field": "create_time",
            "time_from": TS_START,
            "time_to": TS_END,
            "page_size": 100,
            "response_optional_fields": "order_status",
        }
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/api/v2/order/get_order_list", params)
        order_list = resp.get("response", {}).get("order_list", [])
        pages.append({
            "http": resp.get("_http_status"), "error": resp.get("error"),
            "count": len(order_list), "cursor_used": cursor,
        })
        order_sns.extend([o["order_sn"] for o in order_list])
        more = resp.get("response", {}).get("more", False)
        cursor = resp.get("response", {}).get("next_cursor", "")
        if not more or not cursor:
            break
        time.sleep(0.3)
    save_raw("order_list_pages.json", pages)
    save_raw("order_sns.json", order_sns)
    return order_sns


def fetch_order_details(client: ShopeeClient, order_sns: list[str]) -> list[dict]:
    order_details = []
    for i in range(0, len(order_sns), 50):
        batch = order_sns[i:i + 50]
        resp = client.get("/api/v2/order/get_order_detail", {
            "order_sn_list": ",".join(batch),
            "response_optional_fields": (
                "order_status,total_amount,currency,item_list,payment_method,"
                "cancel_by,cancel_reason,create_time,update_time"
            ),
        })
        order_details.extend(resp.get("response", {}).get("order_list", []))
        time.sleep(0.3)
    save_raw("order_detail.json", order_details)
    return order_details


def fetch_all_escrow(client: ShopeeClient, order_sns: list[str]) -> dict[str, dict]:
    by_sn: dict[str, dict] = {}
    for sn in order_sns:
        attempt = 0
        while True:
            attempt += 1
            resp = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": sn})
            err = resp.get("error", "")
            http = resp.get("_http_status")
            if not err and http == 200:
                break
            low = str(err).lower()
            msg_low = str(resp.get("message", "")).lower()
            is_transient = "rate" in low or "limit" in low or http == 429 or \
                "inner error" in msg_low or "try later" in msg_low
            if is_transient and attempt <= 5:
                backoff = min(2 ** attempt, 30)
                time.sleep(backoff)
                continue
            break
        by_sn[sn] = resp
        time.sleep(0.25)
    save_raw("escrow_detail_full.json", by_sn)
    return by_sn


def fetch_returns(client: ShopeeClient) -> dict:
    backoffs = [0, 5, 15, 40, 90, 180]
    last_resp = None
    for attempt, backoff in enumerate(backoffs, start=1):
        if backoff:
            time.sleep(backoff)
        resp = client.get("/api/v2/returns/get_return_list", {
            "create_time_from": TS_START, "create_time_to": TS_END, "page_size": 100,
        })
        last_resp = resp
        if not resp.get("error") and resp.get("_http_status") == 200:
            break
    save_raw("return_list_final.json", last_resp)
    return last_resp


def fetch_ads(client: ShopeeClient) -> dict:
    resp = client.get("/api/v2/ads/get_all_cpc_ads_daily_performance", {
        "start_date": "08-09-2026", "end_date": "08-09-2026",
    })
    save_raw("ads_daily.json", resp)
    return resp


def fetch_all_items(client: ShopeeClient) -> tuple[list[dict], bool]:
    items: list[dict] = []
    offset = 0
    page_size = 100
    pages = []
    declared_total = None
    while True:
        resp = client.get("/api/v2/product/get_item_list", {
            "offset": offset, "page_size": page_size, "item_status": "NORMAL",
        })
        body = resp.get("response", {})
        page_items = body.get("item", [])
        has_next = body.get("has_next_page", False)
        declared_total = body.get("total_count", declared_total)
        pages.append({
            "offset": offset, "n_items": len(page_items),
            "has_next_page": has_next, "total_count": declared_total,
            "http_status": resp.get("_http_status"), "error": resp.get("error"),
        })
        items.extend(page_items)
        if not has_next or not page_items:
            break
        offset += page_size
        time.sleep(0.2)
    pagination_complete = (declared_total is None) or (len(items) >= declared_total)
    save_raw("item_list_full.json", {
        "pages": pages, "declared_total_count": declared_total,
        "fetched_item_count": len(items), "pagination_complete": pagination_complete,
        "item": items,
    })
    return items, pagination_complete


def fetch_all_models(client: ShopeeClient, item_ids: list[int]) -> dict[int, dict]:
    by_item: dict[int, dict] = {}
    for item_id in item_ids:
        resp = client.get("/api/v2/product/get_model_list", {"item_id": item_id})
        by_item[item_id] = resp
        time.sleep(0.15)
    save_raw("model_list_full.json", by_item)
    return by_item


# =====================================================================
# UPSERT helpers — INSERT ... ON CONFLICT ... DO UPDATE, tracking
# inserted vs updated via RETURNING (xmax = 0) AS inserted.
# =====================================================================

class UpsertCounters:
    def __init__(self) -> None:
        self.inserted = 0
        self.updated = 0

    def record(self, was_inserted: bool) -> None:
        if was_inserted:
            self.inserted += 1
        else:
            self.updated += 1


def reconnect(old_conn):
    """Close old_conn (best-effort) and open a fresh one.

    Each DB-write section below fetches from Shopee first (which can take
    from seconds to several minutes across pagination/backoff) and only
    then touches Postgres. A connection held open across that fetch has
    been observed to die (Neon's serverless compute appears to suspend /
    drop connections that see no query activity for a while, independent
    of TCP keepalives). Opening a fresh connection right before each
    write burst sidesteps that instead of hoping the old one survived.
    """
    try:
        old_conn.close()
    except Exception:  # noqa: BLE001
        pass
    new_conn = get_db_conn()
    new_conn.autocommit = False
    return new_conn, new_conn.cursor()


def run_etl(client: ShopeeClient, conn, etl_run_id: str, shop_id: str) -> dict:
    """Full P2A ingestion for BUSINESS_DATE_STR. Returns a summary dict.
    Reconnects to Postgres before each write burst (see `reconnect`);
    commits after each section so partial progress survives a later
    section's failure. Caller owns the final `conn` handed back and
    should close it."""
    cur = conn.cursor()
    counters: dict[str, UpsertCounters] = {
        "dim_channel": UpsertCounters(), "dim_product": UpsertCounters(),
        "fact_order": UpsertCounters(), "fact_order_item": UpsertCounters(),
        "fact_settlement": UpsertCounters(), "fact_ads_daily": UpsertCounters(),
        "fact_inventory_snapshot": UpsertCounters(),
    }

    # ---- dim_channel ----
    cur.execute(
        """
        INSERT INTO core.dim_channel (channel, shop_id, shop_name, market)
        VALUES (%s, %s, %s, 'VN')
        ON CONFLICT (channel, shop_id) DO UPDATE SET shop_name = EXCLUDED.shop_name
        RETURNING (xmax = 0) AS inserted;
        """,
        (CHANNEL, shop_id, "Le Petit Marseillais Vietnam"),
    )
    counters["dim_channel"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Orders ----
    order_sns = fetch_all_order_sns(client)
    order_details = fetch_order_details(client, order_sns)

    conn, cur = reconnect(conn)
    status_by_sn: dict[str, str] = {}
    for od in order_details:
        sn = od["order_sn"]
        status_by_sn[sn] = od.get("order_status")
        business_date = vn_date_from_epoch(od.get("create_time")) or datetime.strptime(BUSINESS_DATE_STR, "%Y-%m-%d").date()
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
                CHANNEL, shop_id, sn, od.get("order_status"),
                ts_from_epoch(od.get("create_time")), ts_from_epoch(od.get("update_time")),
                od.get("currency"), od.get("total_amount"), business_date,
                SOURCE_SYSTEM, sn, ts_from_epoch(od.get("create_time")), ts_from_epoch(od.get("update_time")),
                "/api/v2/order/get_order_detail", shop_id, etl_run_id,
            ),
        )
        counters["fact_order"].record(cur.fetchone()[0])

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
                    qty = EXCLUDED.qty,
                    unit_price = EXCLUDED.unit_price,
                    item_amount = EXCLUDED.item_amount,
                    source_updated_at = EXCLUDED.source_updated_at,
                    sku = EXCLUDED.sku, item_id = EXCLUDED.item_id, model_id = EXCLUDED.model_id,
                    item_sku = EXCLUDED.item_sku, model_sku = EXCLUDED.model_sku,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, shop_id, sn, order_item_id, resolved_sku, it.get("item_name"),
                    qty, unit_price, item_amount, business_date,
                    item_id, model_id, item_sku, model_sku,
                    SOURCE_SYSTEM, order_item_id, ts_from_epoch(od.get("create_time")), ts_from_epoch(od.get("update_time")),
                    "/api/v2/order/get_order_detail", shop_id, etl_run_id,
                ),
            )
            counters["fact_order_item"].record(cur.fetchone()[0])

    conn.commit()

    # ---- Finance / escrow ----
    escrow_by_sn = fetch_all_escrow(client, order_sns)
    conn, cur = reconnect(conn)
    for sn, e in escrow_by_sn.items():
        order_status = status_by_sn.get(sn)
        business_date = datetime.strptime(BUSINESS_DATE_STR, "%Y-%m-%d").date()
        err = e.get("error", "")
        http = e.get("_http_status")
        order_income = e.get("response", {}).get("order_income") if not err and http == 200 else None
        if err or http != 200:
            settlement_type = "api_error"
            settlement_amount = None
        elif order_status == "CANCELLED":
            settlement_type = "not_finance_eligible"
            settlement_amount = None
        elif order_income:
            settlement_type = "escrow_estimate"
            settlement_amount = order_income.get("escrow_amount_after_adjustment")
            if settlement_amount is None:
                settlement_amount = order_income.get("escrow_amount")
        else:
            settlement_type = "api_error"
            settlement_amount = None

        cur.execute(
            """
            INSERT INTO core.fact_settlement (
                channel, shop_id, order_id, settlement_id, settlement_amount,
                settlement_type, settlement_time, business_date,
                source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, settlement_id) DO UPDATE SET
                settlement_amount = EXCLUDED.settlement_amount,
                settlement_type = EXCLUDED.settlement_type,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, shop_id, sn, sn, settlement_amount,
                settlement_type, business_date,
                SOURCE_SYSTEM, e.get("request_id"), "/api/v2/payment/get_escrow_detail", shop_id, etl_run_id,
            ),
        )
        counters["fact_settlement"].record(cur.fetchone()[0])

    conn.commit()

    # ---- Returns (attempt only — do not fabricate on failure) ----
    returns_resp = fetch_returns(client)
    conn, cur = reconnect(conn)
    returns_err = returns_resp.get("error", "")
    returns_http = returns_resp.get("_http_status")
    if not returns_err and returns_http == 200:
        return_rows = returns_resp.get("response", {}).get("return", [])
        day_load_status = "PASS" if return_rows else "API_EMPTY_VERIFIED"
        for r in return_rows:
            return_id = str(r.get("return_sn") or r.get("returnsn") or r.get("request_id"))
            cur.execute(
                """
                INSERT INTO core.fact_return_refund (
                    channel, shop_id, order_id, return_id, return_status, refund_amount,
                    return_reason, business_date, source_system, source_record_id,
                    source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, shop_id, return_id) DO UPDATE SET
                    return_status = EXCLUDED.return_status,
                    refund_amount = EXCLUDED.refund_amount,
                    etl_run_id = EXCLUDED.etl_run_id;
                """,
                (
                    CHANNEL, shop_id, r.get("order_sn"), return_id, r.get("status"),
                    r.get("refund_amount"), r.get("reason"),
                    datetime.strptime(BUSINESS_DATE_STR, "%Y-%m-%d").date(),
                    SOURCE_SYSTEM, return_id, "/api/v2/returns/get_return_list", shop_id, etl_run_id,
                ),
            )
    else:
        day_load_status = "API_ERROR_TRANSIENT"

    conn.commit()

    # ---- Ads (shop-level daily, direct + broad preserved as separate rows) ----
    ads_resp = fetch_ads(client)
    conn, cur = reconnect(conn)
    ads_rows_written = 0
    ads_day = ads_resp.get("response", [])
    if isinstance(ads_day, list) and ads_day:
        d = ads_day[0]
        business_date = datetime.strptime(BUSINESS_DATE_STR, "%Y-%m-%d").date()
        for label, order_key, gmv_key in [("SHOP_TOTAL_DIRECT", "direct_order", "direct_gmv"),
                                            ("SHOP_TOTAL_BROAD", "broad_order", "broad_gmv")]:
            cur.execute(
                """
                INSERT INTO core.fact_ads_daily (
                    channel, ad_account_id, shop_id, campaign_id, campaign_name,
                    business_date, impressions, clicks, spend, orders, gmv,
                    source_system, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, ad_account_id, business_date, campaign_id) DO UPDATE SET
                    impressions = EXCLUDED.impressions,
                    clicks = EXCLUDED.clicks,
                    spend = EXCLUDED.spend,
                    orders = EXCLUDED.orders,
                    gmv = EXCLUDED.gmv,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, shop_id, shop_id, label, label,
                    business_date, d.get("impression"), d.get("clicks"), d.get("expense"),
                    d.get(order_key), d.get(gmv_key),
                    SOURCE_SYSTEM, "/api/v2/ads/get_all_cpc_ads_daily_performance", shop_id, etl_run_id,
                ),
            )
            counters["fact_ads_daily"].record(cur.fetchone()[0])
            ads_rows_written += 1

    conn.commit()

    # ---- Products + Inventory ----
    items, product_pagination_complete = fetch_all_items(client)
    item_ids = [it["item_id"] for it in items]
    models_by_item = fetch_all_models(client, item_ids)

    conn, cur = reconnect(conn)
    now_utc = datetime.now(timezone.utc)
    inv_business_date = now_utc.astimezone(VN_TZ).date()
    product_rows_written = 0
    inventory_rows_written = 0
    for item_id, resp in models_by_item.items():
        for m in resp.get("response", {}).get("model", []):
            sku = m.get("model_sku")
            if not sku:
                continue
            status_normal = m.get("model_status") == "MODEL_NORMAL"
            cur.execute(
                """
                INSERT INTO core.dim_product (
                    sku, product_name, barcode, is_active,
                    source_system, source_record_id, source_updated_at, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sku) DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    barcode = EXCLUDED.barcode,
                    is_active = EXCLUDED.is_active,
                    source_updated_at = EXCLUDED.source_updated_at,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    sku, m.get("model_name"), m.get("gtin_code"), status_normal,
                    SOURCE_SYSTEM, str(item_id), now_utc, etl_run_id,
                ),
            )
            counters["dim_product"].record(cur.fetchone()[0])
            product_rows_written += 1

            summary_info = m.get("stock_info_v2", {}).get("summary_info", {})
            qty_available = summary_info.get("total_available_stock")
            qty_reserved = summary_info.get("total_reserved_stock")
            qty_on_hand = (qty_available + qty_reserved) if (qty_available is not None and qty_reserved is not None) else None
            cur.execute(
                """
                INSERT INTO core.fact_inventory_snapshot (
                    channel, warehouse, sku, snapshot_at, business_date,
                    qty_on_hand, qty_reserved, qty_available,
                    source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, warehouse, sku, snapshot_at) DO UPDATE SET
                    qty_on_hand = EXCLUDED.qty_on_hand,
                    qty_reserved = EXCLUDED.qty_reserved,
                    qty_available = EXCLUDED.qty_available,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, "ALL", sku, now_utc, inv_business_date,
                    qty_on_hand, qty_reserved, qty_available,
                    SOURCE_SYSTEM, str(item_id), "/api/v2/product/get_model_list", shop_id, etl_run_id,
                ),
            )
            counters["fact_inventory_snapshot"].record(cur.fetchone()[0])
            inventory_rows_written += 1

    conn.commit()
    cur.close()
    conn.close()

    return {
        "order_sns": order_sns,
        "order_details": order_details,
        "escrow_by_sn": escrow_by_sn,
        "returns_resp": returns_resp,
        "day_load_status": day_load_status,
        "ads_resp": ads_resp,
        "ads_rows_written": ads_rows_written,
        "items": items,
        "product_pagination_complete": product_pagination_complete,
        "models_by_item": models_by_item,
        "product_rows_written": product_rows_written,
        "inventory_rows_written": inventory_rows_written,
        "counters": {k: {"inserted": v.inserted, "updated": v.updated} for k, v in counters.items()},
    }


def main(run_label: str) -> dict:
    shop_id = get_secret(ACCOUNT_SHOP_ID)
    if not shop_id:
        raise RuntimeError("shop_id not found in Keychain")

    client = ShopeeClient()
    conn = get_db_conn()
    conn.autocommit = False

    etl_run_id = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO control.etl_run_log (etl_run_id, source_system, source_endpoint,
            source_shop_id, run_type, business_date, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'running');
        """,
        (etl_run_id, SOURCE_SYSTEM, "p2a_shopee_to_postgres", shop_id, run_label, BUSINESS_DATE_STR),
    )
    conn.commit()  # separate, short transaction so the "running" row is durable even if the load fails

    result: dict = {"etl_run_id": etl_run_id}
    try:
        # run_etl reconnects before every write burst and closes its own
        # final connection — this `conn` (used only for the running-row
        # insert above) is not touched again.
        result.update(run_etl(client, conn, etl_run_id, shop_id))
        rows_processed = sum(v["inserted"] + v["updated"] for v in result["counters"].values())
        status_conn = get_db_conn()
        status_conn.autocommit = True
        status_conn.cursor().execute(
            """
            UPDATE control.etl_run_log
            SET status = 'success', finished_at = now(), rows_processed = %s
            WHERE etl_run_id = %s;
            """,
            (rows_processed, etl_run_id),
        )
        status_conn.close()
        result["run_status"] = "success"
    except Exception as e:  # noqa: BLE001
        # A fresh connection for the failure write — the one that raised
        # may already be dead (that is the failure mode this guards).
        try:
            fail_conn = get_db_conn()
            fail_conn.autocommit = True
            fail_conn.cursor().execute(
                """
                UPDATE control.etl_run_log
                SET status = 'fail', finished_at = now(), error_message = %s
                WHERE etl_run_id = %s;
                """,
                (str(e)[:2000], etl_run_id),
            )
            fail_conn.close()
        except Exception:  # noqa: BLE001
            pass
        result["run_status"] = "fail"
        result["error"] = str(e)
        raise

    return result


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "p2a_run"
    out = main(label)
    save_raw(f"_run_result_{label}.json", {
        k: v for k, v in out.items()
        if k not in ("order_details", "escrow_by_sn", "returns_resp", "ads_resp", "items", "models_by_item")
    })
    print(json.dumps({
        "etl_run_id": out["etl_run_id"],
        "run_status": out["run_status"],
        "counters": out.get("counters"),
        "day_load_status": out.get("day_load_status"),
        "product_pagination_complete": out.get("product_pagination_complete"),
        "product_rows_written": out.get("product_rows_written"),
        "inventory_rows_written": out.get("inventory_rows_written"),
        "ads_rows_written": out.get("ads_rows_written"),
        "order_count": len(out.get("order_sns", [])),
    }, indent=2))
