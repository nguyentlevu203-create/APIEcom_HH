#!/usr/bin/env python3
"""
FIRST END-TO-END REPORTING PILOT — collector (Sections 4-13 of the spec).

READ-ONLY. Every call goes through tiktok_client's hard-coded allowlist —
a write-shaped call is refused in code before any request is sent.

    python3 collect.py --date 2026-09-07

Writes:
  raw/<date>_*.json              one file per API call/page, full metadata
  normalized/*.csv                orders, order_lines, finance_*, returns,
                                   affiliate_orders, product/video/live analytics
  logs/collect_<date>.log         plain-text call log (no secrets)

Does not compute KPIs, reconciliation, or the Excel/Markdown/CEO summary —
that's build_reports.py, which reads only the normalized/ CSVs this writes.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from pilot_common import (
    LOGS_DIR,
    NORMALIZED_DIR,
    RAW_DIR,
    PilotStopError,
    bootstrap_session,
    now_utc_iso,
    write_raw,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiktok_client import NetworkError, SecurityError  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover
    VN_TZ = None

MAX_PAGES = 200  # hard safety cap per domain — not "retry forever" (Section 24)
RATE_LIMIT_CODE = "36009002"
TRANSIENT_INTERNAL_CODE = "36009003"
MAX_RETRIES = 3


def _call_with_retry(fn, log: Logger, label: str):
    """Call fn() (a client.read_domain(...) invocation), retrying a bounded
    number of times on TikTok's own transient-error codes (rate limit /
    'internal error, retry later'). Never retries forever (Section 24) and
    never retries a genuine schema/auth error."""
    delay = 0.6
    for attempt in range(1, MAX_RETRIES + 1):
        resp = fn()
        code = str(resp.code)
        if resp.ok or code not in (RATE_LIMIT_CODE, TRANSIENT_INTERNAL_CODE):
            return resp
        log.log(f"{label}: transient code={code} attempt={attempt}/{MAX_RETRIES}, "
                f"backing off {delay:.1f}s")
        time.sleep(delay)
        delay *= 2.5
    return resp  # last attempt's response, whatever it was


class Logger:
    def __init__(self, path: Path):
        self.path = path
        self.f = open(path, "a", encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"{now_utc_iso()} {msg}"
        print(line)
        self.f.write(line + "\n")
        self.f.flush()


def day_window_epoch(report_date: str) -> tuple[int, int]:
    """SHOP LOCAL DATE window -> (start_epoch, end_epoch), end exclusive.
    Shop region is VN (shop_info.json) -> Asia/Ho_Chi_Minh."""
    y, m, d = (int(x) for x in report_date.split("-"))
    tz = VN_TZ
    start = datetime(y, m, d, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def next_day_str(report_date: str) -> str:
    y, m, d = (int(x) for x in report_date.split("-"))
    return (datetime(y, m, d) + timedelta(days=1)).strftime("%Y-%m-%d")


def write_csv(report_date: str, name: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    """Every normalized file is date-namespaced (orders_2026-09-07.csv) —
    running collect.py for a second date must never clobber the first
    date's normalized output (needed for the multi-date finance lifecycle
    in finance_lifecycle.py)."""
    stem = name[:-4] if name.endswith(".csv") else name
    path = NORMALIZED_DIR / f"{stem}_{report_date}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# ---------------------------------------------------------------------------
# Generic pagination
# ---------------------------------------------------------------------------

def paginate(
    log: Logger,
    session,
    domain: str,
    report_date: str,
    raw_prefix: str,
    *,
    query_base: Optional[dict[str, str]] = None,
    body_base: Optional[dict[str, Any]] = None,
    path_params: Optional[dict[str, str]] = None,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict[str, Any]], str]:
    """Calls client.read_domain(domain) repeatedly following next_page_token
    (tried as a query param, which matches every paginated endpoint used in
    this pilot). Returns (list_of_row_dicts_from_each_page_top_list_field,
    overall_status) where overall_status in {PASS, PASS_EMPTY, FAIL_*}.
    Writes one raw snapshot per page. Never raises for TikTok/network
    failures — SecurityError (allowlist bug) is the only thing that
    propagates."""
    all_rows: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    page_number = 0
    status = "PASS_EMPTY"

    while True:
        page_number += 1
        query = dict(query_base or {})
        if page_token:
            query["page_token"] = page_token
        try:
            resp = _call_with_retry(
                lambda: session.client.read_domain(
                    domain,
                    session.access_token,
                    shop_cipher=session.shop_info.get("shop_cipher"),
                    extra_query=query,
                    body=body_base,
                    path_params=path_params,
                ),
                log, f"{domain} page {page_number}",
            )
        except NetworkError as e:
            log.log(f"{domain} page {page_number}: FAIL_NETWORK {e}")
            status = "FAIL_NETWORK"
            break

        next_token = resp.data.get("next_page_token") if isinstance(resp.data, dict) else None
        write_raw(
            report_date,
            f"{report_date}_{raw_prefix}_page_{page_number:03d}.json",
            endpoint=domain,
            api_version=_version_of(domain),
            shop_info=session.shop_info,
            request_time_utc=now_utc_iso(),
            response=resp,
            page_number=page_number,
            next_page_token=next_token,
        )

        if not resp.ok:
            log.log(f"{domain} page {page_number}: code={resp.code} message={resp.message}")
            status = "FAIL_API"
            break

        rows = _list_field(resp.data)
        all_rows.extend(rows)
        log.log(f"{domain} page {page_number}: rows={len(rows)} next_page_token={'yes' if next_token else 'no'}")

        if not next_token or page_number >= max_pages:
            status = "PASS" if all_rows else "PASS_EMPTY"
            break
        page_token = next_token

    return all_rows, status


def _version_of(domain: str) -> str:
    from tiktok_client import READ_ENDPOINTS
    path = READ_ENDPOINTS[domain]["path"]
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) > 1 else "unknown"


def _list_field(data: dict[str, Any]) -> list[dict[str, Any]]:
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# 1. Authorized shop snapshot
# ---------------------------------------------------------------------------

def collect_authorized_shop(log: Logger, session, report_date: str) -> None:
    resp = session.client.get_authorized_shops(session.access_token)
    write_raw(
        report_date,
        f"{report_date}_authorized_shop.json",
        endpoint="authorized_shops",
        api_version="202309",
        shop_info=session.shop_info,
        request_time_utc=now_utc_iso(),
        response=resp,
    )
    log.log(f"authorized_shop: code={resp.code}")


# ---------------------------------------------------------------------------
# 2. Orders — search (collect IDs, full pagination) then batched detail
# ---------------------------------------------------------------------------

def collect_orders(log: Logger, session, report_date: str) -> dict[str, Any]:
    start_epoch, end_epoch = day_window_epoch(report_date)
    search_rows, search_status = paginate(
        log, session, "orders", report_date, "orders",
        query_base={"page_size": "50"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
    )
    order_ids = [r["id"] for r in search_rows if r.get("id")]
    log.log(f"orders search: {len(order_ids)} order ids in window "
            f"[{start_epoch},{end_epoch}) (shop-local {report_date})")

    detail_rows: list[dict[str, Any]] = []
    detail_status = "PASS_EMPTY"
    batch_no = 0
    for i in range(0, len(order_ids), 50):
        batch_no += 1
        chunk = order_ids[i:i + 50]
        try:
            resp = session.client.read_domain(
                "order_detail", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"),
                extra_query={"ids": ",".join(chunk)},
            )
        except NetworkError as e:
            log.log(f"order_detail batch {batch_no}: FAIL_NETWORK {e}")
            detail_status = "FAIL_NETWORK"
            continue
        write_raw(
            report_date, f"{report_date}_order_details_{batch_no:03d}.json",
            endpoint="order_detail", api_version="202309", shop_info=session.shop_info,
            request_time_utc=now_utc_iso(), response=resp, page_number=batch_no,
            extra_meta={"ids_requested": len(chunk)},
        )
        if not resp.ok:
            log.log(f"order_detail batch {batch_no}: code={resp.code} message={resp.message}")
            detail_status = "FAIL_API"
            continue
        rows = resp.data.get("orders", [])
        detail_rows.extend(rows)
        log.log(f"order_detail batch {batch_no}: {len(chunk)} ids requested, {len(rows)} returned")

    if detail_rows:
        detail_status = "PASS"

    return {
        "search_status": search_status,
        "detail_status": detail_status,
        "order_ids": order_ids,
        "order_details": detail_rows,
    }


def normalize_orders(report_date: str, order_details: list[dict[str, Any]]) -> None:
    order_rows = []
    line_rows = []
    for o in order_details:
        payment = o.get("payment") or {}
        order_rows.append({
            "order_id": o.get("id"),
            "status": o.get("status"),
            "create_time": o.get("create_time"),
            "update_time": o.get("update_time"),
            "paid_time": o.get("paid_time"),
            "cancel_time": o.get("cancel_time"),
            "user_id": o.get("user_id"),
            "currency": payment.get("currency"),
            "sub_total": payment.get("sub_total"),
            "shipping_fee": payment.get("shipping_fee"),
            "seller_discount": payment.get("seller_discount"),
            "platform_discount": payment.get("platform_discount"),
            "total_amount": payment.get("total_amount"),
            "original_total_product_price": payment.get("original_total_product_price"),
            "tax": payment.get("tax"),
            "original_shipping_fee": payment.get("original_shipping_fee"),
            "shipping_fee_seller_discount": payment.get("shipping_fee_seller_discount"),
            "shipping_fee_platform_discount": payment.get("shipping_fee_platform_discount"),
            "fulfillment_type": o.get("fulfillment_type"),
            "warehouse_id": o.get("warehouse_id"),
            "is_sample_order": o.get("is_sample_order"),
            "is_cod": o.get("is_cod"),
            "cancel_reason": o.get("cancel_reason"),
        })
        for li in o.get("line_items", []):
            line_rows.append({
                "order_id": o.get("id"),
                "line_item_id": li.get("id"),
                "product_id": li.get("product_id"),
                "product_name": li.get("product_name"),
                "sku_id": li.get("sku_id"),
                "sku_name": li.get("sku_name"),
                "seller_sku": li.get("seller_sku"),
                "quantity": 1,  # TikTok order lines are 1 unit per line_item; see README note in build_reports
                "original_price": li.get("original_price"),
                "sale_price": li.get("sale_price"),
                "seller_discount": li.get("seller_discount"),
                "platform_discount": li.get("platform_discount"),
                "currency": li.get("currency"),
                "display_status": li.get("display_status"),
                "package_status": li.get("package_status"),
                "cancel_reason": li.get("cancel_reason"),
            })
    write_csv(report_date, "orders.csv", order_rows, [
        "order_id", "status", "create_time", "update_time", "paid_time", "cancel_time",
        "user_id", "currency", "sub_total", "shipping_fee", "seller_discount",
        "platform_discount", "total_amount", "original_total_product_price", "tax",
        "original_shipping_fee", "shipping_fee_seller_discount",
        "shipping_fee_platform_discount", "fulfillment_type", "warehouse_id",
        "is_sample_order", "is_cod", "cancel_reason",
    ])
    write_csv(report_date, "order_lines.csv", line_rows, [
        "order_id", "line_item_id", "product_id", "product_name", "sku_id", "sku_name",
        "seller_sku", "quantity", "original_price", "sale_price", "seller_discount",
        "platform_discount", "currency", "display_status", "package_status", "cancel_reason",
    ])


# ---------------------------------------------------------------------------
# 3. Finance
# ---------------------------------------------------------------------------

def collect_finance(log: Logger, session, report_date: str, order_ids: list[str]) -> dict[str, Any]:
    # A. Statements — most recent N pages (no time filter param on this
    # endpoint in the current client; sorted DESC by statement_time so the
    # newest statements — the ones plausibly touching this report date —
    # come first).
    statement_rows, statements_status = paginate(
        log, session, "finance", report_date, "finance_statements",
        query_base={"page_size": "50", "sort_field": "statement_time", "sort_order": "DESC"},
        max_pages=10,
    )

    # D. Payments — same recency-window approach.
    start_epoch, _ = day_window_epoch(report_date)
    payments_window_start = start_epoch - 60 * 24 * 3600
    payments_rows, payments_status = paginate(
        log, session, "finance_payments", report_date, "finance_payments",
        query_base={
            "page_size": "50", "sort_field": "create_time", "sort_order": "DESC",
            "create_time_ge": str(payments_window_start), "create_time_lt": str(int(time.time())),
        },
        max_pages=10,
    )

    # Unsettled transactions — authoritative NOT_SETTLED_YET source.
    unsettled_rows, unsettled_status = paginate(
        log, session, "finance_unsettled", report_date, "finance_unsettled",
        query_base={"page_size": "50", "sort_field": "order_create_time", "sort_order": "DESC"},
        max_pages=10,
    )
    unsettled_by_order = {
        r.get("order_id"): r for r in unsettled_rows if r.get("order_id")
    }
    unsettled_out_rows = [{
        "order_id": r.get("order_id"),
        "order_create_time": r.get("order_create_time"),
        "currency": r.get("currency"),
        "status": r.get("status"),
        "unsettled_reason": r.get("unsettled_reason"),
        "estimated_settlement": r.get("estimated_settlement"),
        "est_revenue_amount": r.get("est_revenue_amount"),
        "est_fee_tax_amount": r.get("est_fee_tax_amount"),
        "est_shipping_cost_amount": r.get("est_shipping_cost_amount"),
        "est_settlement_amount": r.get("est_settlement_amount"),
    } for r in unsettled_rows]
    write_csv(report_date, "finance_unsettled.csv", unsettled_out_rows, [
        "order_id", "order_create_time", "currency", "status", "unsettled_reason",
        "estimated_settlement", "est_revenue_amount", "est_fee_tax_amount",
        "est_shipping_cost_amount", "est_settlement_amount",
    ])

    # C. Order Statement Transactions — one call per order_id (no batch
    # endpoint exists for this path per official docs).
    order_tx_rows: list[dict[str, Any]] = []
    match_rows: list[dict[str, Any]] = []
    statement_ids_seen: set[str] = set()
    for idx, oid in enumerate(order_ids, start=1):
        time.sleep(0.12)  # spread sequential per-order calls to avoid 36009002
        try:
            resp = _call_with_retry(
                lambda oid=oid: session.client.read_domain(
                    "finance_order_statement_transactions", session.access_token,
                    shop_cipher=session.shop_info.get("shop_cipher"),
                    path_params={"order_id": oid},
                ),
                log, f"finance_order_statement_transactions {oid}",
            )
        except NetworkError as e:
            log.log(f"finance_order_statement_transactions {oid}: FAIL_NETWORK {e}")
            match_rows.append({"order_id": oid, "finance_match_status": "UNKNOWN",
                                "reason": f"network_error:{e}"})
            continue

        if idx <= 25 or idx % 25 == 0:
            write_raw(
                report_date,
                f"{report_date}_finance_order_statement_transactions_{idx:04d}.json",
                endpoint="finance_order_statement_transactions", api_version="202501",
                shop_info=session.shop_info, request_time_utc=now_utc_iso(), response=resp,
                extra_meta={"order_id": oid},
            )

        # TikTok returns code=0 with all-zero / empty sku_transactions for
        # an order that exists but has not yet been assigned to any
        # statement (observed live, e.g. a COD order still IN_TRANSIT).
        # That is NOT a match — treat it the same as "not settled yet".
        has_real_settlement = resp.ok and bool(resp.data.get("sku_transactions"))
        if has_real_settlement:
            d = resp.data
            statement_ids = {
                sku.get("statement_id") for sku in (d.get("sku_transactions") or [])
                if sku.get("statement_id")
            }
            order_tx_rows.append({
                "order_id": d.get("order_id", oid),
                "order_create_time": d.get("order_create_time"),
                "currency": d.get("currency"),
                "revenue_amount": d.get("revenue_amount"),
                "fee_and_tax_amount": d.get("fee_and_tax_amount"),
                "shipping_cost_amount": d.get("shipping_cost_amount"),
                "settlement_amount": d.get("settlement_amount"),
                "sku_transaction_count": len(d.get("sku_transactions") or []),
                # A sku_transactions list could theoretically span >1
                # statement; store the primary one plus the full set.
                "statement_id": sorted(statement_ids)[0] if statement_ids else "",
                "statement_ids": ";".join(sorted(statement_ids)),
            })
            statement_ids_seen |= statement_ids
            match_rows.append({
                "order_id": oid,
                "finance_match_status": "MATCHED",
                "settlement_amount": d.get("settlement_amount"),
                "statement_ids": ";".join(sorted(statement_ids)),
                "reason": "order_statement_transactions returned non-empty sku_transactions",
            })
        elif resp.ok:
            u = unsettled_by_order.get(oid)
            match_rows.append({
                "order_id": oid,
                "finance_match_status": "NOT_SETTLED_YET",
                "settlement_amount": None,
                "statement_ids": "",
                "reason": (
                    f"unsettled_reason={u.get('unsettled_reason')!r}" if u else
                    "order_statement_transactions returned code=0 but empty "
                    "sku_transactions (order not yet assigned to a statement)"
                ),
            })
        else:
            if oid in unsettled_by_order:
                u = unsettled_by_order[oid]
                match_rows.append({
                    "order_id": oid,
                    "finance_match_status": "NOT_SETTLED_YET",
                    "settlement_amount": None,
                    "statement_ids": "",
                    "reason": f"unsettled_reason={u.get('unsettled_reason')!r} "
                              f"est_settlement={d_get(u, 'estimated_settlement')}",
                })
            else:
                msg = (resp.message or "").lower()
                if "not found" in msg or "not exist" in msg or "no data" in msg:
                    match_rows.append({
                        "order_id": oid, "finance_match_status": "NOT_SETTLED_YET",
                        "settlement_amount": None, "statement_ids": "",
                        "reason": f"api_message={resp.message!r} (order not found in "
                                  "finance yet — treated as not-settled, not as FAIL)",
                    })
                else:
                    match_rows.append({
                        "order_id": oid, "finance_match_status": "UNKNOWN",
                        "settlement_amount": None, "statement_ids": "",
                        "reason": f"code={resp.code} message={resp.message!r}",
                    })

    # B. Statement Transactions — fetch detail once per statement actually
    # referenced by our target-date orders (dedup, capped).
    statement_tx_rows: list[dict[str, Any]] = []
    for sid in sorted(statement_ids_seen)[:100]:
        time.sleep(0.15)
        try:
            resp = _call_with_retry(
                lambda sid=sid: session.client.read_domain(
                    "finance_statement_transactions", session.access_token,
                    shop_cipher=session.shop_info.get("shop_cipher"),
                    path_params={"statement_id": sid},
                    extra_query={"sort_field": "order_create_time", "page_size": "100"},
                ),
                log, f"finance_statement_transactions {sid}",
            )
        except NetworkError as e:
            log.log(f"finance_statement_transactions {sid}: FAIL_NETWORK {e}")
            continue
        write_raw(
            report_date, f"{report_date}_finance_statement_transactions_{sid}.json",
            endpoint="finance_statement_transactions", api_version="202501",
            shop_info=session.shop_info, request_time_utc=now_utc_iso(), response=resp,
            extra_meta={"statement_id": sid},
        )
        if resp.ok:
            for tx in resp.data.get("transactions", []):
                statement_tx_rows.append({
                    "statement_id": sid,
                    "transaction_id": tx.get("id"),
                    "type": tx.get("type"),
                    "order_id": tx.get("order_id"),
                    "order_create_time": tx.get("order_create_time"),
                    # adjustment_amount only exists at this (statement-transaction)
                    # level — Get Order Statement Transactions does not expose it.
                    "adjustment_amount": tx.get("adjustment_amount"),
                    "revenue_amount": tx.get("revenue_amount"),
                    "fee_tax_amount": tx.get("fee_tax_amount"),
                    "shipping_cost_amount": tx.get("shipping_cost_amount"),
                    "settlement_amount": tx.get("settlement_amount"),
                })

    write_csv(report_date, "finance_statements.csv", statement_rows, sorted({
        k for r in statement_rows for k in r.keys()
    }) or ["id", "status", "create_time"])
    write_csv(report_date, "payments.csv", payments_rows, sorted({
        k for r in payments_rows for k in r.keys()
    }) or ["id", "status", "create_time"])
    write_csv(report_date, "finance_transactions.csv", statement_tx_rows, [
        "statement_id", "transaction_id", "type", "order_id", "order_create_time",
        "adjustment_amount", "revenue_amount", "fee_tax_amount", "shipping_cost_amount",
        "settlement_amount",
    ])
    write_csv(report_date, "order_finance_transactions.csv", order_tx_rows, [
        "order_id", "order_create_time", "currency", "revenue_amount",
        "fee_and_tax_amount", "shipping_cost_amount", "settlement_amount",
        "sku_transaction_count", "statement_id", "statement_ids",
    ])
    write_csv(report_date, "finance_match_status.csv", match_rows, [
        "order_id", "finance_match_status", "settlement_amount", "statement_ids", "reason",
    ])

    return {
        "statements_status": statements_status,
        "payments_status": payments_status,
        "unsettled_status": unsettled_status,
        "statement_count": len(statement_rows),
        "payments_count": len(payments_rows),
        "order_tx_count": len(order_tx_rows),
        "match_rows": match_rows,
    }


def d_get(d: dict, key: str):
    return d.get(key)


# ---------------------------------------------------------------------------
# 4. Returns
# ---------------------------------------------------------------------------

def collect_returns(log: Logger, session, report_date: str) -> str:
    start_epoch, end_epoch = day_window_epoch(report_date)
    rows, status = paginate(
        log, session, "return_refund", report_date, "returns",
        query_base={"page_size": "50", "sort_field": "create_time", "sort_order": "DESC"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
    )
    out_rows = []
    for r in rows:
        refund = r.get("refund_amount") or {}
        out_rows.append({
            "return_id": r.get("id") or r.get("return_id"),
            "order_id": r.get("order_id"),
            "return_type": r.get("return_type"),
            "return_status": r.get("return_status"),
            "return_reason": r.get("return_reason") or r.get("reason_text"),
            "create_time": r.get("create_time"),
            "update_time": r.get("update_time"),
            # P8.6 fix: real API field is refund_total, not "amount" (see incr_worker.py run_returns)
            "refund_amount": refund.get("refund_total") if isinstance(refund, dict) else refund,
            "currency": refund.get("currency") if isinstance(refund, dict) else None,
        })
    write_csv(report_date, "returns.csv", out_rows, [
        "return_id", "order_id", "return_type", "return_status", "return_reason",
        "create_time", "update_time", "refund_amount", "currency",
    ])
    return status


# ---------------------------------------------------------------------------
# 4b. Products / Inventory (daily pipeline step 05) — full catalog snapshot,
# not date-windowed (products aren't created "on" a report date the way
# orders are; this is a point-in-time snapshot of current catalog state,
# needed later for the COGS join by seller_sku/sku_id).
# ---------------------------------------------------------------------------

def collect_products(log: Logger, session, report_date: str) -> str:
    rows, status = paginate(
        log, session, "product", report_date, "products",
        query_base={"page_size": "100"},
        body_base={"status": "ALL"},
    )
    out_rows = []
    for p in rows:
        for sku in p.get("skus", []):
            price = sku.get("price") or {}
            inventory = sku.get("inventory") or []
            total_qty = sum(D_int(i.get("quantity")) for i in inventory)
            out_rows.append({
                "product_id": p.get("id"),
                "product_title": p.get("title"),
                "product_status": p.get("status"),
                "sku_id": sku.get("id"),
                "seller_sku": sku.get("seller_sku"),
                "sku_status": (sku.get("status_info") or {}).get("status"),
                "price_currency": price.get("currency"),
                "price_tax_exclusive": price.get("tax_exclusive_price"),
                "inventory_total_qty": total_qty,
                "warehouse_count": len(inventory),
            })
    write_csv(report_date, "products.csv", out_rows, [
        "product_id", "product_title", "product_status", "sku_id", "seller_sku",
        "sku_status", "price_currency", "price_tax_exclusive", "inventory_total_qty",
        "warehouse_count",
    ])
    return status


def D_int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 5. Affiliate
# ---------------------------------------------------------------------------

def collect_affiliate(log: Logger, session, report_date: str) -> str:
    start_epoch, end_epoch = day_window_epoch(report_date)
    rows, status = paginate(
        log, session, "affiliate", report_date, "affiliate_orders",
        query_base={"page_size": "50"},
        body_base={"create_time_ge": start_epoch, "create_time_lt": end_epoch},
    )
    # One row per (order_id, sku): the response's top-level "id" IS the
    # TikTok Shop order_id (confirmed live — same ID format/space as
    # orders.csv's order_id), with a nested "skus" list per commission line.
    out_rows = []
    for r in rows:
        order_id = r.get("id")
        for sku in r.get("skus", []):
            price = sku.get("price") or {}
            est_base = sku.get("estimated_commission_base") or {}
            est_ads_commission = sku.get("estimated_paid_shop_ads_commission") or {}
            out_rows.append({
                "order_id": order_id,
                "order_create_time": r.get("create_time"),
                "delivery_time": r.get("delivery_time"),
                "product_id": sku.get("product_id"),
                "sku_id": sku.get("sku_id"),
                "quantity": sku.get("quantity"),
                "price_amount": price.get("amount"),
                "price_currency": price.get("currency"),
                "creator_username": sku.get("creator_username"),
                "content_id": sku.get("content_id"),
                "content_type": sku.get("content_type"),
                "commission_model": sku.get("commission_model"),
                "shop_ads_commission_rate": sku.get("shop_ads_commission_rate"),
                "estimated_commission_base_amount": est_base.get("amount"),
                "estimated_commission_base_currency": est_base.get("currency"),
                "estimated_paid_shop_ads_commission_amount": est_ads_commission.get("amount"),
                "settlement_status": sku.get("settlement_status"),
                "fully_return": sku.get("fully_return"),
                "target_collaboration_id": sku.get("target_collaboration_id"),
            })
    write_csv(report_date, "affiliate_orders.csv", out_rows, [
        "order_id", "order_create_time", "delivery_time", "product_id", "sku_id",
        "quantity", "price_amount", "price_currency", "creator_username", "content_id",
        "content_type", "commission_model", "shop_ads_commission_rate",
        "estimated_commission_base_amount", "estimated_commission_base_currency",
        "estimated_paid_shop_ads_commission_amount", "settlement_status", "fully_return",
        "target_collaboration_id",
    ])
    return status


# ---------------------------------------------------------------------------
# 6/7/8. Analytics — product / video / live
# ---------------------------------------------------------------------------

def _flatten(prefix: str, obj: Any, out: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, ensure_ascii=False) if obj and isinstance(obj[0], dict) else ";".join(str(x) for x in obj)
    else:
        out[prefix] = obj


def collect_product_analytics(log: Logger, session, report_date: str) -> str:
    rows, status = paginate(
        log, session, "product_analytics", report_date, "product_analytics",
        query_base={
            "start_date_ge": report_date, "end_date_lt": next_day_str(report_date),
            "currency": "LOCAL", "page_size": "100",
        },
    )
    flat_rows = []
    for r in rows:
        flat: dict[str, Any] = {}
        _flatten("", r, flat)
        flat_rows.append(flat)
    fieldnames = sorted({k for r in flat_rows for k in r.keys()})
    write_csv(report_date, "product_analytics.csv", flat_rows, fieldnames)
    return status


def collect_video_analytics(log: Logger, session, report_date: str) -> str:
    rows, status = paginate(
        log, session, "video_analytics", report_date, "video_analytics",
        query_base={
            "start_date_ge": report_date, "end_date_lt": next_day_str(report_date),
            "currency": "LOCAL", "page_size": "100",
        },
    )
    flat_rows = []
    for r in rows:
        flat: dict[str, Any] = {}
        _flatten("", r, flat)
        flat_rows.append(flat)
    fieldnames = sorted({k for r in flat_rows for k in r.keys()})
    write_csv(report_date, "video_analytics.csv", flat_rows, fieldnames)
    return status


def collect_live_analytics(log: Logger, session, report_date: str) -> str:
    rows, status = paginate(
        log, session, "live_analytics", report_date, "live_analytics",
        query_base={
            "start_date_ge": report_date, "end_date_lt": next_day_str(report_date),
            "currency": "LOCAL", "page_size": "100",
        },
    )
    flat_rows = []
    for r in rows:
        flat: dict[str, Any] = {}
        _flatten("", r, flat)
        flat_rows.append(flat)
    fieldnames = sorted({k for r in flat_rows for k in r.keys()}) or ["id", "title", "start_time"]
    write_csv(report_date, "live_analytics.csv", flat_rows, fieldnames)
    return status


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Shop-local report date, YYYY-MM-DD")
    args = ap.parse_args()
    report_date = args.date

    log = Logger(LOGS_DIR / f"collect_{report_date}.log")
    log.log(f"=== PILOT COLLECT start for {report_date} ===")

    try:
        session = bootstrap_session()
    except PilotStopError as e:
        log.log(f"STOP: {e}")
        print(str(e))
        return 1

    log.log(f"shop={session.shop_info.get('shop_name')} region={session.shop_info.get('region')}")

    domain_status: dict[str, str] = {}

    try:
        collect_authorized_shop(log, session, report_date)
        domain_status["authorized_shop"] = "PASS"
    except SecurityError:
        raise
    except Exception as e:
        log.log(f"authorized_shop: EXC {e}")
        domain_status["authorized_shop"] = "FAIL"

    orders_result = collect_orders(log, session, report_date)
    normalize_orders(report_date, orders_result["order_details"])
    domain_status["orders"] = orders_result["search_status"]
    domain_status["order_detail"] = orders_result["detail_status"]

    finance_result = collect_finance(log, session, report_date, orders_result["order_ids"])
    domain_status["finance_statements"] = finance_result["statements_status"]
    domain_status["finance_payments"] = finance_result["payments_status"]
    domain_status["finance_unsettled"] = finance_result["unsettled_status"]

    domain_status["returns"] = collect_returns(log, session, report_date)
    domain_status["products"] = collect_products(log, session, report_date)
    domain_status["affiliate"] = collect_affiliate(log, session, report_date)
    domain_status["product_analytics"] = collect_product_analytics(log, session, report_date)
    domain_status["video_analytics"] = collect_video_analytics(log, session, report_date)
    domain_status["live_analytics"] = collect_live_analytics(log, session, report_date)

    summary_path = NORMALIZED_DIR / f"_collect_summary_{report_date}.json"
    summary_path.write_text(json.dumps({
        "report_date": report_date,
        "collected_at_utc": now_utc_iso(),
        "order_ids_count": len(orders_result["order_ids"]),
        "domain_status": domain_status,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    log.log(f"=== PILOT COLLECT done. Status: {json.dumps(domain_status)} ===")
    print(json.dumps(domain_status, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
