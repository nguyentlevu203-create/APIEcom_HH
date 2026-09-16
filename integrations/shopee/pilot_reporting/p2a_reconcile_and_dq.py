#!/usr/bin/env python3
"""
P2A — reconciliation (API evidence vs DB) + data quality checks, run
after p2a_shopee_to_postgres.py. Reads raw evidence this run wrote
under pilot_reporting/raw/p2a_2026-09-08/ and compares against live DB
state via hh_etl_writer (read-only queries only, beyond what the main
ETL script already committed).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import keyring
import psycopg2

PILOT_DIR = Path(__file__).resolve().parent
RAW_DIR = PILOT_DIR / "raw" / "p2a_2026-09-08"
ARTIFACTS_DIR = Path("/Users/VuIT/Desktop/APIClaude/artifacts/v0")

BUSINESS_DATE = "2026-09-08"
CHANNEL = "SHOPEE"


def get_db_conn():
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    conn = psycopg2.connect(url)
    del url
    return conn


def load_json(name):
    with open(RAW_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    order_sns = load_json("order_sns.json")
    order_details = load_json("order_detail.json")
    ads = load_json("ads_daily.json")
    item_full = load_json("item_list_full.json")
    escrow = load_json("escrow_detail_full.json")

    api_order_count = len(order_sns)
    api_units = 0
    api_gmv = 0
    api_cancel = 0
    for od in order_details:
        if od.get("order_status") == "CANCELLED":
            api_cancel += 1
        for it in od.get("item_list", []):
            api_units += it.get("model_quantity_purchased", 0) or 0
        api_gmv += od.get("total_amount", 0) or 0

    api_product_count = item_full.get("fetched_item_count", len(item_full.get("item", [])))
    api_finance_rows = len(escrow)
    api_ads_rows = len(ads.get("response", [])) * 2  # direct + broad preserved as 2 rows

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE channel=%s AND business_date=%s;",
        (CHANNEL, BUSINESS_DATE),
    )
    db_order_count = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*), COALESCE(sum(qty),0) FROM core.fact_order_item WHERE channel=%s AND business_date=%s;",
        (CHANNEL, BUSINESS_DATE),
    )
    db_item_rows, db_units = cur.fetchone()

    cur.execute(
        "SELECT COALESCE(sum(total_amount),0) FROM core.fact_order WHERE channel=%s AND business_date=%s;",
        (CHANNEL, BUSINESS_DATE),
    )
    db_gmv = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE channel=%s AND business_date=%s AND order_status='CANCELLED';",
        (CHANNEL, BUSINESS_DATE),
    )
    db_cancel = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM core.dim_product WHERE source_system='SHOPEE';")
    db_product_count = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM core.fact_settlement WHERE channel=%s AND business_date=%s;",
        (CHANNEL, BUSINESS_DATE),
    )
    db_finance_rows = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM core.fact_ads_daily WHERE channel=%s AND business_date=%s;",
        (CHANNEL, BUSINESS_DATE),
    )
    db_ads_rows = cur.fetchone()[0]

    rows = [
        {
            "METRIC": "Order count", "API_VALUE": api_order_count, "DB_VALUE": db_order_count,
            "DIFFERENCE": db_order_count - api_order_count,
            "DEFINITION": "distinct order_sn returned by get_order_list for create_time in [2026-09-08 00:00, 2026-09-09 00:00) Asia/Ho_Chi_Minh",
            "DATE_BASIS": "create_time (Asia/Ho_Chi_Minh)", "STATUS_BASIS": "all statuses",
            "PASS_FAIL": "PASS" if db_order_count == api_order_count else "FAIL", "NOTE": "",
        },
        {
            "METRIC": "Units ordered", "API_VALUE": api_units, "DB_VALUE": int(db_units),
            "DIFFERENCE": int(db_units) - api_units,
            "DEFINITION": "sum of item_list[].model_quantity_purchased across all fetched order_detail records",
            "DATE_BASIS": "create_time (Asia/Ho_Chi_Minh)", "STATUS_BASIS": "all statuses",
            "PASS_FAIL": "PASS" if int(db_units) == api_units else "FAIL", "NOTE": "",
        },
        {
            "METRIC": "Platform Order GMV (all statuses)", "API_VALUE": api_gmv, "DB_VALUE": int(db_gmv),
            "DIFFERENCE": int(db_gmv) - api_gmv,
            "DEFINITION": "sum of order_detail.total_amount across all fetched orders, all statuses (order-level GMV, NOT escrow/settlement)",
            "DATE_BASIS": "create_time (Asia/Ho_Chi_Minh)", "STATUS_BASIS": "all statuses",
            "PASS_FAIL": "PASS" if int(db_gmv) == api_gmv else "FAIL", "NOTE": "",
        },
        {
            "METRIC": "Cancel order count", "API_VALUE": api_cancel, "DB_VALUE": db_cancel,
            "DIFFERENCE": db_cancel - api_cancel,
            "DEFINITION": "count of order_detail.order_status == 'CANCELLED'",
            "DATE_BASIS": "create_time (Asia/Ho_Chi_Minh)", "STATUS_BASIS": "CANCELLED",
            "PASS_FAIL": "PASS" if db_cancel == api_cancel else "FAIL", "NOTE": "",
        },
        {
            "METRIC": "Products API count vs DB normalized products", "API_VALUE": api_product_count,
            "DB_VALUE": db_product_count, "DIFFERENCE": db_product_count - api_product_count,
            "DEFINITION": "API_VALUE = fetched_item_count (item-level, paginated to completion); DB_VALUE = distinct core.dim_product rows with source_system=SHOPEE (model/SKU-level, since dim_product's natural key is sku, not item_id)",
            "DATE_BASIS": "n/a (current catalog state)", "STATUS_BASIS": "item_status=NORMAL",
            "PASS_FAIL": "INFO_ONLY",
            "NOTE": "Not expected to match 1:1 — one item can have multiple SKU/models (item-to-model is 1:N), so DB_VALUE (SKU count) is normally >= API_VALUE (item count). Not a reconciliation failure by itself.",
        },
        {
            "METRIC": "Finance/escrow source rows vs DB rows", "API_VALUE": api_finance_rows,
            "DB_VALUE": db_finance_rows, "DIFFERENCE": db_finance_rows - api_finance_rows,
            "DEFINITION": "one escrow_detail call attempted per order_sn vs one core.fact_settlement row per order_sn for business_date 2026-09-08",
            "DATE_BASIS": "2026-09-08 (assigned business_date for all settlement rows this run)", "STATUS_BASIS": "all",
            "PASS_FAIL": "PASS" if db_finance_rows == api_finance_rows else "FAIL", "NOTE": "",
        },
        {
            "METRIC": "Ads source rows vs DB rows", "API_VALUE": api_ads_rows, "DB_VALUE": db_ads_rows,
            "DIFFERENCE": db_ads_rows - api_ads_rows,
            "DEFINITION": "API_VALUE = 1 API day-row x 2 (direct+broad preserved separately); DB_VALUE = core.fact_ads_daily rows for business_date 2026-09-08",
            "DATE_BASIS": "2026-09-08", "STATUS_BASIS": "n/a",
            "PASS_FAIL": "PASS" if db_ads_rows == api_ads_rows else "FAIL", "NOTE": "",
        },
    ]

    out_path = ARTIFACTS_DIR / "P2A_SHOPEE_RECONCILIATION.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["METRIC", "API_VALUE", "DB_VALUE", "DIFFERENCE",
                                            "DEFINITION", "DATE_BASIS", "STATUS_BASIS", "PASS_FAIL", "NOTE"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"Reconciliation written: {out_path}")
    for r in rows:
        print(f"  {r['METRIC']}: API={r['API_VALUE']} DB={r['DB_VALUE']} diff={r['DIFFERENCE']} -> {r['PASS_FAIL']}")

    # ---- Data quality checks ----
    dq = {}
    cur.execute(
        """SELECT order_id, count(*) c FROM core.fact_order WHERE channel=%s AND shop_id=(
             SELECT shop_id FROM core.fact_order WHERE channel=%s LIMIT 1)
           GROUP BY channel, shop_id, order_id HAVING count(*) > 1;""",
        (CHANNEL, CHANNEL),
    )
    # Simpler, correct duplicate check via the actual unique key grain:
    cur.execute(
        """SELECT count(*) FROM (
             SELECT channel, shop_id, order_id, count(*) c
             FROM core.fact_order GROUP BY channel, shop_id, order_id HAVING count(*) > 1
           ) x;"""
    )
    dq["duplicate_order_key"] = cur.fetchone()[0]

    cur.execute(
        """SELECT count(*) FROM (
             SELECT channel, shop_id, order_id, order_item_id, count(*) c
             FROM core.fact_order_item GROUP BY channel, shop_id, order_id, order_item_id HAVING count(*) > 1
           ) x;"""
    )
    dq["duplicate_order_item_key"] = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM core.fact_order WHERE channel IS NULL;")
    dq["null_channel"] = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM core.fact_order WHERE order_id IS NULL;")
    dq["null_order_id"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE business_date > (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date;"
    )
    dq["future_business_date"] = cur.fetchone()[0]

    cur.execute(
        """SELECT count(*) FROM core.fact_order_item oi
           WHERE NOT EXISTS (
               SELECT 1 FROM core.fact_order o
               WHERE o.channel = oi.channel AND o.shop_id = oi.shop_id AND o.order_id = oi.order_id
           );"""
    )
    dq["orphan_order_item"] = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE channel=%s AND currency IS NOT NULL AND currency <> 'VND';",
        (CHANNEL,),
    )
    dq["unexpected_currency"] = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM core.fact_order_item WHERE sku IS NULL OR sku = '';")
    dq["sku_missing_on_order_item"] = cur.fetchone()[0]

    cur.execute(
        """SELECT count(*) FROM core.dim_product
           WHERE source_system='SHOPEE' AND (brand IS NULL OR category_l1 IS NULL);"""
    )
    dq["sku_missing_hh_mapping_brand_or_category"] = cur.fetchone()[0]

    dq["cogs_unavailable_note"] = "core.dim_cogs is empty in P2A by design — COGS is deferred to future P5 Drive Master, not fabricated here"
    dq["hh_ean_unavailable_note"] = "core.dim_product.barcode is populated from Shopee gtin_code where the seller entered one; a canonical HH EAN mapping is deferred to future P5 Drive Master"

    print("\n=== DATA QUALITY ===")
    for k, v in dq.items():
        print(f"  {k} = {v}")

    with open(ARTIFACTS_DIR / "_p2a_dq_result.json", "w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2, ensure_ascii=False)

    conn.close()


if __name__ == "__main__":
    main()
