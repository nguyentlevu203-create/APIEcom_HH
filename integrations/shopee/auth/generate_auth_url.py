#!/usr/bin/env python3
"""
Generate a fresh Shopee Shop Authorization URL for the LIVE app.

GET {API_HOST}/api/v2/shop/auth_partner
    ?partner_id={partner_id}&redirect={redirect}&timestamp={timestamp}&sign={sign}

sign = HMAC-SHA256(key=partner_key, msg=partner_id+path+timestamp)

The printed URL is a signed but non-secret redirect link (Shopee's own
OAuth consent page). It contains no Partner Key, no code, no token.
Opening it lets the shop owner re-authorize; Shopee then redirects back
to the configured Live Redirect URL Domain with a new code + shop_id.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import API_HOST, AUTH_PARTNER_PATH, LIVE_PARTNER_ID, LIVE_REDIRECT_URL  # noqa: E402
from keychain import ACCOUNT_LIVE_PARTNER_KEY, get_secret  # noqa: E402
from token_exchange import sign  # noqa: E402


def main() -> int:
    partner_key = get_secret(ACCOUNT_LIVE_PARTNER_KEY)
    if not partner_key:
        print("Live Partner Key not found in Keychain.")
        return 1

    timestamp = int(time.time())
    s = sign(LIVE_PARTNER_ID, AUTH_PARTNER_PATH, timestamp, partner_key)
    del partner_key

    url = (
        f"{API_HOST}{AUTH_PARTNER_PATH}"
        f"?partner_id={LIVE_PARTNER_ID}"
        f"&redirect={quote(LIVE_REDIRECT_URL, safe='')}"
        f"&timestamp={timestamp}"
        f"&sign={s}"
    )
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
