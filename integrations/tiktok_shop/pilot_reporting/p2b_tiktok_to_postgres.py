#!/usr/bin/env python3
"""
P2B — TikTok Shop -> Neon PostgreSQL (hh_ecom) controlled load for
business date 2026-09-08.

Reuses the existing, proven collect.py output as-is (raw/ + normalized/
under this directory) — no changes to tiktok_client.py, token_store.py,
keychain.py, or collect.py. This script is an adapter/ETL layer that
reads collect.py's normalized CSVs for report_date, transforms, and
UPSERTs into hh_ecom via hh_etl_writer. Mirrors the P2A Shopee script's
structure (../../shopee/pilot_reporting/p2a_shopee_to_postgres.py) for
consistency.

Security:
- DB credentials come only from the macOS Keychain (service
  HH_ECOM_NEON, key hh_etl_writer_database_url) via `keyring`.
- Connects as hh_etl_writer only. No DDL is issued by this script.
- Nothing in this file ever prints a password or full DATABASE_URL.

Run twice in a row to test UPSERT idempotency (see __main__).
"""
from __future__ import annotations

import csv
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import keyring
import psycopg2

PILOT_DIR = Path(__file__).resolve().parent
NORMALIZED_DIR = PILOT_DIR / "normalized"

VN_TZ = timezone(timedelta(hours=7))
BUSINESS_DATE_STR = "2026-09-08"
BUSINESS_DATE = datetime.strptime(BUSINESS_DATE_STR, "%Y-%m-%d").date()

CHANNEL = "TIKTOK"
SOURCE_SYSTEM = "TIKTOK"
SHOP_ID = "7495998229872806108"
SHOP_NAME = "Le Petit Marseillais Vietnam"


def get_db_conn():
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    if not url:
        raise RuntimeError("hh_etl_writer_database_url missing from Keychain")
    conn = psycopg2.connect(
        url,
        keepalives=1,
        keepalives_idle=20,
        keepalives_interval=10,
        keepalives_count=3,
    )
    del url
    return conn


def reconnect(old_conn):
    """See P2A's identical helper: Neon has been observed to drop
    connections left idle across a slow fetch/transform stretch. Open a
    fresh connection right before each write burst instead of hoping the
    old one survived."""
    try:
        old_conn.close()
    except Exception:  # noqa: BLE001
        pass
    new_conn = get_db_conn()
    new_conn.autocommit = False
    return new_conn, new_conn.cursor()


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


def vn_date_from_epoch(epoch):
    ts = ts_from_epoch(epoch)
    if ts is None:
        return None
    return ts.astimezone(VN_TZ).date()


def num(v):
    if v in (None, ""):
        return None
    return v


def read_csv(name: str) -> list[dict[str, Any]]:
    path = NORMALIZED_DIR / f"{name}_{BUSINESS_DATE_STR}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class UpsertCounters:
    def __init__(self) -> None:
        self.inserted = 0
        self.updated = 0

    def record(self, was_inserted: bool) -> None:
        if was_inserted:
            self.inserted += 1
        else:
            self.updated += 1


