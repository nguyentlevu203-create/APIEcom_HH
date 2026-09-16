#!/usr/bin/env python3
"""
Step 1 + Step 2: exchange the one-time authorization code for tokens, then
report which of the requested P0 scopes were actually granted.

Run this yourself in your own terminal (not pasted into a chat):

    cd integrations/tiktok_shop
    pip install -r requirements.txt
    python authorize.py

You will be prompted for:
  - App Key        (visible input — not secret, shown in Partner Center UI)
  - App Secret     (hidden input — never echoed, never stored on disk)
  - Authorization Code (hidden input — from the redirect URL's `code`
                         param; valid once, expires ~30 minutes after
                         the seller authorized)

Nothing typed here is printed back, logged, or written to any file except
the resulting access_token / refresh_token, which are saved to
tokens.json with owner-only file permissions (0600). That file is in
.gitignore — never commit it.
"""
from __future__ import annotations

import getpass
import os
import sys

from config import APP_KEY, REQUESTED_P0_SCOPES
from keychain import get_app_secret_or_prompt
from tiktok_client import TikTokAPIError, TikTokShopClient, mask
from token_store import save_tokens

EXPIRED_HINTS = ("expire", "invalid", "auth code", "auth_code")


def prompt_app_key() -> str:
    env_val = os.environ.get("TIKTOK_APP_KEY")
    if env_val:
        return env_val
    return input(f"App Key [{APP_KEY}]: ").strip() or APP_KEY


def main() -> int:
    app_key = prompt_app_key()
    app_secret = get_app_secret_or_prompt(app_key)
    auth_code = getpass.getpass("Authorization Code (hidden): ").strip()

    if not app_key or not app_secret or not auth_code:
        print("App Key, App Secret, and Authorization Code are all required.")
        return 1

    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)

    try:
        data = client.exchange_token(auth_code)
    except TikTokAPIError as e:
        msg = (e.message or "").lower()
        if any(hint in msg for hint in EXPIRED_HINTS):
            print("\nAUTH CODE EXPIRED — REAUTHORIZE REQUIRED.")
            return 1
        print(f"\nToken exchange FAILED — code={e.code} message={e.message}")
        return 1
    except Exception as e:  # network / unexpected
        print(f"\nToken exchange FAILED — unexpected error: {e}")
        return 1

    granted_scopes = data.get("granted_scopes", [])
    user_type = data.get("user_type")
    region = data.get("seller_base_region")
    seller_name = data.get("seller_name")

    print("\nToken exchange: PASS")
    print(f"seller_name           : {seller_name}")
    print(f"seller_base_region    : {region}")
    print(f"user_type             : {user_type} (expected 0 = seller token)")
    print(f"open_id               : {data.get('open_id')}")
    print(f"access_token          : {mask(data.get('access_token'))} (hidden)")
    print(f"refresh_token         : {mask(data.get('refresh_token'))} (hidden)")
    print(f"access_token_expire_in : {data.get('access_token_expire_in')} (epoch timestamp)")
    print(f"refresh_token_expire_in: {data.get('refresh_token_expire_in')} (epoch timestamp)")
    print(f"granted_scopes         : {granted_scopes}")

    if user_type != 0:
        print("\n⚠️  WARNING: user_type is not 0 — this does not look like a seller token.")
    if region and region != "VN":
        print(f"\n⚠️  WARNING: seller_base_region is '{region}', expected 'VN'.")

    data["app_key"] = app_key
    save_tokens(data)
    print(f"\nTokens saved locally (0600 permissions): {os.path.join(os.path.dirname(__file__), 'tokens.json')}")

    print("\n--- Step 2: Scope check (requested P0 vs granted) ---")
    print(f"{'SCOPE':45} {'REQUESTED':10} {'GRANTED':10} STATUS")
    for scope in REQUESTED_P0_SCOPES:
        granted = scope in granted_scopes
        status = "OK" if granted else "MISSING"
        print(f"{scope:45} {'YES':10} {'YES' if granted else 'NO':10} {status}")

    print("\nNext: run `python test_authorized_shops.py` to verify Get Authorized Shops.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
