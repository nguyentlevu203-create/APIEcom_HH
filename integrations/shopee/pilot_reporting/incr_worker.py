#!/usr/bin/env python3
"""
P3 — Shopee incremental worker. Invoked as its own subprocess by
pipelines/incremental.py (never imported directly into a process that
also imports the TikTok client — both integrations/shopee/ and
integrations/tiktok_shop/ define a bare `config.py`, which collide if
both are on sys.path in the same interpreter).

    python3 incr_worker.py <domain> <window_start_iso> <window_end_iso> <etl_run_id>

Reuses the existing, unmodified ShopeeClient (shopee_client.py) and
keychain.py exactly as p2a_shopee_to_postgres.py does. Prints one JSON
result line to stdout; exit code 0 = success, 1 = failure (message on
stderr, sync_state NOT advanced by the caller in that case).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

SHOPEE_DIR = Path(__file__).resolve().parent.parent
PIPELINES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pipelines"
sys.path.insert(0, str(SHOPEE_DIR))
sys.path.insert(0, str(PIPELINES_DIR))

from shopee_client import ShopeeClient  # noqa: E402
from keychain import get_secret, ACCOUNT_SHOP_ID  # noqa: E402
import incr_common as ic  # noqa: E402
from canonical_normalizer import normalize_shopee_escrow, parse_numeric  # noqa: E402


def _parse_retry_after(raw) -> float | None:
    """RFC 7231 Retry-After: either an integer seconds count or an
    HTTP-date. Returns None (falls back to the default backoff table) for
    a missing header, an HTTP-date (rare for this API, not worth the
    parsing surface), or anything unparseable."""
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def shopee_check(resp: dict) -> dict:
    if resp.get("_http_status") in ic.TRANSIENT_HTTP:
        raise ic.TransientHTTPError(str(resp.get("_http_status")))
    if resp.get("error"):
        raise RuntimeError(f"Shopee API error: {resp.get('error')} - {resp.get('message')}")
    return resp


def paginated_order_sns(client: ShopeeClient, ts_from: int, ts_to: int, log) -> list[str]:
    order_sns: list[str] = []
    cursor = ""
    while True:
        params = {
            "time_range_field": "update_time",  # catches status CHANGES on existing orders too
            "time_from": ts_from, "time_to": ts_to, "page_size": 100,
            "response_optional_fields": "order_status",
        }
        if cursor:
            params["cursor"] = cursor

        def call():
            return shopee_check(client.get("/api/v2/order/get_order_list", params))

        resp = ic.with_backoff(call, "shopee order_list", log)
        body = resp.get("response", {})
        order_list = body.get("order_list", [])
        order_sns.extend([o["order_sn"] for o in order_list])
        log(f"order_list page rows={len(order_list)}")
        more = body.get("more", False)
        cursor = body.get("next_cursor", "")
        if not more or not cursor:
            break
        time.sleep(0.3)
    return order_sns


def order_details(client: ShopeeClient, order_sns: list[str], log) -> list[dict]:
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

        resp = ic.with_backoff(call, "shopee order_detail", log)
        details.extend(resp.get("response", {}).get("order_list", []))
        time.sleep(0.3)
    return details


def resolve_shopee_item_sku(it: dict) -> tuple[str | None, str]:
    """Deterministic seller-SKU resolution for one Shopee order-item line
    (P5C root-cause fix — see sql/033_p5c_fact_order_item_identifiers.sql).

    Priority: model_sku when the line genuinely has a variation and
    model_sku is populated; otherwise item_sku when Shopee provides it
    for a no-variation line (model_id == 0). Never inferred from
    item_name/product_name. Returns (resolved_sku_or_None, evidence_field)
    where evidence_field in {"model_sku", "item_sku", "NONE"} — this is
    the exact rule P5A.1 established by live evidence, reused verbatim
    (see artifacts/v0/_p5a1_build.py's resolved_sku = model_sku or item_sku).
    """
    model_sku = (it.get("model_sku") or "").strip()
    item_sku = (it.get("item_sku") or "").strip()
    if model_sku:
        return model_sku, "model_sku"
    if item_sku:
        return item_sku, "item_sku"
    return None, "NONE"


def run_orders(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    client = ShopeeClient()
    order_sns = paginated_order_sns(client, int(window_start.timestamp()), int(window_end.timestamp()), log)
    details = order_details(client, order_sns, log) if order_sns else []

    order_ctr, item_ctr = ic.Counters(), ic.Counters()
    for od in details:
        sn = od["order_sn"]
        bd = ic.ts_from_epoch(od.get("create_time"))
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
                "SHOPEE", shop_id, sn, od.get("order_status"),
                ic.ts_from_epoch(od.get("create_time")), ic.ts_from_epoch(od.get("update_time")),
                od.get("currency"), od.get("total_amount"), bd,
                "SHOPEE", sn, ic.ts_from_epoch(od.get("create_time")), ic.ts_from_epoch(od.get("update_time")),
                "/api/v2/order/get_order_detail", shop_id, etl_run_id,
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
                    "/api/v2/order/get_order_detail", shop_id, etl_run_id,
                ),
            )
            item_ctr.record(cur.fetchone()[0])

    return {"order_sns_count": len(order_sns), "order": order_ctr.as_dict(), "order_item": item_ctr.as_dict()}


def run_returns(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    client = ShopeeClient()

    # P9.x fix — root-caused live this session: get_return_list requires
    # page_no explicitly. Without it, Shopee returns a generic
    # error_data/"Inner error, please try later. [5]" that was
    # misclassified for months as an external platform outage — it is
    # actually a missing-required-parameter client bug. Confirmed by a
    # controlled A/B call: identical params except page_no, reproducibly
    # fails without it and succeeds with it. Paginate via `more`
    # (boolean), incrementing page_no — same pattern as page_no-based
    # Shopee endpoints elsewhere in this client.
    rows = []
    page_no = 0
    while True:
        def call(page_no=page_no):
            return shopee_check(client.get("/api/v2/returns/get_return_list", {
                "create_time_from": int(window_start.timestamp()), "create_time_to": int(window_end.timestamp()),
                "page_size": 100, "page_no": page_no,
            }))

        resp = ic.with_backoff(call, "shopee returns", log)
        body = resp.get("response", {})
        page_rows = body.get("return", [])
        rows.extend(page_rows)
        log(f"returns page_no={page_no} rows={len(page_rows)}")
        if not body.get("more") or not page_rows:
            break
        page_no += 1
        time.sleep(0.2)

    ctr = ic.Counters()
    for r in rows:
        return_id = str(r.get("return_sn") or r.get("returnsn") or r.get("request_id"))
        bd = ic.ts_from_epoch(r.get("create_time"))
        bd = bd.astimezone(ic.VN_TZ).date() if bd else ic.vn_date(window_end)
        cur.execute(
            """
            INSERT INTO core.fact_return_refund (
                channel, shop_id, order_id, return_id, return_status, refund_amount, currency,
                return_reason, business_date, source_system, source_record_id,
                source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel, shop_id, return_id) DO UPDATE SET
                return_status = EXCLUDED.return_status, refund_amount = EXCLUDED.refund_amount,
                currency = EXCLUDED.currency, etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "SHOPEE", shop_id, r.get("order_sn"), return_id, r.get("status"),
                r.get("refund_amount"), r.get("currency"), r.get("reason"), bd,
                "SHOPEE", return_id, "/api/v2/returns/get_return_list", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return {"return": ctr.as_dict()}


def run_finance(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    client = ShopeeClient()
    order_sns = paginated_order_sns(client, int(window_start.timestamp()), int(window_end.timestamp()), log)
    ctr = ic.Counters()
    for sn in order_sns:
        def call(sn=sn):
            resp = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": sn})
            # A per-order escrow error (order not yet in escrow) is an
            # EXPECTED business state, handled explicitly below — only a
            # transient HTTP status is retried/raised here.
            if resp.get("_http_status") in ic.TRANSIENT_HTTP:
                raise ic.TransientHTTPError(str(resp.get("_http_status")))
            return resp

        e = ic.with_backoff(call, f"shopee escrow {sn}", log)
        err = e.get("error", "")
        http = e.get("_http_status")
        resp_body = e.get("response", {}) or {}
        order_income = resp_body.get("order_income") if not err and http == 200 else None
        buyer_payment_info = resp_body.get("buyer_payment_info") if not err and http == 200 else None

        # Phase 6A — single canonical normalizer call produces BOTH the
        # structured fields and the raw payload from the SAME response,
        # in the SAME pass, so raw and structured can never diverge the
        # way they did when a separate later backfill script updated
        # only the raw columns (see RAW_ONLY_WRITER_INVENTORY.csv,
        # order 2609153STTS4DT). No other code path may write
        # order_income_raw/buyer_payment_info_raw without also going
        # through this normalizer.
        if err or http != 200 or order_income is None:
            settlement_type, settlement_amount = "api_error", None
            detail = {}
            order_income_raw = buyer_payment_info_raw = None
        else:
            settlement_type = "escrow_estimate"
            norm = normalize_shopee_escrow(order_income, buyer_payment_info)
            detail = norm["structured"]
            settlement_amount = norm["settlement_amount"]
            order_income_raw = json.dumps(norm["order_income_raw"]) if norm["order_income_raw"] is not None else None
            buyer_payment_info_raw = json.dumps(norm["buyer_payment_info_raw"]) if norm["buyer_payment_info_raw"] is not None else None

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
                order_income_raw, buyer_payment_info_raw,
                source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
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
                order_income_raw = EXCLUDED.order_income_raw, buyer_payment_info_raw = EXCLUDED.buyer_payment_info_raw,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                "SHOPEE", shop_id, sn, sn, settlement_amount, settlement_type, ic.vn_date(window_end),
                detail.get("order_original_price"), detail.get("seller_discount"),
                detail.get("voucher_from_seller"), detail.get("voucher_from_shopee"),
                detail.get("seller_return_refund"), detail.get("drc_adjustable_refund"),
                detail.get("seller_lost_compensation"), detail.get("commission_fee"), detail.get("service_fee"),
                detail.get("seller_transaction_fee"), detail.get("actual_shipping_fee"),
                detail.get("shopee_shipping_rebate"), detail.get("buyer_paid_shipping_fee"),
                detail.get("order_ams_commission_fee"), detail.get("ads_escrow_top_up_fee_or_technical_support_fee"),
                order_income_raw, buyer_payment_info_raw,
                "SHOPEE", e.get("request_id"), "/api/v2/payment/get_escrow_detail", shop_id, etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
        time.sleep(0.2)
    return {"settlement": ctr.as_dict()}


def run_ads(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=None) -> dict:
    client = ShopeeClient()
    # target_date lets P4 reconciliation ask for a specific historical
    # calendar date directly — vn_date(window_end) alone would be wrong
    # there, since a reconcile window's window_end is the EXCLUSIVE start
    # of the NEXT day (needed for the orders/returns time-range filters),
    # not "now" the way an incremental call's window_end is.
    d = target_date or ic.vn_date(window_end)
    date_str = d.strftime("%d-%m-%Y")

    def call():
        return shopee_check(client.get("/api/v2/ads/get_all_cpc_ads_daily_performance", {
            "start_date": date_str, "end_date": date_str,
        }))

    resp = ic.with_backoff(call, "shopee ads", log)
    ctr = ic.Counters()
    ads_day = resp.get("response", [])
    if isinstance(ads_day, list) and ads_day:
        row = ads_day[0]
        for label, order_key, gmv_key in [("SHOP_TOTAL_DIRECT", "direct_order", "direct_gmv"),
                                            ("SHOP_TOTAL_BROAD", "broad_order", "broad_gmv")]:
            impressions, clicks, spend, gmv = row.get("impression"), row.get("clicks"), row.get("expense"), row.get(gmv_key)
            row_ctr = round(clicks / impressions, 4) if impressions else None
            row_roas = round(gmv / spend, 4) if spend else None
            cur.execute(
                """
                INSERT INTO core.fact_ads_daily (
                    channel, ad_account_id, shop_id, campaign_id, campaign_name,
                    business_date, impressions, clicks, spend, orders, gmv, ctr, roas,
                    attribution_basis, source_system, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, ad_account_id, business_date, campaign_id) DO UPDATE SET
                    impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks,
                    spend = EXCLUDED.spend, orders = EXCLUDED.orders, gmv = EXCLUDED.gmv,
                    ctr = EXCLUDED.ctr, roas = EXCLUDED.roas,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    "SHOPEE", shop_id, shop_id, label, label, d,
                    impressions, clicks, spend,
                    row.get(order_key), gmv, row_ctr, row_roas,
                    "DIRECT" if label == "SHOP_TOTAL_DIRECT" else "BROAD",
                    "SHOPEE", "/api/v2/ads/get_all_cpc_ads_daily_performance", shop_id, etl_run_id,
                ),
            )
            ctr.record(cur.fetchone()[0])
    return {"ads": ctr.as_dict()}


def run_product_inventory(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    client = ShopeeClient()
    items, offset, page_size = [], 0, 100
    while True:
        def call(offset=offset):
            return shopee_check(client.get("/api/v2/product/get_item_list", {
                "offset": offset, "page_size": page_size, "item_status": "NORMAL",
            }))
        resp = ic.with_backoff(call, "shopee item_list", log)
        body = resp.get("response", {})
        page_items = body.get("item", [])
        items.extend(page_items)
        if not body.get("has_next_page", False) or not page_items:
            break
        offset += page_size
        time.sleep(0.2)

    product_ctr, inv_ctr = ic.Counters(), ic.Counters()
    now = ic.now_utc()
    inv_date = ic.vn_date(now)
    for it in items:
        item_id = it["item_id"]

        def call(item_id=item_id):
            return shopee_check(client.get("/api/v2/product/get_model_list", {"item_id": item_id}))
        m_resp = ic.with_backoff(call, f"shopee model_list {item_id}", log)
        for m in m_resp.get("response", {}).get("model", []):
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
                    product_name = EXCLUDED.product_name, barcode = EXCLUDED.barcode,
                    is_active = EXCLUDED.is_active, source_updated_at = EXCLUDED.source_updated_at,
                    etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (sku, m.get("model_name"), m.get("gtin_code"), status_normal,
                 "SHOPEE", str(item_id), now, etl_run_id),
            )
            product_ctr.record(cur.fetchone()[0])

            summary = m.get("stock_info_v2", {}).get("summary_info", {})
            qty_available = summary.get("total_available_stock")
            qty_reserved = summary.get("total_reserved_stock")
            qty_on_hand = (qty_available + qty_reserved) if (qty_available is not None and qty_reserved is not None) else None
            cur.execute(
                """
                INSERT INTO core.fact_inventory_snapshot (
                    channel, warehouse, sku, snapshot_at, business_date,
                    qty_on_hand, qty_reserved, qty_available,
                    source_system, source_record_id, source_endpoint, source_shop_id, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, warehouse, sku, snapshot_at) DO UPDATE SET
                    qty_on_hand = EXCLUDED.qty_on_hand, qty_reserved = EXCLUDED.qty_reserved,
                    qty_available = EXCLUDED.qty_available, etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                ("SHOPEE", "ALL", sku, now, inv_date, qty_on_hand, qty_reserved, qty_available,
                 "SHOPEE", str(item_id), "/api/v2/product/get_model_list", shop_id, etl_run_id),
            )
            inv_ctr.record(cur.fetchone()[0])
        time.sleep(0.15)

    return {"dim_product": product_ctr.as_dict(), "fact_inventory_snapshot": inv_ctr.as_dict()}


## P11-HEAL — AMS backlog/catch-up handling.
#
# Proven live 2026-09-23 (SHOPEE/affiliate_ams stuck since 2026-09-15):
# run_affiliate_ams() used to be a single all-or-nothing pass over every
# day in the backlog — one RuntimeError anywhere (e.g. the newest
# requested day being past whatever "latest data date" AMS currently
# has for a given report) aborted the whole DB transaction, so days
# that HAD already succeeded were rolled back too and sync_state never
# advanced at all. Every following run re-requested the exact same
# (growing) window and hit the same wall. The three functions below
# separate "AMS says this date isn't published yet" (expected, keep
# what already succeeded, stop for now, retry next cycle) from any
# other error (still a real, whole-call failure — unchanged from
# before).
AMS_DATE_NOT_READY_MARKERS = (
    "data has not been updated",   # documented "today" rejection (pre-existing)
    "invalid time range",          # proven live, 2026-09-23 (see report)
    "latest data date",            # proven live, 2026-09-23 (see report)
)


def _is_ams_date_not_ready(message) -> bool:
    """True only for the specific class of AMS error_param that means
    "no data published for this date yet" — never for a real failure
    (auth, malformed params, 429/5xx after retries, etc.), which must
    still fail the whole call exactly as before."""
    text = str(message or "").lower()
    return any(marker in text for marker in AMS_DATE_NOT_READY_MARKERS)


def _ams_compute_catchup_days(window_start, window_end, now, target_date=None):
    """Pure — the exact day list run_affiliate_ams() used to compute
    inline. target_date (reconciliation D-1/D-3/D-7 re-check) always
    means exactly that one day. Otherwise: every day from the
    incremental window_start through min(window_end, yesterday) — AMS
    reports lag by ~1 day and reject "today" outright, so it's never
    requested via the forward-moving incremental path."""
    if target_date:
        return [target_date]
    d0 = ic.vn_date(window_start)
    d1 = min(ic.vn_date(window_end), ic.vn_date(now) - timedelta(days=1))
    days = []
    d = d0
    while d <= d1:
        days.append(d)
        d += timedelta(days=1)
    return days


def _run_ams_catchup(days, process_day, log):
    """Pure control flow (process_day is injected — no DB/HTTP here, so
    this is fully unit-testable). Processes days in chronological order.
    On a "date not ready" error: stops (later days would fail the same
    way — AMS's lag only ever moves forward), keeping every day that
    already succeeded THIS call. Any other error propagates immediately
    (unchanged failure behavior — e.g. a 429 that exhausted its retry
    budget must still fail the whole call, not be silently treated as
    partial success). Raises if zero days completed — no artificial
    sync_state advancement over a call that made no real progress.

    Returns the last successfully completed date."""
    last_completed_date = None
    for d in days:
        try:
            process_day(d)
        except RuntimeError as e:
            if not _is_ams_date_not_ready(str(e)):
                raise
            log(f"AMS date not ready yet for {d.isoformat()} ({e}) — "
                f"stopping catch-up here, keeping {days.index(d)} earlier day(s) already written this run")
            break
        last_completed_date = d

    if last_completed_date is None:
        raise RuntimeError(
            f"AMS reports not available for any requested day ({days[0]}..{days[-1]})"
        )
    return last_completed_date


def _ams_effective_sync_end(days, last_completed_date):
    """None means "full window completed, advance sync_state to
    window_end as before". Otherwise, sync_state must advance only
    through the end of the last day actually written — never further,
    and never reset backward either."""
    if not days or last_completed_date >= days[-1]:
        return None
    tz7 = timezone(timedelta(hours=7))
    return datetime(
        last_completed_date.year, last_completed_date.month, last_completed_date.day,
        23, 59, 59, tzinfo=tz7,
    ).astimezone(timezone.utc)


def _ams_day_bounds_utc7(d):
    tz7 = timezone(timedelta(hours=7))
    start = int(datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz7).timestamp())
    end = int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz7).timestamp())
    return start, end


