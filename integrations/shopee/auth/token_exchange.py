#!/usr/bin/env python3
"""
Shopee Open Platform V2 — authorization code -> access_token/refresh_token.

POST {API_HOST}/api/v2/auth/token/get

Public/auth signature (no access_token yet):
    base_string = partner_id + path + timestamp
    sign = HMAC-SHA256(key=partner_key, msg=base_string).hexdigest()

The authorization code is single-use and time-limited. It is taken as a
CLI argument, used once in memory to build the request, and never written
to any file or log. Only the words FOUND / NOT FOUND for token presence
are ever printed — never the actual code, key, or token values.

Usage:
    python3 token_exchange.py --code <code> --shop-id <shop_id>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import API_HOST, LIVE_PARTNER_ID, TOKEN_GET_PATH  # noqa: E402
from keychain import (  # noqa: E402
    ACCOUNT_ACCESS_TOKEN,
    ACCOUNT_ACCESS_TOKEN_EXPIRE_AT,
    ACCOUNT_LIVE_PARTNER_KEY,
    ACCOUNT_PARTNER_ID,
    ACCOUNT_REFRESH_TOKEN,
    ACCOUNT_REFRESH_TOKEN_EXPIRE_AT,
    ACCOUNT_SHOP_ID,
    get_secret,
    has_secret,
    set_secret,
)

REAUTH_ERROR_MARKERS = (
    "invalid code",
    "invalid_code",
    "expired code",
    "code already used",
    "authorization expired",
    "expired or used or invalid",
    "error_auth",
)


def sign(partner_id: int, path: str, timestamp: int, partner_key: str) -> str:
    base_string = f"{partner_id}{path}{timestamp}"
    return hmac.new(
        partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def exchange(code: str, shop_id: int) -> dict:
    partner_key = get_secret(ACCOUNT_LIVE_PARTNER_KEY)
    if not partner_key:
        raise RuntimeError(
            "Live Partner Key not found in Keychain. Run setup_partner_key.py "
            "yourself first (see that file's docstring)."
        )

    timestamp = int(time.time())
    partner_id = LIVE_PARTNER_ID
    s = sign(partner_id, TOKEN_GET_PATH, timestamp, partner_key)
    del partner_key  # drop local reference as soon as it's no longer needed

    url = f"{API_HOST}{TOKEN_GET_PATH}"
    params = {"partner_id": partner_id, "timestamp": timestamp, "sign": s}
    body = {"code": code, "shop_id": shop_id, "partner_id": partner_id}

    resp = requests.post(url, params=params, json=body, timeout=15)
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Non-JSON response, HTTP {resp.status_code}")
    data["_http_status"] = resp.status_code
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--shop-id", required=True, type=int)
    args = ap.parse_args()

    try:
        data = exchange(args.code, args.shop_id)
    except Exception as exc:  # noqa: BLE001
        print(f"TOKEN EXCHANGE = FAIL (request error: {type(exc).__name__})")
        return 1

    http_status = data.get("_http_status")
    error = data.get("error", "")
    message_raw = str(data.get("message", ""))
    message = message_raw.lower()

    if error or http_status != 200:
        combined = f"{error} {message}".lower()
        if any(marker in combined for marker in REAUTH_ERROR_MARKERS):
            print("SHOPEE REAUTHORIZATION REQUIRED")
        else:
            print(f"TOKEN EXCHANGE = FAIL (http={http_status} error={error!r} message={message_raw!r})")
        return 1

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expire_in = data.get("expire_in")
    resp_shop_id = data.get("shop_id")
    resp_partner_id = data.get("partner_id")
    request_id = data.get("request_id")

    if not access_token or not refresh_token:
        print("TOKEN EXCHANGE = FAIL (access_token/refresh_token missing from response)")
        return 1

    now = int(time.time())
    access_expire_at = now + int(expire_in) if expire_in else 0
    # Shopee refresh tokens are valid 30 days from issuance.
    refresh_expire_at = now + 30 * 24 * 60 * 60

    set_secret(ACCOUNT_ACCESS_TOKEN, access_token)
    set_secret(ACCOUNT_REFRESH_TOKEN, refresh_token)
    set_secret(ACCOUNT_SHOP_ID, str(resp_shop_id or shop_id_fallback(args.shop_id)))
    set_secret(ACCOUNT_PARTNER_ID, str(resp_partner_id or LIVE_PARTNER_ID))
    set_secret(ACCOUNT_ACCESS_TOKEN_EXPIRE_AT, str(access_expire_at))
    set_secret(ACCOUNT_REFRESH_TOKEN_EXPIRE_AT, str(refresh_expire_at))

    del access_token, refresh_token  # drop local references

    print("TOKEN EXCHANGE = PASS")
    print(f"Access Token: {'FOUND' if has_secret(ACCOUNT_ACCESS_TOKEN) else 'NOT FOUND'}")
    print(f"Refresh Token: {'FOUND' if has_secret(ACCOUNT_REFRESH_TOKEN) else 'NOT FOUND'}")
    print(f"Request ID: {'FOUND' if request_id else 'NOT FOUND'}")
    print(f"Access Token Expiry (epoch): {access_expire_at}")
    print(f"Refresh Token Expiry (epoch, assumed 30d): {refresh_expire_at}")
    return 0


def shop_id_fallback(shop_id: int) -> int:
    return shop_id


if __name__ == "__main__":
    sys.exit(main())
