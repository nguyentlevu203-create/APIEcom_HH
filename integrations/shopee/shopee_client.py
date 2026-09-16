"""
Minimal Shopee Open Platform V2 read-only client.

Shop-level signature (used once access_token exists):
    base_string = partner_id + path + timestamp + access_token + shop_id
    sign = HMAC-SHA256(key=partner_key, msg=base_string).hexdigest()

Every call here is a GET against a read endpoint. No write endpoint is
implemented in this module on purpose — there is nothing here that could
mutate shop/product/order state even by mistake.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import API_HOST, LIVE_PARTNER_ID
from keychain import (
    ACCOUNT_ACCESS_TOKEN,
    ACCOUNT_ACCESS_TOKEN_EXPIRE_AT,
    ACCOUNT_LIVE_PARTNER_KEY,
    ACCOUNT_SHOP_ID,
    get_secret,
)

REFRESH_BUFFER_SECONDS = 600  # refresh once fewer than 10 minutes of validity remain


def _refresh_if_needed() -> None:
    """P8.4 Section H — proactively refresh the access_token before it
    expires, using the existing refresh_token (auth/refresh_token.py).
    Root cause of the prior mid-session expiry: unlike TikTok's
    bootstrap_session() (access_token_needs_refresh + do_refresh, called
    before every domain handler), Shopee's client never checked token
    age at all — it read whatever was in Keychain and used it until a
    live API call failed with invalid_acceess_token. This closes that
    gap the same way TikTok already works: a pre-flight expiry check
    before every ShopeeClient use, not a reactive retry after failure."""
    expire_at = get_secret(ACCOUNT_ACCESS_TOKEN_EXPIRE_AT)
    if expire_at:
        try:
            if int(expire_at) - time.time() > REFRESH_BUFFER_SECONDS:
                return
        except ValueError:
            pass  # malformed/missing expiry — refresh to be safe
    auth_dir = Path(__file__).resolve().parent / "auth"
    if str(auth_dir) not in sys.path:
        sys.path.insert(0, str(auth_dir))
    from refresh_token import main as _do_refresh  # noqa: E402
    if _do_refresh() != 0:
        raise RuntimeError("Shopee access_token pre-flight refresh failed — see refresh_token.py output above.")


class ShopeeClient:
    def __init__(self) -> None:
        _refresh_if_needed()
        self.partner_id = LIVE_PARTNER_ID
        shop_id = get_secret(ACCOUNT_SHOP_ID)
        if not shop_id:
            raise RuntimeError("shop_id not found in Keychain — run token exchange first.")
        self.shop_id = int(shop_id)

    def _sign(self, path: str, timestamp: int, access_token: str) -> str:
        partner_key = get_secret(ACCOUNT_LIVE_PARTNER_KEY)
        if not partner_key:
            raise RuntimeError("Live Partner Key not found in Keychain.")
        base_string = f"{self.partner_id}{path}{timestamp}{access_token}{self.shop_id}"
        s = hmac.new(
            partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        del partner_key
        return s

    def get(self, path: str, extra_params: Optional[dict[str, Any]] = None) -> dict:
        """GET only. Returns dict with _http_status merged in. Never raises
        on a Shopee-level error — caller inspects the 'error' field."""
        access_token = get_secret(ACCOUNT_ACCESS_TOKEN)
        if not access_token:
            raise RuntimeError("access_token not found in Keychain.")

        timestamp = int(time.time())
        s = self._sign(path, timestamp, access_token)

        params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": s,
            "shop_id": self.shop_id,
            "access_token": access_token,
        }
        del access_token
        if extra_params:
            params.update(extra_params)

        resp = requests.get(f"{API_HOST}{path}", params=params, timeout=20)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": "non_json_response", "message": resp.text[:200]}
        data["_http_status"] = resp.status_code
        return data


def status_from_response(data: dict, empty_check: Optional[Any] = None) -> str:
    """Classify a response into PASS / PASS_EMPTY / NO_PERMISSION / FAIL."""
    error = str(data.get("error", ""))
    if not error:
        if empty_check is not None and not empty_check:
            return "PASS_EMPTY"
        return "PASS"
    low = error.lower()
    if "permission" in low or "not_authorize" in low or "no_permission" in low or "forbidden" in low:
        return "NO_PERMISSION"
    if "param" in low or "invalid" in low:
        return "FAIL_REQUEST_SCHEMA"
    if "auth" in low or "token" in low:
        return "FAIL_AUTH"
    return "FAIL"
