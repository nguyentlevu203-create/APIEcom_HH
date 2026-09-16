#!/usr/bin/env python3
"""
Generate a fresh Shopee Shop Authorization URL for the AMS ("Affiliate
for order", Live Partner_id 2044772) app — a SEPARATE app from the main
production app. Reads only AMS_LIVE_PARTNER_KEY from Keychain.

GET {API_HOST}/api/v2/shop/auth_partner
    ?partner_id={partner_id}&redirect={redirect}&timestamp={timestamp}&sign={sign}

sign = HMAC-SHA256(key=AMS partner_key, msg=partner_id+path+timestamp)

The printed URL is a signed but non-secret redirect link (Shopee's own
OAuth consent page). It contains no Partner Key, no code, no token.
Opening it lets the shop owner authorize the AMS app; Shopee then
redirects back to the configured Live Redirect URL Domain with a new
code + shop_id.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import time
from pathlib import Path
from urllib.parse import quote

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import API_HOST, AUTH_PARTNER_PATH, KEYCHAIN_SERVICE, LIVE_REDIRECT_URL  # noqa: E402

AMS_PARTNER_ID = 2044772
ACCOUNT_AMS_LIVE_PARTNER_KEY = "AMS_LIVE_PARTNER_KEY"


def sign(partner_id: int, path: str, timestamp: int, partner_key: str) -> str:
    base_string = f"{partner_id}{path}{timestamp}"
    return hmac.new(partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).hexdigest()


def main() -> int:
    partner_key = keyring.get_password(KEYCHAIN_SERVICE, ACCOUNT_AMS_LIVE_PARTNER_KEY)
    if not partner_key:
        print("AMS Live Partner Key not found in Keychain.")
        return 1

    timestamp = int(time.time())
    s = sign(AMS_PARTNER_ID, AUTH_PARTNER_PATH, timestamp, partner_key)
    del partner_key

    url = (
        f"{API_HOST}{AUTH_PARTNER_PATH}"
        f"?partner_id={AMS_PARTNER_ID}"
        f"&redirect={quote(LIVE_REDIRECT_URL, safe='')}"
        f"&timestamp={timestamp}"
        f"&sign={s}"
    )
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
