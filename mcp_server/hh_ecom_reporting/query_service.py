#!/usr/bin/env python3
"""
P8.0 — HH Ecom read-only reporting query service.

Every function here is a fixed, parameterized query against ONE or more
already-approved mart.v_ai_*/mart.v_ceo_* views, executed only via the
hh_ai_reader role (SELECT-only, zero grants on core.*/control.*/audit.*).
No function accepts or builds SQL from caller input beyond bound
parameters on a small, validated set of columns. There is no
execute_sql/raw_sql/run_query tool anywhere in this module or its caller
(server.py) — see P8_0_MCP_SECURITY.md.

Connection: prefers the HH_AI_READER_DATABASE_URL environment variable
(for portable/remote deployment); falls back to the project's existing
macOS Keychain convention for local development. Never hard-coded.
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras

try:
    import keyring
except ImportError:  # pragma: no cover - keyring is macOS-local-dev only
    keyring = None

SERVICE = "HH_ECOM_NEON"
ACCOUNT = "hh_ai_reader_database_url"
ENV_VAR = "HH_AI_READER_DATABASE_URL"

STATEMENT_TIMEOUT_MS = 8000  # query timeout — Section 7
MAX_ROW_LIMIT = 1000
DEFAULT_ROW_LIMIT = 200
MAX_DATE_RANGE_DAYS = 31  # Section 7 — 31-day maximum query range

VALID_PLATFORMS = {"SHOPEE", "TIKTOK"}
VALID_ACCOUNT_TYPES = {"ALL", "AFFILIATE_ACCOUNTS"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CROSSWALK_CSV = Path(__file__).resolve().parents[2] / "artifacts" / "v0" / "P7_1_PLATFORM_KPI_CROSSWALK.csv"


# ---------------------------------------------------------------------------
# Validation errors — never leak raw DB errors to the caller (Section 7)
# ---------------------------------------------------------------------------

class ValidationError(ValueError):
    """Raised for any invalid caller input. Safe to surface verbatim."""


def _validate_date(label: str, value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{label} must be a YYYY-MM-DD date string, got: {value!r}")
    return value


def _validate_range(from_date: str, to_date: str) -> None:
    _validate_date("from_date", from_date)
    _validate_date("to_date", to_date)
    if from_date > to_date:
        raise ValidationError("from_date must be on or before to_date")
    import datetime
    d0 = datetime.date.fromisoformat(from_date)
    d1 = datetime.date.fromisoformat(to_date)
    if (d1 - d0).days + 1 > MAX_DATE_RANGE_DAYS:
        raise ValidationError(
            f"date range exceeds the {MAX_DATE_RANGE_DAYS}-day maximum "
            f"({from_date}..{to_date} is {(d1 - d0).days + 1} days)"
        )


def _validate_platform(platform: Optional[str]) -> Optional[str]:
    if platform is None:
        return None
    p = str(platform).strip().upper()
    if p not in VALID_PLATFORMS:
        raise ValidationError(f"platform must be one of {sorted(VALID_PLATFORMS)}, got: {platform!r}")
    return p


def _validate_account_type(account_type: Optional[str]) -> Optional[str]:
    if account_type is None:
        return None
    a = str(account_type).strip().upper()
    if a not in VALID_ACCOUNT_TYPES:
        raise ValidationError(f"account_type must be one of {sorted(VALID_ACCOUNT_TYPES)}, got: {account_type!r}")
    return a


def _validate_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_ROW_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise ValidationError(f"limit must be an integer, got: {limit!r}")
    if n <= 0:
        raise ValidationError("limit must be a positive integer")
    return min(n, MAX_ROW_LIMIT)


# ---------------------------------------------------------------------------
# connection + observability (Section 8) — no secrets, no payloads logged
# ---------------------------------------------------------------------------

REQUEST_LOG: list[dict] = []


def _log(tool: str, params_safe: dict, status: str, latency_ms: float, row_count: int = 0):
    """Section 8: log ONLY timestamp, tool_name, safe parameter metadata
    (dates/platform/account_type/limit — never a full response payload),
    duration, row_count, success/failure. Never a secret, connection
    string, or token. Written as one JSON line to stderr (captured by
    whatever process supervisor runs the server) AND kept in-memory for
    this-process introspection/tests."""
    import datetime
    import json as _json
    entry = {
        "ts": time.time(), "tool": tool, "params": params_safe,
        "status": status, "latency_ms": round(latency_ms, 1), "row_count": row_count,
    }
    REQUEST_LOG.append(entry)
    log_line = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tool_name": tool, "params": params_safe, "duration_ms": round(latency_ms, 1),
        "row_count": row_count, "outcome": "success" if status == "OK" else "failure",
    }
    print(f"MCP_AUDIT {_json.dumps(log_line, default=str)}", file=sys.stderr, flush=True)


def _get_connection_string() -> str:
    url = os.environ.get(ENV_VAR)
    if url:
        return url
    if keyring is not None:
        url = keyring.get_password(SERVICE, ACCOUNT)
        if url:
            return url
    raise RuntimeError(
        f"hh_ai_reader connection string not found. Set the {ENV_VAR} environment "
        "variable, or run on a host with the project's Keychain entry configured."
    )


def _conn():
    url = _get_connection_string()
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor,
                             connect_timeout=5)
    del url
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    cur.close()
    return conn


def _to_jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = str(v) if hasattr(v, "isoformat") else v
    return out


def _run(tool_name: str, sql: str, params: tuple, params_safe: dict) -> list[dict]:
    """Executes exactly one fixed, parameterized statement. Never logs SQL
    text or a full response payload — only safe metadata (Section 8)."""
    t0 = time.time()
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = [_to_jsonable(dict(r)) for r in cur.fetchall()]
        conn.close()
        _log(tool_name, params_safe, "OK", (time.time() - t0) * 1000, len(rows))
        return rows
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 — never leak DB error detail (Section 7)
        _log(tool_name, params_safe, f"ERROR:{type(exc).__name__}", (time.time() - t0) * 1000)
        raise RuntimeError("A read-only reporting query failed. No further detail is exposed.") from None


# ---------------------------------------------------------------------------
# shared cost/status envelope helpers — reused, not reimplemented per tool
# ---------------------------------------------------------------------------

# Static classification for cost lines that are structurally never expected
# to be populated yet (per P7.2's locked CM2 contract) — used only to give
# ChatGPT an honest status, never to fabricate a value.
_KNOWN_MISSING_COST_STATUS = {
    "ads_spend": "SEPARATE_API_REQUIRED",
    "booking_kol_koc": "MISSING_SOURCE",
    "live_inhouse_cost": "MISSING_SOURCE",
}

_COST_LINE_LABELS = [
    ("fixed_fee", "platform_fixed_fee"),
    ("service_fee", "platform_service_fee"),
    ("payment_fee", "payment_fee"),
    ("vxp_fee", "vxp_fee"),
    ("infrastructure_fee", "infrastructure_fee"),
    ("affiliate_fee", "affiliate_commission_cost"),
    ("ads_spend", "ads_spend"),
    ("hh_internal_packaging_cost", "hh_internal_packaging_cost"),
    ("platform_packaging_or_fulfillment_fee", "platform_packaging_or_fulfillment_fee"),
    ("cancel_return_logistics_cost", "cancel_return_logistics_cost"),
    ("booking_kol_koc", "booking_kol_koc_cost"),
    ("live_inhouse_cost", "live_inhouse_cost"),
    ("backoffice_cost", "backoffice_cost"),
]


def _cost_status(column: str, value) -> str:
    if value is not None:
        return "READY"
    return _KNOWN_MISSING_COST_STATUS.get(column, "MISSING_SOURCE")


def _cm2_missing_sources(row: dict) -> list[str]:
    missing = []
    if row.get("ads_spend") is None:
        missing.append("TIKTOK_ADS_SEPARATE_API_REQUIRED")
    if row.get("booking_kol_koc") is None:
        missing.append("BOOKING_KOL_KOC_MISSING_SOURCE")
    if row.get("live_inhouse_cost") is None:
        missing.append("LIVE_INHOUSE_COST_MISSING_SOURCE")
    if row.get("backoffice_cost") is None:
        missing.append("BACKOFFICE_COST_RULE_NOT_APPROVED")
    return missing


# ---------------------------------------------------------------------------
# TOOL 1 — get_ecom_overview
# ---------------------------------------------------------------------------

def get_ecom_overview(from_date: str, to_date: str, platform: Optional[str] = None) -> dict:
    _validate_range(from_date, to_date)
    platform = _validate_platform(platform)

    sql = (
        "SELECT business_date, channel, orders, units, platform_gmv, "
        "net_sales, net_sales_status, net_sales_basis, "
        "total_cogs, gm1, gm1_margin, gm1_status, "
        "cm1, cm1_margin, cm1_status, "
        "cm2, cm2_margin, cm2_status, "
        "ads_spend, booking_kol_koc, live_inhouse_cost, backoffice_cost, "
        "profit, profit_margin, profit_status, data_freshness_status "
        "FROM mart.v_ceo_ecom_daily WHERE business_date BETWEEN %s AND %s"
    )
    params: tuple = (from_date, to_date)
    if platform:
        sql += " AND channel = %s"
        params += (platform,)
    sql += " ORDER BY business_date, channel"

    rows = _run("get_ecom_overview", sql, params,
                {"from_date": from_date, "to_date": to_date, "platform": platform})

    out = []
    for r in rows:
        cm2_missing = _cm2_missing_sources(r)
        out.append({
            "platform": r["channel"],
            "business_date": r["business_date"],
            "orders": r["orders"],
            "units": r["units"],
            # explicitly separate — Platform GMV is never Net Sales
            "platform_gmv": r["platform_gmv"],
            "net_sales": {"value": r["net_sales"], "status": r["net_sales_status"], "value_basis": r["net_sales_basis"]},
            "cogs": {"value": r["total_cogs"], "status": "READY" if r["total_cogs"] is not None else "MISSING_SOURCE"},
            "gm1": {"value": r["gm1"], "status": r["gm1_status"]},
            "gm1_margin_pct": r["gm1_margin"],
            "cm1": {"value": r["cm1"], "status": r["cm1_status"]},
            "cm1_margin_pct": r["cm1_margin"],
            "cm2": {"value": r["cm2"], "status": r["cm2_status"], "missing_sources": cm2_missing or None},
            "cm2_margin_pct": r["cm2_margin"],
            "profit": {"value": r["profit"], "status": r["profit_status"]},
            "profit_margin_pct": r["profit_margin"],
            "data_freshness_status": r["data_freshness_status"],
            "source": "mart.v_ceo_ecom_daily",
        })
    return {"rows": out, "row_count": len(out), "date_range": [from_date, to_date], "platform_filter": platform}


# ---------------------------------------------------------------------------
# TOOL 2 — get_cost_breakdown
# ---------------------------------------------------------------------------

def get_cost_breakdown(from_date: str, to_date: str, platform: Optional[str] = None) -> dict:
    _validate_range(from_date, to_date)
    platform = _validate_platform(platform)

    cols = ", ".join(c for c, _ in _COST_LINE_LABELS)
    sql = (
        f"SELECT business_date, channel, {cols} "
        "FROM mart.v_ceo_ecom_daily WHERE business_date BETWEEN %s AND %s"
    )
    params: tuple = (from_date, to_date)
    if platform:
        sql += " AND channel = %s"
        params += (platform,)
    sql += " ORDER BY business_date, channel"

    rows = _run("get_cost_breakdown", sql, params,
                {"from_date": from_date, "to_date": to_date, "platform": platform})

    out = []
    for r in rows:
        for column, cost_name in _COST_LINE_LABELS:
            value = r.get(column)
            out.append({
                "cost_name": cost_name,
                "amount": value,
                "amount_basis": "VND, per-day per-channel" if value is not None else None,
                "platform": r["channel"],
                "business_date": r["business_date"],
                "source": "mart.v_ceo_ecom_daily",
                "status": _cost_status(column, value),
                "value_basis": "DERIVED" if value is not None else None,
            })
    return {"rows": out, "row_count": len(out), "date_range": [from_date, to_date], "platform_filter": platform,
            "note": "amount=NULL always means the real status column explains why — never a fabricated zero."}


# ---------------------------------------------------------------------------
# TOOL 3 — get_operations (Shopee Account Health; snapshot/current-state only)
# ---------------------------------------------------------------------------

_METRIC_ID_LABELS = {
    1: "Late Shipment Rate", 3: "Non-Fulfilment Rate", 4: "Preparation Time",
    11: "Chat Response Rate", 12: "Pre-order Listing Rate", 15: "Pre-order Listing Count",
    21: "Response Time", 22: "Shop Rating", 23: "Non-Responded Chats", 25: "Fast Handover Rate",
    29: "Average Response Time", 42: "Cancellation Rate", 43: "Return-refund Rate",
    52: "Severe Listing Violations", 53: "Other Listing Violations", 54: "Prohibited Listings",
    55: "Counterfeit/IP Infringement", 56: "Spam Listings", 2011: "PQR Products",
}
# Metric IDs the task lists as wanted but confirmed ABSENT from this shop's
# real metric_list in P7.1 — reported honestly as MISSING_SOURCE, never
# silently dropped or set to zero.
_EXPECTED_BUT_ABSENT_IDS = {21, 23, 25, 29}


def get_operations(platform: str, date: Optional[str] = None) -> dict:
    platform = _validate_platform(platform)
    if platform != "SHOPEE":
        return {
            "rows": [], "row_count": 0, "platform": platform,
            "note": "Account Health / operational KPIs are only proven for SHOPEE this phase. "
                    "TikTok has no equivalent violation/operations API (see get_data_coverage).",
        }
    if date:
        _validate_date("date", date)

    if date:
        sql = (
            "SELECT shop_id, snapshot_date, metric_id, metric_name, current_period, last_period, "
            "unit, target_value, target_comparator, overall_shop_rating, value_basis, last_updated_at "
            "FROM mart.v_ai_operations_daily WHERE snapshot_date = %s ORDER BY metric_id"
        )
        params: tuple = (date,)
    else:
        # latest snapshot: every metric row for the single most recent
        # snapshot_date, not an arbitrary single row (no LIMIT 1 trap).
        sql = (
            "SELECT shop_id, snapshot_date, metric_id, metric_name, current_period, last_period, "
            "unit, target_value, target_comparator, overall_shop_rating, value_basis, last_updated_at "
            "FROM mart.v_ai_operations_daily WHERE snapshot_date = ("
            "SELECT MAX(snapshot_date) FROM mart.v_ai_operations_daily) ORDER BY metric_id"
        )
        params = ()

    rows = _run("get_operations", sql, params, {"platform": platform, "date": date})

    out = []
    seen_ids = set()
    for r in rows:
        seen_ids.add(r["metric_id"])
        # Every row here was actually returned by Shopee's account-health API
        # for this snapshot — that is API_ACTUAL regardless of whether the
        # value itself is null. A null current_period on a metric Shopee DID
        # return (e.g. listing-violation counts) is a real "nothing to
        # report" value proven in P7.1, not a missing source — conflating
        # the two was a real bug caught by this phase's MCP-to-mart
        # reconciliation. Only metric_ids entirely ABSENT from the result
        # set (handled below) are genuinely MISSING_SOURCE.
        out.append({
            "metric": _METRIC_ID_LABELS.get(r["metric_id"], r["metric_name"]),
            "value": r["current_period"],
            "unit": r["unit"],
            "snapshot_date": r["snapshot_date"],
            "source": "mart.v_ai_operations_daily (Shopee get_shop_performance)",
            "status": "API_ACTUAL",
            "blocking_reason": None if r["current_period"] is not None else
                "Shopee returned this metric with a null value this period (a real reported "
                "state, e.g. zero violations — not fabricated, not a missing source).",
        })
    for mid in _EXPECTED_BUT_ABSENT_IDS - seen_ids:
        out.append({
            "metric": _METRIC_ID_LABELS[mid], "value": None, "unit": None,
            "snapshot_date": rows[0]["snapshot_date"] if rows else date,
            "source": "mart.v_ai_operations_daily (Shopee get_shop_performance)",
            "status": "MISSING_SOURCE",
            "blocking_reason": "Not returned by Shopee's account-health API for this shop as of the last snapshot (confirmed absent in P7.1's live probe).",
        })
    return {
        "rows": out, "row_count": len(out), "platform": platform,
        "note": "Account Health is snapshot/current-state data only — Shopee's API has no historical "
                "backfill (proven in P7.1: identical response with/without a date filter). Do not "
                "treat this as a time series before the date this pilot's snapshots began.",
    }


# ---------------------------------------------------------------------------
# TOOL 4 — get_video_performance
# ---------------------------------------------------------------------------

def get_video_performance(from_date: str, to_date: str, platform: str,
                           account_type: Optional[str] = None, limit: Optional[int] = None) -> dict:
    _validate_range(from_date, to_date)
    platform = _validate_platform(platform)
    account_type = _validate_account_type(account_type)
    limit = _validate_limit(limit)

    if platform != "TIKTOK":
        return {"rows": [], "row_count": 0, "platform": platform,
                "note": "Video-grain performance is only proven for TIKTOK this phase."}

    sql = (
        "SELECT channel, video_id, business_date, account_type, "
        "creator_username, creator_nick_name, creator_author_type, title, "
        "views, sku_orders, items_sold, click_through_rate, gmv_amount, gmv_currency, "
        "value_basis, last_updated_at "
        "FROM mart.v_ai_video_daily WHERE business_date BETWEEN %s AND %s AND channel = %s"
    )
    params: tuple = (from_date, to_date, platform)
    if account_type:
        sql += " AND account_type = %s"
        params += (account_type,)
    sql += " ORDER BY gmv_amount DESC NULLS LAST LIMIT %s"
    params += (limit,)

    rows = _run("get_video_performance", sql, params,
                {"from_date": from_date, "to_date": to_date, "platform": platform,
                 "account_type": account_type, "limit": limit})

    out = [{
        "video_id": r["video_id"], "business_date": r["business_date"], "account_type": r["account_type"],
        "creator": r["creator_username"] or r["creator_nick_name"], "creator_type": r["creator_author_type"],
        "title": r["title"], "views": r["views"], "orders": r["sku_orders"], "items_sold": r["items_sold"],
        "ctr": r["click_through_rate"], "gmv": r["gmv_amount"], "gmv_currency": r["gmv_currency"],
        "status": "API_ACTUAL", "value_basis": r["value_basis"], "source": "mart.v_ai_video_daily",
    } for r in rows]
    return {
        "rows": out, "row_count": len(out), "date_range": [from_date, to_date], "platform": platform,
        "account_type_filter": account_type,
        "note": "product_impressions/product_clicks are proven NOT available at this per-video grain "
                "(confirmed absent in P7.1's real payload) and are deliberately not attached here. "
                "account_type='ALL' vs 'AFFILIATE_ACCOUNTS' are separate slices — never sum across them.",
    }


# ---------------------------------------------------------------------------
# TOOL 5 — get_live_performance
# ---------------------------------------------------------------------------

def get_live_performance(from_date: str, to_date: str, account_type: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
    _validate_range(from_date, to_date)
    account_type = _validate_account_type(account_type) or "ALL"  # default total slice
    limit = _validate_limit(limit)

    sql = (
        "SELECT channel, live_id, business_date, account_type, username, title, "
        "duration_seconds, viewers, views, sku_orders, items_sold, customers, "
        "click_through_rate, click_to_order_rate, avg_viewing_duration_secs, "
        "likes, comments, shares, gmv_amount, gmv_currency, value_basis, last_updated_at "
        "FROM mart.v_ai_live_daily "
        "WHERE business_date BETWEEN %s AND %s AND channel = 'TIKTOK' AND account_type = %s "
        "ORDER BY gmv_amount DESC NULLS LAST LIMIT %s"
    )
    params = (from_date, to_date, account_type, limit)
    rows = _run("get_live_performance", sql, params,
                {"from_date": from_date, "to_date": to_date, "account_type": account_type, "limit": limit})

    out = [{
        "live_id": r["live_id"], "business_date": r["business_date"], "account_type": r["account_type"],
        "host_username": r["username"], "title": r["title"], "duration_seconds": r["duration_seconds"],
        "viewers": r["viewers"], "views": r["views"], "orders": r["sku_orders"], "items_sold": r["items_sold"],
        "customers": r["customers"], "ctr": r["click_through_rate"], "ctor": r["click_to_order_rate"],
        "avg_viewing_duration_secs": r["avg_viewing_duration_secs"],
        "likes": r["likes"], "comments": r["comments"], "shares": r["shares"],
        "gmv": r["gmv_amount"], "gmv_currency": r["gmv_currency"],
        "status": "API_ACTUAL", "value_basis": r["value_basis"], "source": "mart.v_ai_live_daily",
    } for r in rows]
    return {
        "rows": out, "row_count": len(out), "date_range": [from_date, to_date], "account_type": account_type,
        "shopee_live_status": "NO_PERMISSION — Shopee LIVE has no data here; do not substitute AMS content metrics for it.",
        "double_count_rule": "account_type='ALL' is the authoritative total slice. 'AFFILIATE_ACCOUNTS' is a "
                              "SUBSET of the same sessions, never an addition. Never sum ALL + AFFILIATE_ACCOUNTS.",
    }


# ---------------------------------------------------------------------------
# TOOL 6 — get_affiliate_performance
# ---------------------------------------------------------------------------

def get_affiliate_performance(from_date: str, to_date: str, platform: str, limit: Optional[int] = None) -> dict:
    _validate_range(from_date, to_date)
    platform = _validate_platform(platform)
    limit = _validate_limit(limit)

    if platform != "SHOPEE":
        return {
            "rows": [], "row_count": 0, "platform": platform,
            "note": "No dedicated TikTok affiliate creator/channel view exists in the approved mart "
                    "layer. TikTok's affiliate performance is exposed at the video/LIVE content grain "
                    "instead — call get_video_performance or get_live_performance with "
                    "account_type='AFFILIATE_ACCOUNTS' for TikTok affiliate results. This is a real "
                    "data-topology fact, not an omission.",
        }

    sql = (
        "SELECT business_date, channel, affiliate_id, affiliate_name, affiliate_username, "
        "sales, items_sold, orders, clicks, est_commission, roi, total_buyers, new_buyers, "
        "value_basis, source_updated_at "
        "FROM mart.v_ai_affiliate_creator_daily "
        "WHERE business_date BETWEEN %s AND %s AND channel = %s "
        "ORDER BY sales DESC NULLS LAST LIMIT %s"
    )
    params = (from_date, to_date, platform, limit)
    rows = _run("get_affiliate_performance", sql, params,
                {"from_date": from_date, "to_date": to_date, "platform": platform, "limit": limit})

    out = [{
        "affiliate": r["affiliate_name"] or r["affiliate_username"], "business_date": r["business_date"],
        "sales_affiliate_gmv": r["sales"], "orders": r["orders"], "items_sold": r["items_sold"],
        "clicks": r["clicks"], "commission": r["est_commission"], "roi": r["roi"],
        "total_buyers": r["total_buyers"], "new_buyers": r["new_buyers"], "views": None,
        "status": "ESTIMATED_PERFORMANCE", "value_basis": r["value_basis"],
        "source": "mart.v_ai_affiliate_creator_daily",
    } for r in rows]
    return {
        "rows": out, "row_count": len(out), "date_range": [from_date, to_date], "platform": platform,
        "note": "Affiliate GMV (sales_affiliate_gmv) is kept separate from Net Sales — never add it "
                "into a Net Sales total. 'views' is not available at this grain (Shopee AMS creator "
                "performance has no view-count field); left NULL, not fabricated.",
    }


# ---------------------------------------------------------------------------
# TOOL 7 — get_data_coverage
# ---------------------------------------------------------------------------

_CROSSWALK_CACHE: Optional[list[dict]] = None


def _load_crosswalk() -> list[dict]:
    global _CROSSWALK_CACHE
    if _CROSSWALK_CACHE is None:
        with open(CROSSWALK_CSV, encoding="utf-8") as f:
            _CROSSWALK_CACHE = list(csv.DictReader(f))
    return _CROSSWALK_CACHE


def get_data_coverage(from_date: Optional[str] = None, to_date: Optional[str] = None,
                       platform: Optional[str] = None) -> dict:
    if from_date or to_date:
        if not (from_date and to_date):
            raise ValidationError("from_date and to_date must be supplied together")
        _validate_range(from_date, to_date)
    platform = _validate_platform(platform)

    crosswalk = _load_crosswalk()
    kpi_rows = [r for r in crosswalk if r["STATUS"] not in ("EMPTY_IN_SOURCE", "TARGET_THRESHOLD_NOT_A_KPI")]
    if platform:
        kpi_rows = [r for r in kpi_rows if r["PLATFORM"] in (platform, "SHOPEE, TIKTOK", "N/A")
                    or platform in r["PLATFORM"]]

    from collections import Counter
    tally = Counter(r["STATUS"] for r in kpi_rows)

    live_metric_rows = []
    if from_date and to_date:
        sql = "SELECT metric_name, channel, business_date, availability_status, value_basis, note FROM mart.v_ai_metric_status WHERE business_date BETWEEN %s AND %s"
        params: tuple = (from_date, to_date)
        if platform:
            sql += " AND channel = %s"
            params += (platform,)
        sql += " LIMIT 500"
        live_metric_rows = _run("get_data_coverage", sql, params,
                                 {"from_date": from_date, "to_date": to_date, "platform": platform})

    return {
        "kpi_universe_total": len(kpi_rows),
        "kpi_status_tally": dict(tally),
        "kpi_rows": [
            {
                "hh_kpi": r["HH_KPI"], "platform": r["PLATFORM"], "section": r["SECTION"],
                "status": r["STATUS"], "core_location": r["CORE_LOCATION"], "mart_location": r["MART_LOCATION"],
                "gap_action": r["GAP_ACTION"],
            } for r in kpi_rows
        ],
        "live_metric_status_sample": live_metric_rows,
        "source": "artifacts/v0/P7_1_PLATFORM_KPI_CROSSWALK.csv (locked baseline) "
                   "+ mart.v_ai_metric_status (live, date-scoped detail when a range is given)",
        "warning_template": "CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH — see kpi_status_tally and "
                             "any NO_PERMISSION/SEPARATE_API_REQUIRED/NOT_EXPOSED_PUBLIC_API/MISSING_SOURCE rows above.",
    }
