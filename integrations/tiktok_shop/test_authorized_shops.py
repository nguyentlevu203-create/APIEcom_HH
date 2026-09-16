#!/usr/bin/env python3
"""
Phase 6: call Get Authorized Shops and confirm the returned shop is
EXACTLY Le Petit Marseillais Vietnam / region VN — no fuzzy/"close enough"
matching. If it doesn't match exactly, this stops and reports failure
rather than guessing.

Can be run standalone:

    python3 test_authorized_shops.py

App Secret comes from the macOS Keychain (see setup_app_secret.py) and
falls back to a hidden prompt only if the Keychain has nothing stored yet.
"""
from __future__ import annotations

import sys

from config import APP_KEY, SHOP_EXPECTED_NAME, SHOP_EXPECTED_REGION
from keychain import get_app_secret_or_prompt
from tiktok_client import NetworkError, SecurityError, TikTokShopClient
from token_store import load_tokens, save_shop_info


def run(client: TikTokShopClient, access_token: str) -> tuple[bool, dict]:
    """Returns (pass: bool, shop_info or {})."""
    try:
        resp = client.get_authorized_shops(access_token)
    except (NetworkError, SecurityError) as e:
        print(f"AUTHORIZED SHOP               FAIL — {e}")
        return False, {}

    if not resp.ok:
        print(f"AUTHORIZED SHOP               FAIL — code={resp.code} message={resp.message}")
        return False, {}

    shops = resp.data.get("shops", [])
    match = None
    for shop in shops:
        if shop.get("name") == SHOP_EXPECTED_NAME and shop.get("region") == SHOP_EXPECTED_REGION:
            match = shop
            break

    if not match:
        print(
            f"AUTHORIZED SHOP               FAIL — no shop exactly matching "
            f"name={SHOP_EXPECTED_NAME!r} region={SHOP_EXPECTED_REGION!r}. "
            f"Shops returned: {[(s.get('name'), s.get('region')) for s in shops]}"
        )
        print("STOP — will not proceed with a different/similar shop.")
        return False, {}

    shop_info = {
        "shop_id": match.get("id") or match.get("shop_id"),
        "shop_cipher": match.get("cipher") or match.get("shop_cipher"),
        "shop_code": match.get("code"),
        "shop_name": match.get("name"),
        "region": match.get("region"),
        "seller_type": match.get("seller_type"),
    }
    save_shop_info(shop_info)

    print("AUTHORIZED SHOP               PASS")
    print(f"SHOP NAME                     {shop_info['shop_name']}")
    print(f"REGION                        {shop_info['region']}")
    print(f"SHOP CIPHER                   {'FOUND' if shop_info['shop_cipher'] else 'MISSING'}")
    return True, shop_info


def main() -> int:
    tokens = load_tokens()
    if not tokens:
        print("No tokens.json found. Run authorize.py first.")
        return 1

    app_key = tokens.get("app_key") or APP_KEY
    app_secret = get_app_secret_or_prompt(app_key)
    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)

    ok, _ = run(client, tokens["access_token"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
