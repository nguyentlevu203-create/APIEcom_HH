#!/usr/bin/env python3
"""
Shopee Open Platform V2 — refresh access_token using the existing stored
refresh_token (mirrors token_exchange.py's structure; same public
signature scheme: base_string = partner_id + path + timestamp).

POST {API_HOST}/api/v2/auth/access_token/get
body: {refresh_token, shop_id, partner_id}

This does not create a new authorization and does not touch shop/order/
product state — it only extends the existing session using the
already-granted refresh_token (valid 30 days from original issuance).
Never prints access_token/refresh_token values.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import API_HOST, LIVE_PARTNER_ID, TOKEN_REFRESH_PATH  # noqa: E402
from keychain import (  # noqa: E402
    ACCOUNT_ACCESS_TOKEN,
    ACCOUNT_ACCESS_TOKEN_EXPIRE_AT,
    ACCOUNT_LIVE_PARTNER_KEY,
    ACCOUNT_REFRESH_TOKEN,
    ACCOUNT_REFRESH_TOKEN_EXPIRE_AT,
    ACCOUNT_SHOP_ID,
    get_secret,
    has_secret,
    set_secret,
)


def sign(partner_id: int, path: str, timestamp: int, partner_key: str) -> str:
    base_string = f"{partner_id}{path}{timestamp}"
    return hmac.new(
        partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def main() -> int:
    partner_key = get_secret(ACCOUNT_LIVE_PARTNER_KEY)
    refresh_token = get_secret(ACCOUNT_REFRESH_TOKEN)
    shop_id_str = get_secret(ACCOUNT_SHOP_ID)
    if not partner_key or not refresh_token or not shop_id_str:
        print("SHOPEE REFRESH = FAIL (missing partner_key/refresh_token/shop_id in Keychain)")
        return 1
    shop_id = int(shop_id_str)

    timestamp = int(time.time())
    partner_id = LIVE_PARTNER_ID
    s = sign(partner_id, TOKEN_REFRESH_PATH, timestamp, partner_key)
    del partner_key

    url = f"{API_HOST}{TOKEN_REFRESH_PATH}"
    params = {"partner_id": partner_id, "timestamp": timestamp, "sign": s}
    body = {"refresh_token": refresh_token, "shop_id": shop_id, "partner_id": partner_id}
    del refresh_token

    resp = requests.post(url, params=params, json=body, timeout=15)
    try:
        data = resp.json()
    except ValueError:
        print(f"SHOPEE REFRESH = FAIL (non-JSON, http={resp.status_code})")
        return 1

    error = data.get("error", "")
    if error or resp.status_code != 200:
        print(f"SHOPEE REFRESH = FAIL (http={resp.status_code} error={error!r} message={str(data.get('message',''))!r})")
        return 1

    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    expire_in = data.get("expire_in")
    if not new_access or not new_refresh:
        print("SHOPEE REFRESH = FAIL (tokens missing from response)")
        return 1

    now = int(time.time())
    access_expire_at = now + int(expire_in) if expire_in else now + 4 * 60 * 60
    refresh_expire_at = now + 30 * 24 * 60 * 60

    set_secret(ACCOUNT_ACCESS_TOKEN, new_access)
    set_secret(ACCOUNT_REFRESH_TOKEN, new_refresh)
    set_secret(ACCOUNT_ACCESS_TOKEN_EXPIRE_AT, str(access_expire_at))
    set_secret(ACCOUNT_REFRESH_TOKEN_EXPIRE_AT, str(refresh_expire_at))
    del new_access, new_refresh

    print("SHOPEE REFRESH = PASS")
    print(f"Access Token: {'FOUND' if has_secret(ACCOUNT_ACCESS_TOKEN) else 'NOT FOUND'}")
    print(f"New Access Token Expiry (epoch): {access_expire_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
