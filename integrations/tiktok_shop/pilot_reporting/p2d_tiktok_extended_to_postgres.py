#!/usr/bin/env python3
"""
P2D — TikTok Shop extended facts (Affiliate, Product Analytics, LIVE
Analytics) -> Neon PostgreSQL (hh_ecom), business date 2026-09-08.

Reads the already-collected, already-normalized CSVs from
collect.py's 2026-09-11 run (normalized/affiliate_orders_2026-09-08.csv,
normalized/product_analytics_2026-09-08.csv,
normalized/live_analytics_2026-09-08.csv) — no new API calls, no changes
to collect.py or tiktok_client.py. UPSERTs into the three new core
tables created by sql/031_ecom_extended_facts.sql via hh_etl_writer.

Run twice in a row to test UPSERT idempotency (see __main__).
"""
from __future__ import annotations

import csv
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
    return ts.astimezone(VN_TZ).date() if ts else None


def dec(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


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


def load_affiliate(cur, etl_run_id: str, counters: dict) -> int:
    rows = read_csv("affiliate_orders")
    for r in rows:
        business_date = vn_date_from_epoch(r.get("order_create_time")) or BUSINESS_DATE
        base_gmv = dec(r.get("estimated_commission_base_amount"))
        est_commission = dec(r.get("estimated_paid_shop_ads_commission_amount"))
        status = r.get("settlement_status")
        # settled_commission is the SAME source number, exposed only when
        # the source itself says SETTLED — never a separately derived value.
        settled_commission = est_commission if status == "SETTLED" else None
        source_record_id = f"{r['order_id']}:{r['sku_id']}:{r['content_id']}"
        cur.execute(
            """
            INSERT INTO core.fact_affiliate_daily (
                channel, shop_id, order_id, sku_id, content_id, product_id,
                creator_username, content_type, commission_model, quantity, currency,
                affiliate_attributed_gmv, estimated_commission, validated_commission,
                settled_commission, settlement_status, business_date,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, order_id, sku_id, content_id) DO UPDATE SET
                affiliate_attributed_gmv = EXCLUDED.affiliate_attributed_gmv,
                estimated_commission = EXCLUDED.estimated_commission,
                settled_commission = EXCLUDED.settled_commission,
                settlement_status = EXCLUDED.settlement_status,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, SHOP_ID, r["order_id"], r["sku_id"], r["content_id"], r.get("product_id"),
                r.get("creator_username"), r.get("content_type"), r.get("commission_model"),
                num(r.get("quantity")), r.get("price_currency"),
                base_gmv, est_commission, settled_commission, status, business_date,
                SOURCE_SYSTEM, source_record_id, ts_from_epoch(r.get("order_create_time")), None,
                "/affiliate_seller/202410/orders/search", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_affiliate_daily"].record(cur.fetchone()[0])
    return len(rows)


def load_product_analytics(cur, etl_run_id: str, counters: dict) -> int:
    rows = read_csv("product_analytics")
    now_utc = datetime.now(timezone.utc)
    for r in rows:
        product_id = r.get("id")
        if not product_id:
            continue
        impressions = num(r.get("total_performance.product_impressions"))
        clicks = num(r.get("total_performance.product_clicks"))
        ctr = dec(r.get("total_performance.ctr"))
        attributed_orders = num(r.get("total_performance.orders"))
        click_to_order_rate = dec(r.get("total_performance.click_order_rate"))
        gmv_amount = dec(r.get("total_performance.gmv.amount"))
        gmv_currency = r.get("total_performance.gmv.currency")
        items_sold = num(r.get("total_performance.items_sold"))
        cur.execute(
            """
            INSERT INTO core.fact_product_analytics_daily (
                channel, shop_id, product_id, sku_id, business_date,
                impressions, clicks, ctr, attributed_orders, click_to_order_rate,
                gmv_amount, gmv_currency, items_sold,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, product_id, business_date) DO UPDATE SET
                impressions = EXCLUDED.impressions,
                clicks = EXCLUDED.clicks,
                ctr = EXCLUDED.ctr,
                attributed_orders = EXCLUDED.attributed_orders,
                click_to_order_rate = EXCLUDED.click_to_order_rate,
                gmv_amount = EXCLUDED.gmv_amount,
                gmv_currency = EXCLUDED.gmv_currency,
                items_sold = EXCLUDED.items_sold,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, SHOP_ID, product_id, BUSINESS_DATE,
                impressions, clicks, ctr, attributed_orders, click_to_order_rate,
                gmv_amount, gmv_currency, items_sold,
                SOURCE_SYSTEM, product_id, now_utc,
                "/analytics/202605/shop_products/performance", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_product_analytics_daily"].record(cur.fetchone()[0])
    return len(rows)


def _rate_to_fraction(v):
    """Same normalization as incr_worker.py's _rate_to_fraction (P7.1) —
    this CSV is a flattened dump of the same live_analytics payload shape,
    so the same "6.18%" vs "0.0096" ambiguity applies here."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        n = dec(s[:-1])
        return (n / 100) if n is not None else None
    return dec(s)


def load_live(cur, etl_run_id: str, counters: dict) -> int:
    rows = read_csv("live_analytics")
    now_utc = datetime.now(timezone.utc)
    # P7.1: this historical one-off loader only ever collected the
    # unfiltered (TikTok-default) query — explicitly 'ALL', never a silent
    # column DEFAULT. See core.fact_live_daily's account_type comment.
    account_type = "ALL"
    for r in rows:
        live_id = r.get("id")
        if not live_id:
            continue
        start_time = ts_from_epoch(r.get("start_time"))
        end_time = ts_from_epoch(r.get("end_time"))
        duration_seconds = int((end_time - start_time).total_seconds()) if start_time and end_time else None
        business_date = vn_date_from_epoch(r.get("start_time")) or BUSINESS_DATE
        # This load reads collect.py's 2026-09-11 collection, whose own
        # domain_status for live_analytics was PASS (10 rows) for
        # 2026-09-08 — the API genuinely served this date, unlike the
        # earlier 2026-09-09 pull (PASS_EMPTY, data-latency). Every row
        # here is therefore READY for the requested date. A blank
        # interaction_performance field on an individual row (observed on
        # a few affiliate-hosted LIVE sessions, e.g. username
        # 'hoangloanaff') is a per-field gap the source itself left empty
        # — not evidence the whole date is unavailable — so it is passed
        # through as NULL on that column rather than flipping the row's
        # latency_status.
        latency_status = "READY"
        cur.execute(
            """
            INSERT INTO core.fact_live_daily (
                channel, shop_id, live_id, business_date, available_data_date,
                title, username, start_time, end_time, account_type, duration_seconds,
                viewers, views, product_impressions, product_clicks, sku_orders,
                customers, items_sold, likes, comments, shares, new_followers,
                avg_viewing_duration_secs, click_through_rate, click_to_order_rate,
                gmv_amount, gmv_currency, latency_status, source_api_version,
                source_system, source_record_id, source_created_at, source_updated_at,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
            ON CONFLICT (channel, shop_id, live_id, account_type) DO UPDATE SET
                viewers = EXCLUDED.viewers,
                views = EXCLUDED.views,
                product_impressions = EXCLUDED.product_impressions,
                product_clicks = EXCLUDED.product_clicks,
                sku_orders = EXCLUDED.sku_orders,
                customers = EXCLUDED.customers,
                items_sold = EXCLUDED.items_sold,
                likes = EXCLUDED.likes,
                comments = EXCLUDED.comments,
                shares = EXCLUDED.shares,
                new_followers = EXCLUDED.new_followers,
                avg_viewing_duration_secs = EXCLUDED.avg_viewing_duration_secs,
                click_through_rate = EXCLUDED.click_through_rate,
                click_to_order_rate = EXCLUDED.click_to_order_rate,
                duration_seconds = EXCLUDED.duration_seconds,
                gmv_amount = EXCLUDED.gmv_amount,
                gmv_currency = EXCLUDED.gmv_currency,
                latency_status = EXCLUDED.latency_status,
                source_api_version = EXCLUDED.source_api_version,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                CHANNEL, SHOP_ID, live_id, business_date, BUSINESS_DATE,
                r.get("title") or None, r.get("username") or None, start_time, end_time,
                account_type, duration_seconds,
                num(r.get("interaction_performance.viewers")), num(r.get("interaction_performance.views")),
                num(r.get("interaction_performance.product_impressions")),
                num(r.get("interaction_performance.product_clicks")),
                num(r.get("sales_performance.sku_orders")),
                num(r.get("sales_performance.customers")), num(r.get("sales_performance.items_sold")),
                num(r.get("interaction_performance.likes")), num(r.get("interaction_performance.comments")),
                num(r.get("interaction_performance.shares")), num(r.get("interaction_performance.new_followers")),
                dec(r.get("interaction_performance.avg_viewing_duration")),
                _rate_to_fraction(r.get("interaction_performance.click_through_rate")),
                _rate_to_fraction(r.get("sales_performance.click_to_order_rate")),
                dec(r.get("sales_performance.gmv.amount")), r.get("sales_performance.gmv.currency"),
                latency_status, "202509",
                SOURCE_SYSTEM, live_id, now_utc,
                "/analytics/202509/shop_lives/performance", SHOP_ID, etl_run_id,
            ),
        )
        counters["fact_live_daily"].record(cur.fetchone()[0])
    return len(rows)


def run_etl(conn, etl_run_id: str) -> dict:
    cur = conn.cursor()
    counters = {
        "fact_affiliate_daily": UpsertCounters(),
        "fact_product_analytics_daily": UpsertCounters(),
        "fact_live_daily": UpsertCounters(),
    }
    affiliate_count = load_affiliate(cur, etl_run_id, counters)
    conn.commit()

    conn, cur = reconnect(conn)
    pa_count = load_product_analytics(cur, etl_run_id, counters)
    conn.commit()

    conn, cur = reconnect(conn)
    live_count = load_live(cur, etl_run_id, counters)
    conn.commit()
    cur.close()
    conn.close()

    return {
        "affiliate_source_rows": affiliate_count,
        "product_analytics_source_rows": pa_count,
        "live_source_rows": live_count,
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
        (etl_run_id, SOURCE_SYSTEM, "p2d_tiktok_extended_to_postgres", SHOP_ID, run_label, BUSINESS_DATE_STR),
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
    label = sys.argv[1] if len(sys.argv) > 1 else "p2d_run"
    out = main(label)
    print(json.dumps(out, indent=2, default=str))
