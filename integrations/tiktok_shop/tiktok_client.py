"""
TikTok Shop Open API client — signing, token exchange/refresh, a generic
signed request helper, and a hard-coded read-only allowlist.

Security rules (do not weaken these):
- App Secret is NEVER written to disk by this module. Callers get it from
  the macOS Keychain (see keychain.py) or a hidden prompt, and hold it only
  in memory for the duration of the process.
- access_token / refresh_token are the only credentials persisted locally,
  in tokens.json with restrictive (0600) permissions, out of Git.
- Every signed request path+method is checked against ALLOWLIST before any
  network call is made. Anything not on the allowlist raises SecurityError
  immediately — no write/update/delete endpoint can be reached through this
  client even by a future coding mistake.
- Nothing in this module ever logs/prints app_secret, auth_code,
  access_token, or refresh_token. `_auth_request` never lets the underlying
  HTTP exception (whose message embeds the full request URL, including
  app_secret/auth_code/refresh_token as query params) propagate.

Endpoint source: every path in READ_ENDPOINTS and AUTHORIZED_SHOPS_PATH
comes from the TikTok Shop Partner Center official API reference, as
re-verified against the current docs
(source="TikTok Shop Partner Center official docs", verified=True).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

try:
    from zoneinfo import ZoneInfo
    _VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover - zoneinfo always available on py3.9+
    _VN_TZ = None

AUTH_HOST = "https://auth.tiktok-shops.com"
API_HOST = "https://open-api.tiktokglobalshop.com"

TOKEN_GET_PATH = "/api/v2/token/get"
TOKEN_REFRESH_PATH = "/api/v2/token/refresh"
AUTHORIZED_SHOPS_PATH = "/authorization/202309/shops"

# grant_type constants — TikTok Shop uses "authorized_code", NOT the more
# common OAuth2 spelling "authorization_code". Do not "fix" this typo.
GRANT_TYPE_AUTH_CODE = "authorized_code"
GRANT_TYPE_REFRESH = "refresh_token"

OFFICIAL_DOCS_SOURCE = "TikTok Shop Partner Center official docs"

# Step 8 read-only connectivity-test endpoints — official paths only.
# `path` may contain a `{date}` placeholder (analytics), filled in by
# read_domain() at call time.
READ_ENDPOINTS = {
    "orders": {
        "method": "POST",
        "path": "/order/202309/orders/search",
        "scope": "seller.order.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "finance": {
        "method": "GET",
        "path": "/finance/202309/statements",
        "scope": "seller.finance.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "analytics": {
        "method": "GET",
        "path": "/analytics/202510/shop/performance/{date}/performance_per_hour",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "product": {
        "method": "POST",
        "path": "/product/202502/products/search",
        "scope": "seller.product.basic",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "return_refund": {
        "method": "POST",
        "path": "/return_refund/202602/returns/search",
        "scope": "seller.return_refund.basic",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "affiliate": {
        "method": "POST",
        "path": "/affiliate_seller/202410/orders/search",
        "scope": "seller.affiliate_collaboration.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "bestsellers": {
        "method": "GET",
        "path": "/analytics/202511/products/bestselling",
        "scope": "data.bestselling.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    # ------------------------------------------------------------------
    # Added for the FIRST END-TO-END REPORTING PILOT (pilot_reporting/).
    # Every path/version/scope below was re-verified live against the
    # current TikTok Shop Partner Center API Reference (docv2) in this
    # session — not assumed, not carried over from stale notes. Two of
    # these (statement_transactions, order statement_transactions) are on
    # v202501 because TikTok retired v202309 for those two endpoints on
    # 2025-07-01. Payments is on v202605 because 202309 is sunset
    # 2026-08-15 (already past 'today' 2026-09-08) and 202605 is the only
    # remaining reserve_amount-stripped version. All GET, all read-only.
    # ------------------------------------------------------------------
    "order_detail": {
        "method": "GET",
        "path": "/order/202309/orders",
        "scope": "seller.order.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "finance_statement_transactions": {
        "method": "GET",
        "path": "/finance/202501/statements/{statement_id}/statement_transactions",
        "scope": "seller.finance.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "finance_order_statement_transactions": {
        "method": "GET",
        "path": "/finance/202501/orders/{order_id}/statement_transactions",
        "scope": "seller.finance.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "finance_unsettled": {
        "method": "GET",
        "path": "/finance/202507/orders/unsettled",
        "scope": "seller.finance.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "finance_payments": {
        "method": "GET",
        "path": "/finance/202605/payments",
        "scope": "seller.finance.info",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "product_analytics": {
        "method": "GET",
        "path": "/analytics/202605/shop_products/performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "video_analytics": {
        "method": "GET",
        "path": "/analytics/202605/shop_videos/performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "live_analytics": {
        "method": "GET",
        "path": "/analytics/202509/shop_lives/performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    # ------------------------------------------------------------------
    # Added for P7.1 (Platform KPI API Coverage). All GET, all read-only,
    # all under the already-granted data.shop_analytics.public.read scope.
    # ------------------------------------------------------------------
    "video_overview_analytics": {
        "method": "GET",
        "path": "/analytics/202509/shop_videos/overview_performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "live_overview_analytics": {
        "method": "GET",
        "path": "/analytics/202509/shop_lives/overview_performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "live_performance_per_minute": {
        "method": "GET",
        "path": "/analytics/202510/shop_lives/{live_id}/performance_per_minutes",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
    "live_products_performance": {
        "method": "GET",
        "path": "/analytics/202512/shop/{live_id}/products_performance",
        "scope": "data.shop_analytics.public.read",
        "source": OFFICIAL_DOCS_SOURCE,
        "verified": True,
    },
}

# ---------------------------------------------------------------------------
# PHASE 4 — hard-coded READ-ONLY allowlist. Anything not listed here is
# refused in code, before a network request is ever built. This does not
# rely on scopes, on the caller's intent, or on prompts — it is enforced by
# _enforce_allowlist() inside _signed_request().
# ---------------------------------------------------------------------------
_EXACT_ALLOWLIST: set[tuple[str, str]] = {
    ("GET", AUTHORIZED_SHOPS_PATH),
    ("POST", "/order/202309/orders/search"),
    ("GET", "/finance/202309/statements"),
    ("POST", "/product/202502/products/search"),
    ("POST", "/return_refund/202602/returns/search"),
    ("POST", "/affiliate_seller/202410/orders/search"),
    ("GET", "/analytics/202511/products/bestselling"),
    # pilot_reporting/ additions (all GET, all read-only, all re-verified
    # live against docv2 in this session — see READ_ENDPOINTS comment).
    ("GET", "/order/202309/orders"),
    ("GET", "/finance/202507/orders/unsettled"),
    ("GET", "/finance/202605/payments"),
    ("GET", "/analytics/202605/shop_products/performance"),
    ("GET", "/analytics/202605/shop_videos/performance"),
    ("GET", "/analytics/202509/shop_lives/performance"),
    # P7.1 additions — all GET, all read-only analytics.
    ("GET", "/analytics/202509/shop_videos/overview_performance"),
    ("GET", "/analytics/202509/shop_lives/overview_performance"),
}
# The analytics performance-per-hour path has a variable {date} segment;
# statement/order statement_transactions have a variable id segment;
# P7.1's live-id-scoped analytics paths have a variable {live_id} segment.
_PATTERN_ALLOWLIST: list[tuple[str, re.Pattern]] = [
    ("GET", re.compile(r"^/analytics/202510/shop/performance/[^/]+/performance_per_hour$")),
    ("GET", re.compile(r"^/finance/202501/statements/[^/]+/statement_transactions$")),
    ("GET", re.compile(r"^/finance/202501/orders/[^/]+/statement_transactions$")),
    ("GET", re.compile(r"^/analytics/202510/shop_lives/[^/]+/performance_per_minutes$")),
    ("GET", re.compile(r"^/analytics/202512/shop/[^/]+/products_performance$")),
]

# Explicitly called out as forbidden by design (illustrative — the allowlist
# above already blocks all of these and everything else by default):
#   seller.delivery.status.write, Create/Edit Product, Update Price,
#   Update Inventory, Cancel Order, Approve/Reject Return, Refund,
#   Fulfillment write, Shipping write, Customer Service send/reply,
#   Campaign/Promotion write, and any DELETE/PUT/PATCH whatsoever.


class SecurityError(RuntimeError):
    """Raised when code attempts to call an endpoint outside the read-only
    allowlist. This must never be caught-and-ignored anywhere."""


def _enforce_allowlist(method: str, path: str) -> None:
    if (method, path) in _EXACT_ALLOWLIST:
        return
    for m, pattern in _PATTERN_ALLOWLIST:
        if method == m and pattern.match(path):
            return
    raise SecurityError(
        f"Refusing to call {method} {path}: not on the read-only allowlist. "
        "No network request was sent."
    )


class TikTokAPIError(RuntimeError):
    """Used for token exchange/refresh failures only (those flows are
    treated as a single PASS/FAIL, unlike the Step 8 read calls)."""

    def __init__(self, code: Any, message: str, request_id: Optional[str] = None):
        super().__init__(f"TikTok API error {code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id


class NetworkError(RuntimeError):
    """Connection/timeout/invalid-JSON errors for signed API calls."""


@dataclass
class APIResponse:
    http_status: int
    code: Any
    message: str
    request_id: Optional[str]
    data: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.code in (0, "0")


def mask(value: Optional[str], keep_start: int = 4, keep_end: int = 4) -> str:
    """Return a safe-to-print masked form of a secret-ish string."""
    if not value:
        return "(empty)"
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    return f"{value[:keep_start]}{'*' * (len(value) - keep_start - keep_end)}{value[-keep_end:]}"


def sign_request(
    path: str,
    params: dict[str, str],
    app_secret: str,
    body_str: Optional[str] = None,
) -> str:
    """
    TikTok Shop request signing.

    Steps:
      1. Drop 'sign' and 'access_token' from the params to sign.
      2. Sort remaining keys ascending (ASCII order).
      3. Build: path + key1 + value1 + key2 + value2 + ...
      4. If Content-Type is not multipart/form-data and a body is present,
         append the EXACT body string that will be sent on the wire (see
         `_signed_request` — the same bytes are never re-serialized between
         signing and sending).
      5. Wrap: app_secret + <string from steps 3-4> + app_secret
      6. HMAC-SHA256 over that string, keyed with app_secret, lowercase hex
         digest.

    access_token is never part of the signed string — it travels only in
    the `x-tts-access-token` header.
    """
    to_sign = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    ordered_keys = sorted(to_sign.keys())
    base = path + "".join(f"{k}{to_sign[k]}" for k in ordered_keys)
    if body_str:
        base += body_str
    wrapped = app_secret + base + app_secret
    digest = hmac.new(app_secret.encode("utf-8"), wrapped.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


@dataclass
class TikTokShopClient:
    app_key: str
    app_secret: str = field(repr=False)  # never logged, never persisted by this class
    timeout: int = 15
    auth_timeout: int = 30  # Phase 7 spec: 30s for token/get & token/refresh

    # ---- Token endpoints (unsigned; app_secret sent as a param per TikTok docs) ----

    def exchange_token(self, auth_code: str) -> dict[str, Any]:
        params = {
            "app_key": self.app_key,
            "app_secret": self.app_secret,
            "auth_code": auth_code,
            "grant_type": GRANT_TYPE_AUTH_CODE,
        }
        return self._auth_request(TOKEN_GET_PATH, params)

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        params = {
            "app_key": self.app_key,
            "app_secret": self.app_secret,
            "refresh_token": refresh_token,
            "grant_type": GRANT_TYPE_REFRESH,
        }
        return self._auth_request(TOKEN_REFRESH_PATH, params)

    def _auth_request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = AUTH_HOST + path
        try:
            resp = requests.get(url, params=params, timeout=self.auth_timeout)
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException:
            # IMPORTANT: do not let this propagate. The underlying exception's
            # message/repr embeds the full prepared URL for this call,
            # including app_secret/auth_code/refresh_token as query
            # parameters. Re-raise a sanitized error instead, with no
            # exception chaining (`from None`) so nothing upstream can print
            # a traceback that includes the original, credential-bearing
            # exception. We also never log this prepared URL anywhere else.
            raise TikTokAPIError(
                "HTTP_ERROR",
                "Network/HTTP error calling the token endpoint "
                "(details withheld — the underlying error embeds the "
                "request URL, which contains app_secret/auth_code/"
                "refresh_token).",
            ) from None
        except ValueError:
            raise TikTokAPIError(
                "BAD_RESPONSE", "Token endpoint did not return valid JSON."
            ) from None

        if payload.get("code") not in (0, "0"):
            raise TikTokAPIError(
                payload.get("code"),
                payload.get("message", "unknown error"),
                payload.get("request_id"),
            )
        return payload["data"]

    # ---- Signed, allowlisted, read-only API requests ----

    def _signed_request(
        self,
        method: str,
        path: str,
        access_token: str,
        query_params: Optional[dict[str, str]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> APIResponse:
        _enforce_allowlist(method, path)  # raises SecurityError, no request sent

        params = dict(query_params or {})
        params["app_key"] = self.app_key
        params["timestamp"] = str(int(time.time()))  # 10-digit unix seconds

        # Serialize the body EXACTLY ONCE. The same bytes are used both to
        # compute the signature and as the literal request payload — never
        # re-serialize between signing and sending (two json.dumps() calls
        # are not guaranteed to produce identical bytes).
        body_bytes: Optional[bytes] = None
        if method == "POST":
            body_bytes = json.dumps(
                body if body is not None else {},
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")

        body_str_for_signing = body_bytes.decode("utf-8") if body_bytes is not None else None
        params["sign"] = sign_request(path, params, self.app_secret, body_str_for_signing)

        url = API_HOST + path
        headers = {
            "Content-Type": "application/json",
            "x-tts-access-token": access_token,
        }
        query_string = urllib.parse.urlencode(params)
        full_url = f"{url}?{query_string}"

        try:
            if method == "GET":
                resp = requests.get(full_url, headers=headers, timeout=self.timeout)
            elif method == "POST":
                resp = requests.post(
                    full_url, headers=headers, data=body_bytes, timeout=self.timeout
                )
            else:
                raise SecurityError(f"Method {method} is not read-only; refusing.")
            http_status = resp.status_code
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"{method} {path}: network/timeout error: {e.__class__.__name__}") from None
        except ValueError:
            raise NetworkError(f"{method} {path}: response was not valid JSON") from None

        return APIResponse(
            http_status=http_status,
            code=payload.get("code"),
            message=payload.get("message", ""),
            request_id=payload.get("request_id"),
            data=payload.get("data") or {},
        )

    def get_authorized_shops(self, access_token: str) -> APIResponse:
        return self._signed_request("GET", AUTHORIZED_SHOPS_PATH, access_token)

    def read_domain(
        self,
        domain: str,
        access_token: str,
        shop_cipher: Optional[str] = None,
        extra_query: Optional[dict[str, str]] = None,
        body: Optional[dict[str, Any]] = None,
        path_params: Optional[dict[str, str]] = None,
    ) -> APIResponse:
        """Call one of READ_ENDPOINTS by name. Read-only and allowlisted."""
        if domain not in READ_ENDPOINTS:
            raise ValueError(f"Unknown read domain: {domain}")
        cfg = READ_ENDPOINTS[domain]
        path = cfg["path"].format(**(path_params or {}))
        query = dict(extra_query or {})
        if shop_cipher:
            query["shop_cipher"] = shop_cipher
        return self._signed_request(cfg["method"], path, access_token, query, body)


def yesterday_vn_date() -> str:
    """YYYY-MM-DD for 'yesterday' in Asia/Ho_Chi_Minh, per the analytics
    endpoint's expected `{date}` path param (Phase 8 spec)."""
    now = datetime.now(_VN_TZ) if _VN_TZ else datetime.utcnow()
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def n_days_ago_vn_date(n: int) -> str:
    """YYYY-MM-DD for 'n days ago' in Asia/Ho_Chi_Minh. Some analytics
    endpoints (e.g. /analytics/202511/products/bestselling, observed
    2026-09-08) reject T-1 with code 28001022 ("date must be on or before"
    an earlier date) — they run on T-2 latency, not T-1."""
    now = datetime.now(_VN_TZ) if _VN_TZ else datetime.utcnow()
    return (now - timedelta(days=n)).strftime("%Y-%m-%d")