def fact_settlement_has_finance_breakdown(cur) -> bool:
    cur.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_schema='core' AND table_name='fact_settlement'
             AND column_name IN ('currency','revenue_amount','fee_and_tax_amount','shipping_cost_amount');"""
    )
    return cur.fetchone()[0] == 4


def run_etl(conn, etl_run_id: str) -> dict:
    cur = conn.cursor()
    counters: dict[str, UpsertCounters] = {
        "dim_channel": UpsertCounters(), "dim_product": UpsertCounters(),
        "fact_order": UpsertCounters(), "fact_order_item": UpsertCounters(),
        "fact_settlement": UpsertCounters(), "fact_return_refund": UpsertCounters(),
        "fact_inventory_snapshot": UpsertCounters(),
    }
    has_finance_breakdown = fact_settlement_has_finance_breakdown(cur)

    # ---- dim_channel ----
    cur.execute(
        """
        INSERT INTO core.dim_channel (channel, shop_id, shop_name, market)
        VALUES (%s, %s, %s, 'VN')
        ON CONFLICT (channel, shop_id) DO UPDATE SET shop_name = EXCLUDED.shop_name
        RETURNING (xmax = 0) AS inserted;
        """,
        (CHANNEL, SHOP_ID, SHOP_NAME),
    )
    counters["dim_channel"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Orders ----
    order_rows = read_csv("orders")
    conn, cur = reconnect(conn)
    for o in order_rows:
        oid = o["order_id"]
        business_date = vn_date_from_epoch(o.get("create_time")) or BUSINESS_DATE
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
                CHANNEL, SHOP_ID, oid, o.get("status"),
                ts_from_epoch(o.get("create_time")), ts_from_epoch(o.get("update_time")),
                o.get("currency"), num(o.get("total_amount")), business_date,
                SOURCE_SYSTEM, oid, ts_from_epoch(o.get("create_time")), ts_from_epoch(o.get("update_time")),
                "/order/202309/orders", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_order"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Order items ----
    line_rows = read_csv("order_lines")
    order_business_date_by_id = {
        o["order_id"]: (vn_date_from_epoch(o.get("create_time")) or BUSINESS_DATE)
        for o in order_rows
    }
    conn, cur = reconnect(conn)
    for li in line_rows:
        oid = li["order_id"]
        qty = num(li.get("quantity"))
        unit_price = num(li.get("sale_price"))
        try:
            item_amount = (float(qty) * float(unit_price)) if (qty is not None and unit_price is not None) else None
        except (TypeError, ValueError):
            item_amount = None
        business_date = order_business_date_by_id.get(oid, BUSINESS_DATE)
        platform_product_id = str(li["product_id"]) if li.get("product_id") not in (None, "") else None
        platform_sku_id = str(li["sku_id"]) if li.get("sku_id") not in (None, "") else None
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
                qty = EXCLUDED.qty,
                unit_price = EXCLUDED.unit_price,
                item_amount = EXCLUDED.item_amount,
                source_updated_at = EXCLUDED.source_updated_at,
                sku = EXCLUDED.sku, platform_product_id = EXCLUDED.platform_product_id,
                platform_sku_id = EXCLUDED.platform_sku_id,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, SHOP_ID, oid, li["line_item_id"],
                li.get("seller_sku") or None, li.get("product_name"),
                qty, unit_price, item_amount, business_date,
                platform_product_id, platform_sku_id,
                SOURCE_SYSTEM, li["line_item_id"], None, None,
                "/order/202309/orders", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_order_item"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Settlement (finance) ----
    # finance_match_status.csv: authoritative per-order match state
    # (MATCHED / NOT_SETTLED_YET / UNKNOWN), never fabricated.
    # order_finance_transactions.csv: revenue/fee/shipping breakdown,
    # present only for MATCHED orders.
    match_rows = read_csv("finance_match_status")
    tx_by_order = {r["order_id"]: r for r in read_csv("order_finance_transactions")}
    conn, cur = reconnect(conn)
    for m in match_rows:
        oid = m["order_id"]
        status = m["finance_match_status"]
        tx = tx_by_order.get(oid)
        settlement_amount = num(m.get("settlement_amount")) if status == "MATCHED" else None
        business_date = order_business_date_by_id.get(oid, BUSINESS_DATE)
        if has_finance_breakdown:
            cur.execute(
                """
                INSERT INTO core.fact_settlement (
                    channel, shop_id, order_id, settlement_id, settlement_amount,
                    settlement_type, settlement_time, business_date,
                    currency, revenue_amount, fee_and_tax_amount, shipping_cost_amount,
                    source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, shop_id, settlement_id) DO UPDATE SET
                    settlement_amount = EXCLUDED.settlement_amount,
                    settlement_type = EXCLUDED.settlement_type,
                    currency = EXCLUDED.currency,
                    revenue_amount = EXCLUDED.revenue_amount,
                    fee_and_tax_amount = EXCLUDED.fee_and_tax_amount,
                    shipping_cost_amount = EXCLUDED.shipping_cost_amount,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, SHOP_ID, oid, oid, settlement_amount,
                    status, business_date,
                    tx.get("currency") if tx else None,
                    num(tx.get("revenue_amount")) if tx else None,
                    num(tx.get("fee_and_tax_amount")) if tx else None,
                    num(tx.get("shipping_cost_amount")) if tx else None,
                    SOURCE_SYSTEM, oid, "/finance/202501/orders/{order_id}/statement_transactions",
                    SHOP_ID, etl_run_id,
                ),
            )
        else:
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
                    CHANNEL, SHOP_ID, oid, oid, settlement_amount,
                    status, business_date,
                    SOURCE_SYSTEM, oid, "/finance/202501/orders/{order_id}/statement_transactions",
                    SHOP_ID, etl_run_id,
                ),
            )
        counters["fact_settlement"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Returns (real API response only — 0 rows this run is expected
    # and reported as PASS_EMPTY, not fabricated) ----
    return_rows = read_csv("returns")
    conn, cur = reconnect(conn)
    has_return_currency = False
    cur.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_schema='core' AND table_name='fact_return_refund' AND column_name='currency';"""
    )
    has_return_currency = cur.fetchone()[0] == 1
    for r in return_rows:
        return_id = r.get("return_id")
        if not return_id:
            continue
        business_date = order_business_date_by_id.get(r.get("order_id"), BUSINESS_DATE)
        if has_return_currency:
            cur.execute(
                """
                INSERT INTO core.fact_return_refund (
                    channel, shop_id, order_id, return_id, return_status, refund_amount,
                    return_reason, business_date, currency, source_system, source_record_id,
                    source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, shop_id, return_id) DO UPDATE SET
                    return_status = EXCLUDED.return_status,
                    refund_amount = EXCLUDED.refund_amount,
                    currency = EXCLUDED.currency,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, SHOP_ID, r.get("order_id"), return_id, r.get("return_status"),
                    num(r.get("refund_amount")), r.get("return_reason"), business_date,
                    r.get("currency"), SOURCE_SYSTEM, return_id,
                    "/return_refund/202602/returns/search", SHOP_ID, etl_run_id,
                ),
            )
        else:
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
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    CHANNEL, SHOP_ID, r.get("order_id"), return_id, r.get("return_status"),
                    num(r.get("refund_amount")), r.get("return_reason"), business_date,
                    SOURCE_SYSTEM, return_id, "/return_refund/202602/returns/search", SHOP_ID, etl_run_id,
                ),
            )
        counters["fact_return_refund"].record(cur.fetchone()[0])
    conn.commit()

    # ---- Products + Inventory (current catalog/stock snapshot — not
    # backdated to business_date; TikTok's product/SKU search endpoint
    # is point-in-time, not historical, so inventory business_date =
    # extraction date per Section 10) ----
    product_rows = read_csv("products")
    conn, cur = reconnect(conn)
    now_utc = datetime.now(timezone.utc)
    inv_business_date = now_utc.astimezone(VN_TZ).date()
    skipped_missing_sku = 0
    for p in product_rows:
        sku = (p.get("seller_sku") or "").strip()
        if not sku:
            skipped_missing_sku += 1
            continue
        cur.execute(
            """
            INSERT INTO core.dim_product (
                sku, product_name, is_active,
                source_system, source_record_id, source_updated_at, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (sku) DO UPDATE SET
                product_name = EXCLUDED.product_name,
                is_active = EXCLUDED.is_active,
                source_updated_at = EXCLUDED.source_updated_at,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                sku, p.get("product_title"), p.get("sku_status") == "ACTIVATE",
                SOURCE_SYSTEM, p.get("sku_id") or p.get("product_id"), now_utc, etl_run_id,
            ),
        )
        counters["dim_product"].record(cur.fetchone()[0])

        qty_available = num(p.get("inventory_total_qty"))
        cur.execute(
            """
            INSERT INTO core.fact_inventory_snapshot (
                channel, warehouse, sku, snapshot_at, business_date,
                qty_on_hand, qty_reserved, qty_available,
                source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, warehouse, sku, snapshot_at) DO UPDATE SET
                qty_on_hand = EXCLUDED.qty_on_hand,
                qty_available = EXCLUDED.qty_available,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, "ALL", sku, now_utc, inv_business_date,
                qty_available, None, qty_available,
                SOURCE_SYSTEM, p.get("sku_id") or p.get("product_id"),
                "/product/202502/products/search", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_inventory_snapshot"].record(cur.fetchone()[0])

    conn.commit()
    cur.close()
    conn.close()

    return {
        "order_count": len(order_rows),
        "order_item_count": len(line_rows),
        "settlement_count": len(match_rows),
        "return_count": len(return_rows),
        "product_row_count": len(product_rows),
        "product_rows_skipped_missing_sku": skipped_missing_sku,
        "has_finance_breakdown_columns": has_finance_breakdown,
        "counters": {k: {"inserted": v.inserted, "updated": v.updated} for k, v in counters.items()},
    }


def main(run_label: str) -> dict:
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
        (etl_run_id, SOURCE_SYSTEM, "p2b_tiktok_to_postgres", SHOP_ID, run_label, BUSINESS_DATE_STR),
    )
    conn.commit()

    result: dict = {"etl_run_id": etl_run_id}
    try:
        result.update(run_etl(conn, etl_run_id))
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
    label = sys.argv[1] if len(sys.argv) > 1 else "p2b_run"
    out = main(label)
    print(json.dumps(out, indent=2, default=str))