def _ams_parse_ts(s):
    if not s or s.strip("-") == "":
        return None
    tz7 = timezone(timedelta(hours=7))
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz7)
    except ValueError:
        return None


def run_affiliate_ams(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=None) -> dict:
    """P8.4 Section D — Shopee AMS affiliate ingestion, added to the
    sanctioned schedule (previously only ever run as a one-off script,
    artifacts/v0/_p6c4_ams_ingest.py, which this ports unchanged apart
    from adapting to this worker's shared cursor/single-commit pattern
    and this file's Counters/RETURNING convention). Uses ONLY the
    separate AMS_* Keychain credentials (distinct app, AMS_PARTNER_ID) -
    never the production Shopee app's credentials, never touches
    core.fact_order/settlement. Commission SSOT/precedence contract
    (FINAL_BILLED > ORDER_LEVEL_ACTUAL > ESTIMATED) is unchanged - this
    only ingests the ORDER_LEVEL_ACTUAL (get_conversion_report) and
    ESTIMATED_PERFORMANCE (get_shop/affiliate/product_performance)
    sources, exactly as the one-off script did."""
    import hashlib
    import hmac

    import requests

    ams_auth_dir = SHOPEE_DIR / "auth"
    if str(ams_auth_dir) not in sys.path:
        sys.path.insert(0, str(ams_auth_dir))
    from config import API_HOST as AMS_API_HOST  # noqa: E402
    from token_exchange_ams import (  # noqa: E402
        ACCOUNT_AMS_ACCESS_TOKEN,
        ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT,
        ACCOUNT_AMS_LIVE_PARTNER_KEY,
        ACCOUNT_AMS_SHOP_ID,
        AMS_PARTNER_ID,
        get_secret,
    )

    # P8.4 Section H — same pre-flight refresh gap as the main Shopee app
    # token, fixed the same way: check expiry before any call, not after
    # a failure. Separate AMS_* credentials, never the production app's.
    # P11-BIS — routed through get_secret() (bundle -> legacy env ->
    # Keychain) instead of a raw keyring.get_password() call, so this
    # works on a GitHub Actions runner (no OS keychain) the same way the
    # main Shopee app's credentials already do.
    expire_at = get_secret(ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT)
    needs_refresh = True
    if expire_at:
        try:
            needs_refresh = int(expire_at) - time.time() <= 600
        except ValueError:
            needs_refresh = True
    if needs_refresh:
        from refresh_token_ams import main as _do_ams_refresh  # noqa: E402
        if _do_ams_refresh() != 0:
            raise RuntimeError("Shopee AMS access_token pre-flight refresh failed.")

    def D(x):
        # AMS performance metrics (roi in particular) return the literal
        # sentinel "--" when the metric is mathematically undefined (e.g.
        # ROI with zero spend), not zero — parse_numeric already treats
        # any non-numeric string as NULL, which is the correct semantic
        # here (production proof: control.etl_run_log shows this exact
        # field crashing the INSERT with "invalid input syntax for type
        # numeric" on 2026-09-17 and 2026-09-21, same product both times).
        return parse_numeric(x)

    def ams_get(path: str, extra: dict) -> dict:
        partner_key = get_secret(ACCOUNT_AMS_LIVE_PARTNER_KEY)
        access_token = get_secret(ACCOUNT_AMS_ACCESS_TOKEN)
        ams_shop_id = int(get_secret(ACCOUNT_AMS_SHOP_ID))
        ts = int(time.time())
        base = f"{AMS_PARTNER_ID}{path}{ts}{access_token}{ams_shop_id}"
        s = hmac.new(partner_key.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
        del partner_key
        params = {"partner_id": AMS_PARTNER_ID, "timestamp": ts, "sign": s,
                   "shop_id": ams_shop_id, "access_token": access_token}
        del access_token
        params.update(extra)
        r = requests.get(f"{AMS_API_HOST}{path}", params=params, timeout=20)
        data = r.json()
        data["_http_status"] = r.status_code
        data["_retry_after"] = _parse_retry_after(r.headers.get("Retry-After"))
        return data

    def paginate(path: str, base_params: dict):
        page_no = 1
        while True:
            data = ams_get(path, dict(base_params, page_no=page_no, page_size=20))
            if data.get("_http_status") in ic.TRANSIENT_HTTP:
                raise ic.TransientHTTPError(str(data.get("_http_status")), retry_after=data.get("_retry_after"))
            if data.get("error"):
                raise RuntimeError(f"AMS API error: {data.get('error')} - {data.get('message')}")
            resp = data.get("response") or {}
            rows = resp.get("list") or []
            for row in rows:
                yield row
            if not resp.get("has_more"):
                break
            page_no += 1
            time.sleep(0.3)

    conv_ctr = ic.Counters()
    perf_ctr = ic.Counters()

    days = _ams_compute_catchup_days(window_start, window_end, ic.now_utc(), target_date)

    def process_day(d):
        start, end = _ams_day_bounds_utc7(d)
        for order in ic.with_backoff(
            lambda: list(paginate("/api/v2/ams/get_conversion_report",
                                   {"place_order_time_start": start, "place_order_time_end": end})),
            "shopee ams conversion", log,
        ):
            for item in (order.get("items") or [{}]):
                cur.execute(
                    """
                    INSERT INTO core.fact_shopee_affiliate_conversion (
                        channel, shop_id, order_id, item_id, model_id, order_status, verified_status,
                        affiliate_id, affiliate_name, affiliate_username, ams_channel, order_type, buyer_status,
                        campaign_id, seller_campaign_type, promotion_id, item_name, price, qty, purchase_value,
                        refund_amount, order_brand_commission, item_brand_commission,
                        item_brand_commission_rate_to_affiliate, item_brand_commission_to_affiliate,
                        item_brand_commission_rate_to_mcn, item_brand_commission_to_mcn,
                        seller_service_fee_rate, seller_service_fee,
                        place_order_time, order_completed_time, conversion_completed_time, business_date,
                        commission_value_basis, source_updated_at, ingested_at, etl_run_id
                    ) VALUES (
                        'SHOPEE', %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s, %s, %s,
                        'ORDER_LEVEL_ACTUAL', now(), now(), %s
                    )
                    ON CONFLICT (channel, shop_id, order_id, COALESCE(item_id,''), COALESCE(model_id,''))
                    DO UPDATE SET
                        order_status=EXCLUDED.order_status, verified_status=EXCLUDED.verified_status,
                        affiliate_id=EXCLUDED.affiliate_id, affiliate_name=EXCLUDED.affiliate_name,
                        affiliate_username=EXCLUDED.affiliate_username, ams_channel=EXCLUDED.ams_channel,
                        order_type=EXCLUDED.order_type, buyer_status=EXCLUDED.buyer_status,
                        campaign_id=EXCLUDED.campaign_id, seller_campaign_type=EXCLUDED.seller_campaign_type,
                        promotion_id=EXCLUDED.promotion_id, item_name=EXCLUDED.item_name, price=EXCLUDED.price,
                        qty=EXCLUDED.qty, purchase_value=EXCLUDED.purchase_value, refund_amount=EXCLUDED.refund_amount,
                        order_brand_commission=EXCLUDED.order_brand_commission,
                        item_brand_commission=EXCLUDED.item_brand_commission,
                        item_brand_commission_rate_to_affiliate=EXCLUDED.item_brand_commission_rate_to_affiliate,
                        item_brand_commission_to_affiliate=EXCLUDED.item_brand_commission_to_affiliate,
                        item_brand_commission_rate_to_mcn=EXCLUDED.item_brand_commission_rate_to_mcn,
                        item_brand_commission_to_mcn=EXCLUDED.item_brand_commission_to_mcn,
                        seller_service_fee_rate=EXCLUDED.seller_service_fee_rate,
                        seller_service_fee=EXCLUDED.seller_service_fee,
                        order_completed_time=EXCLUDED.order_completed_time,
                        conversion_completed_time=EXCLUDED.conversion_completed_time,
                        business_date=EXCLUDED.business_date,
                        source_updated_at=now(), etl_run_id=EXCLUDED.etl_run_id
                    RETURNING (xmax = 0) AS inserted;
                    """,
                    (
                        shop_id, order.get("order_sn"), D(item.get("item_id")), D(item.get("model_id")),
                        order.get("order_status"), order.get("verified_status"),
                        D(order.get("affiliate_id")), order.get("affiliate_name"), order.get("affiliate_username"),
                        order.get("channel"), order.get("order_type"), order.get("buyer_status"),
                        D(item.get("attr_campaign_id")), item.get("seller_campaign_type"), D(item.get("promotion_id")),
                        item.get("item_name"), D(item.get("price")), D(item.get("qty")), D(item.get("purchase_value")),
                        D(item.get("refund_amount")), D(order.get("order_brand_commission")),
                        D(item.get("item_brand_commission")),
                        item.get("item_brand_commission_rate_to_affiliate"), D(item.get("item_brand_commission_to_affiliate")),
                        item.get("item_brand_commission_rate_to_mcn"), D(item.get("item_brand_commission_to_mcn")),
                        item.get("seller_service_fee_rate"), D(item.get("seller_service_fee")),
                        _ams_parse_ts(order.get("place_order_time")), _ams_parse_ts(order.get("order_completed_time")),
                        _ams_parse_ts(order.get("conversion_completed_time")), d,
                        etl_run_id,
                    ),
                )
                conv_ctr.record(cur.fetchone()[0])

        def upsert_perf(grain, aff_id, aff_name, aff_user, item_id, item_name, row):
            cur.execute(
                """
                INSERT INTO core.fact_shopee_affiliate_performance_daily (
                    grain_type, business_date, channel, shop_id, affiliate_id, affiliate_name, affiliate_username,
                    item_id, item_name, sales, items_sold, orders, clicks, est_commission, roi,
                    total_buyers, new_buyers, value_basis, source_updated_at, ingested_at, etl_run_id
                ) VALUES (
                    %s, %s, 'SHOPEE', %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, 'ESTIMATED_PERFORMANCE', now(), now(), %s
                )
                ON CONFLICT (grain_type, business_date, channel, shop_id, COALESCE(affiliate_id,''), COALESCE(item_id,''))
                DO UPDATE SET sales=EXCLUDED.sales, items_sold=EXCLUDED.items_sold, orders=EXCLUDED.orders,
                    clicks=EXCLUDED.clicks, est_commission=EXCLUDED.est_commission, roi=EXCLUDED.roi,
                    total_buyers=EXCLUDED.total_buyers, new_buyers=EXCLUDED.new_buyers,
                    source_updated_at=now(), etl_run_id=EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (grain, d, shop_id, aff_id, aff_name, aff_user, item_id, item_name,
                 D(row.get("sales")), D(row.get("items_sold") or row.get("gross_item_sold")),
                 D(row.get("orders")), D(row.get("clicks")), D(row.get("est_commission")), D(row.get("roi")),
                 D(row.get("total_buyers")), D(row.get("new_buyers")), etl_run_id),
            )
            perf_ctr.record(cur.fetchone()[0])

        ymd = d.strftime("%Y%m%d")
        shop_data = ic.with_backoff(
            lambda: ams_get("/api/v2/ams/get_shop_performance", {
                "channel": "AllChannel", "period_type": "Day", "start_date": ymd, "end_date": ymd,
                "order_type": "ConfirmedOrder",
            }), "shopee ams shop_performance", log,
        )
        if not shop_data.get("error"):
            upsert_perf("SHOP", None, None, None, None, None, shop_data.get("response") or {})

        for row in ic.with_backoff(
            lambda: list(paginate("/api/v2/ams/get_affiliate_performance", {
                "channel": "AllChannel", "period_type": "Day", "start_date": ymd, "end_date": ymd,
                "order_type": "ConfirmedOrder",
            })), "shopee ams affiliate_performance", log,
        ):
            aff_id = D(row.get("affiliate_id"))
            upsert_perf("AFFILIATE", str(aff_id) if aff_id is not None else None, row.get("affiliate_name"),
                        row.get("affiliate_username"), None, None, row)

        for row in ic.with_backoff(
            lambda: list(paginate("/api/v2/ams/get_product_performance", {
                "channel": "AllChannel", "period_type": "Day", "start_date": ymd, "end_date": ymd,
                "order_type": "ConfirmedOrder",
            })), "shopee ams product_performance", log,
        ):
            upsert_perf("PRODUCT", None, None, None, D(row.get("item_id")), row.get("item_name"), row)

    last_completed_date = _run_ams_catchup(days, process_day, log)

    result = {"affiliate_ams_conversion": conv_ctr.as_dict(), "affiliate_ams_performance": perf_ctr.as_dict()}
    effective_sync_end = _ams_effective_sync_end(days, last_completed_date)
    if effective_sync_end is not None:
        # P11-HEAL — only ever set when this call stopped partway through
        # a multi-day catch-up (see _run_ams_catchup); main() uses this
        # instead of window_end so sync_state advances only through
        # confirmed successful coverage, never past it.
        result["_effective_sync_end"] = effective_sync_end
    return result


def run_account_health(cur, etl_run_id, shop_id, window_start, window_end, log) -> dict:
    """Phase 6A — wires the previously-standalone
    artifacts/v0/_p71_shopee_account_health_ingest.py collector into the
    regular incremental cadence (see CADENCE_MINUTES in incr_common.py),
    fixing the staleness gap found in the P8.7S correction pass (last
    snapshot was 2026-09-14, 2 days stale, because nothing scheduled it
    to run again). Uses get_shop_performance (current, non-sunset
    endpoint) — single shop-level call, no pagination, no date param
    (Shopee proven live to return current-state only regardless of any
    historical filter). Dynamic metric_id preserved (no hard-coded
    metric list) per the original script's design, unchanged here.
    """
    client = ShopeeClient()
    resp = client.get("/api/v2/account_health/get_shop_performance", {})
    if resp.get("error"):
        raise RuntimeError(f"get_shop_performance: {resp.get('error')} {resp.get('message')}")
    body = resp.get("response", {}) or {}
    overall = body.get("overall_performance") or {}
    overall_rating = overall.get("rating")
    snapshot_date = ic.vn_date(window_end)

    ctr = ic.Counters()
    for m in body.get("metric_list", []):
        target = m.get("target") or {}
        cur.execute(
            """
            INSERT INTO core.fact_shopee_account_health_snapshot (
                shop_id, snapshot_date, metric_id, metric_name, metric_type, parent_metric_id,
                current_period, last_period, unit, target_value, target_comparator,
                exemption_end_date, overall_shop_rating, source_system, source_endpoint,
                value_basis, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SHOPEE','/api/v2/account_health/get_shop_performance','API_ACTUAL',%s)
            ON CONFLICT (shop_id, snapshot_date, metric_id) DO UPDATE SET
                metric_name = EXCLUDED.metric_name, current_period = EXCLUDED.current_period,
                last_period = EXCLUDED.last_period, target_value = EXCLUDED.target_value,
                target_comparator = EXCLUDED.target_comparator,
                overall_shop_rating = EXCLUDED.overall_shop_rating, etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                str(shop_id), snapshot_date, m.get("metric_id"), m.get("metric_name"), m.get("metric_type"),
                m.get("parent_metric_id") or None,
                parse_numeric(m.get("current_period")), parse_numeric(m.get("last_period")), m.get("unit"),
                parse_numeric(target.get("value")), target.get("comparator"),
                m.get("exemption_end_date") or None, parse_numeric(overall_rating), etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return {"account_health": ctr.as_dict()}


HANDLERS = {
    "orders": run_orders, "returns": run_returns, "finance": run_finance,
    "ads": run_ads, "product_inventory": run_product_inventory,
    "affiliate_ams": run_affiliate_ams, "account_health": run_account_health,
}


# ---------------------------------------------------------------------
# P4 — reconciliation snapshots (before/after) + row writers
# ---------------------------------------------------------------------

def snapshot_orders(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT count(*), COALESCE(sum(total_amount),0),
                  count(*) FILTER (WHERE order_status='CANCELLED')
           FROM core.fact_order WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s;""",
        (shop_id, business_date),
    )
    order_count, gmv, cancel_count = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(sum(qty),0) FROM core.fact_order_item WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    units = cur.fetchone()[0]
    return {"order_count": order_count, "gmv": gmv, "cancel_count": cancel_count, "units": units}


def snapshot_finance(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT settlement_type, count(*) FROM core.fact_settlement
           WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s GROUP BY settlement_type;""",
        (shop_id, business_date),
    )
    by_type = dict(cur.fetchall())
    total = sum(by_type.values())
    return {"total": total, "escrow_estimate": by_type.get("escrow_estimate", 0), "api_error": by_type.get("api_error", 0)}


def snapshot_returns(cur, shop_id, business_date) -> int:
    cur.execute(
        "SELECT count(*) FROM core.fact_return_refund WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    return cur.fetchone()[0]


def snapshot_ads(cur, shop_id, business_date) -> dict:
    cur.execute(
        "SELECT campaign_id, gmv, spend FROM core.fact_ads_daily WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s;",
        (shop_id, business_date),
    )
    return {row[0]: {"gmv": row[1], "spend": row[2]} for row in cur.fetchall()}


def snapshot_affiliate_ams(cur, shop_id, business_date) -> dict:
    cur.execute(
        """SELECT count(*), COALESCE(sum(item_brand_commission), 0)
           FROM core.fact_shopee_affiliate_conversion WHERE shop_id=%s AND business_date=%s;""",
        (shop_id, business_date),
    )
    rows, commission = cur.fetchone()
    return {"rows": rows, "commission": commission}


def reconcile_domain(cur, domain, shop_id, business_date, window_start, window_end, etl_run_id, log):
    """Runs the SAME handler used for incremental ingestion (no fetch/
    UPSERT logic duplicated), bracketed by a before/after DB snapshot,
    and writes audit.reconciliation_result rows. Returns
    (recon_row_summaries, worker_result)."""
    cur.execute(
        "SELECT count(*) FROM core.fact_order WHERE channel='SHOPEE' AND shop_id=%s AND business_date=%s;",
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
        recon_rows.append(ic.recon_row(cur, business_date, "orders_count", "SHOPEE", shop_id,
            before["order_count"], after["order_count"],
            "count(*) core.fact_order WHERE channel=SHOPEE AND business_date=D",
            date_basis, "all statuses", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_units", "SHOPEE", shop_id,
            before["units"], after["units"], "sum(qty) core.fact_order_item WHERE business_date=D",
            date_basis, "all statuses", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_gmv", "SHOPEE", shop_id,
            before["gmv"], after["gmv"],
            "sum(total_amount) core.fact_order WHERE business_date=D — order-level GMV, NOT escrow/settlement, NOT HH Net Sales",
            date_basis, "all statuses", "VND", etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "orders_cancel_count", "SHOPEE", shop_id,
            before["cancel_count"], after["cancel_count"], "count(*) WHERE order_status='CANCELLED'",
            date_basis, "CANCELLED", None, etl_run_id, is_new_date, override, extra))
    elif domain == "finance":
        before = snapshot_finance(cur, shop_id, business_date)
        result = run_finance(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_finance(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["settlement"]["inserted"], result["settlement"]["updated"])
        extra = {"ROWS_INSERTED": result["settlement"]["inserted"], "ROWS_UPDATED": result["settlement"]["updated"]}
        recon_rows.append(ic.recon_row(cur, business_date, "settlement_count", "SHOPEE", shop_id,
            before["total"], after["total"], "count(*) core.fact_settlement WHERE business_date=D",
            "business_date=D (assigned at write time, matches order's own business_date)",
            "all settlement_type", None, etl_run_id, is_new_date, override, extra))
        recon_rows.append(ic.recon_row(cur, business_date, "settlement_escrow_estimate_count", "SHOPEE", shop_id,
            before["escrow_estimate"], after["escrow_estimate"],
            "count(*) WHERE settlement_type='escrow_estimate' — Shopee's settled/estimated escrow state",
            "business_date=D", "escrow_estimate", None, etl_run_id, is_new_date, override, extra))
    elif domain == "returns":
        before = snapshot_returns(cur, shop_id, business_date)
        result = run_returns(cur, etl_run_id, shop_id, window_start, window_end, log)
        after = snapshot_returns(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["return"]["inserted"], result["return"]["updated"])
        recon_rows.append(ic.recon_row(cur, business_date, "returns_count", "SHOPEE", shop_id,
            before, after, "count(*) core.fact_return_refund WHERE business_date=D (create_time window)",
            "create_time (Asia/Ho_Chi_Minh)", "all", None, etl_run_id, is_new_date, override))
    elif domain == "ads":
        before = snapshot_ads(cur, shop_id, business_date)
        result = run_ads(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=business_date)
        after = snapshot_ads(cur, shop_id, business_date)
        override = ic.classify_from_counts(result["ads"]["inserted"], result["ads"]["updated"])
        for campaign_id in ("SHOP_TOTAL_DIRECT", "SHOP_TOTAL_BROAD"):
            b, a = before.get(campaign_id, {}), after.get(campaign_id, {})
            recon_rows.append(ic.recon_row(cur, business_date, f"ads_gmv_{campaign_id}", "SHOPEE", shop_id,
                b.get("gmv"), a.get("gmv"), f"fact_ads_daily.gmv WHERE campaign_id={campaign_id}",
                "business_date=D (requested Ads API date)", campaign_id, "VND", etl_run_id, is_new_date, override))
    elif domain == "affiliate_ams":
        before = snapshot_affiliate_ams(cur, shop_id, business_date)
        result = run_affiliate_ams(cur, etl_run_id, shop_id, window_start, window_end, log, target_date=business_date)
        after = snapshot_affiliate_ams(cur, shop_id, business_date)
        override = ic.classify_from_counts(
            result["affiliate_ams_conversion"]["inserted"], result["affiliate_ams_conversion"]["updated"])
        recon_rows.append(ic.recon_row(cur, business_date, "affiliate_ams_commission", "SHOPEE", shop_id,
            before.get("commission"), after.get("commission"),
            "sum(item_brand_commission) core.fact_shopee_affiliate_conversion WHERE business_date=D",
            "business_date=D (requested AMS report date)", "ORDER_LEVEL_ACTUAL", "VND", etl_run_id, is_new_date, override))
        recon_rows.append(ic.recon_row(cur, business_date, "affiliate_ams_conversion_rows", "SHOPEE", shop_id,
            before.get("rows"), after.get("rows"),
            "count(*) core.fact_shopee_affiliate_conversion WHERE business_date=D",
            "business_date=D (requested AMS report date)", "ORDER_LEVEL_ACTUAL", None, etl_run_id, is_new_date, override))
    else:
        raise ValueError(f"P4 reconciliation not implemented for Shopee domain {domain!r}")

    return recon_rows, result


def main() -> int:
    domain = sys.argv[1]
    window_start = datetime.fromisoformat(sys.argv[2])
    window_end = datetime.fromisoformat(sys.argv[3])
    etl_run_id = sys.argv[4]
    mode = sys.argv[5] if len(sys.argv) > 5 else "incremental"
    business_date_arg = sys.argv[6] if len(sys.argv) > 6 else None

    shop_id = get_secret(ACCOUNT_SHOP_ID)
    if not shop_id:
        print(json.dumps({"status": "FAIL", "error": "Shopee shop_id not found in Keychain"}))
        return 1

    log = ic.simple_log(f"SHOPEE/{domain}")
    conn = ic.get_db_conn()
    cur = conn.cursor()
    try:
        if mode == "reconcile":
            business_date = datetime.strptime(business_date_arg, "%Y-%m-%d").date()
            recon_rows, result = reconcile_domain(cur, domain, shop_id, business_date, window_start, window_end, etl_run_id, log)
            # P4 deliberately does NOT touch control.etl_sync_state — see
            # Section 2 ("do not damage P3 watermarks"). control.etl_run_log
            # (written by the coordinator, run_type='reconciliation') and
            # audit.reconciliation_result are P4's own, separate record.
            conn.commit()
            conn.close()
            print(json.dumps({"status": "PASS", "result": result, "recon_rows": recon_rows}, default=str))
            return 0

        result = HANDLERS[domain](cur, etl_run_id, shop_id, window_start, window_end, log)
        # P11-HEAL — a handler (currently only run_affiliate_ams) may
        # report it only got partway through a multi-day catch-up via
        # "_effective_sync_end"; every other domain never sets this key,
        # so .pop(...) falls back to window_end exactly as before.
        sync_end = result.pop("_effective_sync_end", None) or window_end
        ic.upsert_sync_state(cur, "SHOPEE", domain, shop_id, sync_end, ic.vn_date(sync_end), "success")
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
