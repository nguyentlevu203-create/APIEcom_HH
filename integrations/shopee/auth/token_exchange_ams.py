#!/usr/bin/env python3
"""
Shopee Open Platform V2 — AMS ("Affiliate for order") app: authorization
code -> access_token/refresh_token.

This is a SEPARATE app from the main production "Report Control Towner"
app (Live Partner_id 2044177) used by every other script in this
project. This script:
  - uses AMS_PARTNER_ID (2044772), never LIVE_PARTNER_ID
  - reads the key from Keychain account AMS_LIVE_PARTNER_KEY (set via
    setup_partner_key_ams.py), never LIVE_PARTNER_KEY
  - stores the resulting tokens under AMS_ACCESS_TOKEN / AMS_REFRESH_TOKEN
    / AMS_SHOP_ID / AMS_PARTNER_ID_STORED, never touching the existing
    ACCESS_TOKEN / REFRESH_TOKEN / SHOP_ID / PARTNER_ID accounts that the
    production pipeline depends on.

POST {API_HOST}/api/v2/auth/token/get

The authorization code is single-use and time-limited (Shopee: a few
minutes). It is taken as a CLI argument, used once in memory, and never
written to any file or log. Only FOUND / NOT FOUND for token presence is
ever printed — never the actual code, key, or token values.

Usage:
    python3 token_exchange_ams.py --code <code> --shop-id <shop_id>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time
from pathlib import Path

import keyring
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import API_HOST, KEYCHAIN_SERVICE, TOKEN_GET_PATH  # noqa: E402

AMS_PARTNER_ID = 2044772  # "Affiliate for order" app, Live Partner_id (open.shopee.com/console/app/241801)

ACCOUNT_AMS_LIVE_PARTNER_KEY = "AMS_LIVE_PARTNER_KEY"
ACCOUNT_AMS_ACCESS_TOKEN = "AMS_ACCESS_TOKEN"
ACCOUNT_AMS_REFRESH_TOKEN = "AMS_REFRESH_TOKEN"
ACCOUNT_AMS_SHOP_ID = "AMS_SHOP_ID"
ACCOUNT_AMS_PARTNER_ID_STORED = "AMS_PARTNER_ID_STORED"
ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT = "AMS_ACCESS_TOKEN_EXPIRE_AT"
ACCOUNT_AMS_REFRESH_TOKEN_EXPIRE_AT = "AMS_REFRESH_TOKEN_EXPIRE_AT"

REAUTH_ERROR_MARKERS = (
    "invalid code",
    "invalid_code",
    "expired code",
    "code already used",
    "authorization expired",
    "expired or used or invalid",
    "error_auth",
)


def get_secret(account: str):
    return keyring.get_password(KEYCHAIN_SERVICE, account)


def set_secret(account: str, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, account, value)


def has_secret(account: str) -> bool:
    return bool(get_secret(account))


def sign(partner_id: int, path: str, timestamp: int, partner_key: str) -> str:
    base_string = f"{partner_id}{path}{timestamp}"
    return hmac.new(partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).hexdigest()


def exchange(code: str, shop_id: int) -> dict:
    partner_key = get_secret(ACCOUNT_AMS_LIVE_PARTNER_KEY)
    if not partner_key:
        raise RuntimeError(
            "AMS Live Partner Key not found in Keychain. Run setup_partner_key_ams.py "
            "yourself first (see that file's docstring)."
        )

    timestamp = int(time.time())
    s = sign(AMS_PARTNER_ID, TOKEN_GET_PATH, timestamp, partner_key)
    del partner_key

    url = f"{API_HOST}{TOKEN_GET_PATH}"
    params = {"partner_id": AMS_PARTNER_ID, "timestamp": timestamp, "sign": s}
    body = {"code": code, "shop_id": shop_id, "partner_id": AMS_PARTNER_ID}

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
        print(f"TOKEN_EXCHANGE_STATUS = FAIL (request error: {type(exc).__name__})")
        return 1

    http_status = data.get("_http_status")
    error = data.get("error", "")
    message_raw = str(data.get("message", ""))
    message = message_raw.lower()

    if error or http_status != 200:
        combined = f"{error} {message}".lower()
        if any(marker in combined for marker in REAUTH_ERROR_MARKERS):
            print("SHOPEE REAUTHORIZATION REQUIRED (code expired/used/invalid)")
        else:
            print(f"TOKEN_EXCHANGE_STATUS = FAIL (http={http_status} error={error!r} message={message_raw!r})")
        return 1

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expire_in = data.get("expire_in")
    resp_shop_id = data.get("shop_id")
    resp_partner_id = data.get("partner_id")
    request_id = data.get("request_id")

    if not access_token or not refresh_token:
        print("TOKEN_EXCHANGE_STATUS = FAIL (access_token/refresh_token missing from response)")
        return 1

    now = int(time.time())
    access_expire_at = now + int(expire_in) if expire_in else 0
    refresh_expire_at = now + 30 * 24 * 60 * 60

    set_secret(ACCOUNT_AMS_ACCESS_TOKEN, access_token)
    set_secret(ACCOUNT_AMS_REFRESH_TOKEN, refresh_token)
    set_secret(ACCOUNT_AMS_SHOP_ID, str(resp_shop_id or args.shop_id))
    set_secret(ACCOUNT_AMS_PARTNER_ID_STORED, str(resp_partner_id or AMS_PARTNER_ID))
    set_secret(ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT, str(access_expire_at))
    set_secret(ACCOUNT_AMS_REFRESH_TOKEN_EXPIRE_AT, str(refresh_expire_at))

    del access_token, refresh_token

    print("TOKEN_EXCHANGE_STATUS = PASS")
    print(f"Access Token: {'FOUND' if has_secret(ACCOUNT_AMS_ACCESS_TOKEN) else 'NOT FOUND'}")
    print(f"Refresh Token: {'FOUND' if has_secret(ACCOUNT_AMS_REFRESH_TOKEN) else 'NOT FOUND'}")
    print(f"Request ID: {'FOUND' if request_id else 'NOT FOUND'}")
    print(f"Access Token Expiry (epoch): {access_expire_at}")
    print(f"Refresh Token Expiry (epoch, assumed 30d): {refresh_expire_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
