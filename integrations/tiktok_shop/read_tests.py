"""
Phase 8 + 9: minimal read-only requests per domain, and TikTok response
classification into a fixed, closed set of statuses.

Every request body here is intentionally minimal (small page_size, a short
recent date window) — this is a connectivity/permission smoke test, not a
full ETL pull. Exact official request-schema field names could not be
independently re-verified against live docs in this environment (the
Partner Center docs site is a client-rendered app with no public read API
we could reach); a wrong or incomplete field name will surface honestly as
FAIL_REQUEST_SCHEMA rather than being silently misreported as FAIL_API or
FAIL_AUTH.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from tiktok_client import READ_ENDPOINTS, NetworkError, TikTokShopClient, n_days_ago_vn_date, yesterday_vn_date

# The original Step-8 connectivity-test domains. READ_ENDPOINTS has grown
# since (pilot_reporting/ added order_detail, finance_statement_transactions,
# etc.) but this smoke test intentionally stays scoped to the 7 it always
# covered — _default_request() below only has payloads for these.
SMOKE_TEST_DOMAINS = (
    "orders",
    "finance",
    "analytics",
    "product",
    "return_refund",
    "affiliate",
    "bestsellers",
)

STATUS_VALUES = {
    "PASS",
    "PASS_EMPTY",
    "SKIPPED_SCOPE_NOT_GRANTED",
    "SKIPPED_MARKET_NOT_SUPPORTED",
    "FAIL_AUTH",
    "FAIL_SIGNATURE",
    "FAIL_REQUEST_SCHEMA",
    "FAIL_API",
    "FAIL_NETWORK",
}

_SENSITIVE_KEYS = {"access_token", "refresh_token", "app_secret", "auth_code", "token", "sign"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _classify_message(message: str) -> str:
    m = (message or "").lower()
    if "sign" in m:
        return "FAIL_SIGNATURE"
    if "token" in m or "unauthorized" in m or "access denied" in m:
        return "FAIL_AUTH"
    if "market" in m or "not support" in m or "region" in m:
        return "SKIPPED_MARKET_NOT_SUPPORTED"
    if "param" in m or "required" in m or "invalid" in m or "schema" in m or "field" in m:
        return "FAIL_REQUEST_SCHEMA"
    return "FAIL_API"


def _rows_count(data: dict[str, Any]) -> int:
    for v in data.values():
        if isinstance(v, list):
            return len(v)
    return 0


def _default_request(domain: str) -> dict[str, Any]:
    now = int(time.time())
    one_week = 7 * 24 * 3600
    return {
        # page_size is a QUERY parameter for orders/product/affiliate — confirmed
        # by TikTok's own error (code 36009004: "PageSize is a required field
        # and has not been provided.") when it was only in the body.
        "orders": {
            "query": {"page_size": "1"},
            "body": {"create_time_ge": now - one_week, "create_time_lt": now},
            "path_params": {},
        },
        # finance: TikTok error (36009004) said "SortField is a required
        # field and has not been provided." — added sort_field/sort_order.
        "finance": {
            "query": {"page_size": "1", "sort_field": "statement_time", "sort_order": "DESC"},
            "body": None,
            "path_params": {},
        },
        "analytics": {
            "query": {"currency": "LOCAL"},
            "body": None,
            "path_params": {"date": yesterday_vn_date()},
        },
        "product": {"query": {"page_size": "1"}, "body": {"status": "ALL"}, "path_params": {}},
        # return_refund is already PASS with page_size in the body — do not
        # touch it (surgical-fix instruction).
        "return_refund": {
            "query": {},
            "body": {"page_size": 1, "create_time_ge": now - one_week, "create_time_lt": now},
            "path_params": {},
        },
        "affiliate": {
            "query": {"page_size": "1"},
            "body": {},
            "path_params": {},
        },
        # bestsellers: TikTok error (36009004) said "Date is a required
        # field and has not been provided." — added date/time_slot/currency.
        # Observed 2026-09-08: this endpoint runs T-2, not T-1 (code
        # 28001022, "date must be on or before" T-2) — confirmed by direct
        # call. yesterday_vn_date() (T-1) is rejected; use T-2.
        "bestsellers": {
            "query": {
                "page_size": "1",
                "date": n_days_ago_vn_date(2),
                "time_slot": "1D",
                "currency": "LOCAL",
            },
            "body": None,
            "path_params": {},
        },
    }[domain]


def run_domain_test(
    client: TikTokShopClient,
    domain: str,
    access_token: str,
    shop_cipher: Optional[str],
    granted_scopes: set[str],
) -> dict[str, Any]:
    """
    Runs one Step-8 connectivity test. Never raises for TikTok-level or
    network-level failures (those are reported via `status`); a
    SecurityError (allowlist violation) DOES propagate, because that
    indicates a code bug that must stop the whole run.
    """
    cfg = READ_ENDPOINTS[domain]
    result: dict[str, Any] = {
        "domain": domain,
        "method": cfg["method"],
        "path": cfg["path"],
        "scope": cfg["scope"],
        "http_status": None,
        "tiktok_code": None,
        "message": "",
        "request_id": None,
        "rows": None,
        "status": None,
        "data": {},
    }

    if cfg["scope"] not in granted_scopes:
        result["status"] = "SKIPPED_SCOPE_NOT_GRANTED"
        result["message"] = f"scope '{cfg['scope']}' not in granted_scopes"
        return result

    req = _default_request(domain)
    try:
        resp = client.read_domain(
            domain,
            access_token,
            shop_cipher=shop_cipher,
            extra_query=req["query"],
            body=req["body"],
            path_params=req["path_params"],
        )
    except NetworkError as e:
        result["status"] = "FAIL_NETWORK"
        result["message"] = str(e)
        return result

    result["http_status"] = resp.http_status
    result["tiktok_code"] = resp.code
    result["message"] = resp.message
    result["request_id"] = resp.request_id

    if resp.ok:
        rows = _rows_count(resp.data)
        result["rows"] = rows
        result["status"] = "PASS" if rows > 0 else "PASS_EMPTY"
        result["data"] = _redact(resp.data)
    else:
        result["status"] = _classify_message(resp.message)

    return result


def run_all_domain_tests(
    client: TikTokShopClient,
    access_token: str,
    shop_cipher: Optional[str],
    granted_scopes: set[str],
) -> list[dict[str, Any]]:
    return [
        run_domain_test(client, domain, access_token, shop_cipher, granted_scopes)
        for domain in SMOKE_TEST_DOMAINS
    ]
