#!/usr/bin/env python3
"""
Phase 7: production-safe token refresh.

Two ways to use this module:

1. As a script, to force a refresh right now:
       python3 refresh_token.py

2. As a library call from other scripts (see run_all.py), via
   `ensure_fresh_token()`, which only calls TikTok's refresh endpoint when
   the stored access_token is within REFRESH_BUFFER_SECONDS (24h) of
   expiry — i.e. proactively, before expiry, not reactively after a 401.

App Secret comes from the macOS Keychain — never printed, never persisted
to any file by this module. The prepared refresh request URL (which embeds
app_secret and refresh_token as query params) is never logged.
"""
from __future__ import annotations

import sys
from typing import Any

from config import APP_KEY, REQUESTED_P0_SCOPES, SHOP_EXPECTED_REGION
from keychain import get_app_secret_or_prompt
from tiktok_client import TikTokAPIError, TikTokShopClient, mask
from token_store import (
    access_token_needs_refresh,
    load_tokens,
    refresh_token_expired,
    save_tokens,
)


def do_refresh(app_key: str, app_secret: str, refresh_token_value: str) -> dict[str, Any]:
    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)
    data = client.refresh_token(refresh_token_value)
    # Merge onto the existing record: replace access_token, replace
    # refresh_token if TikTok returned a new one, replace expiry
    # timestamps, replace granted_scopes — but don't lose fields (like
    # seller_name) that a refresh response might omit.
    merged = dict(load_tokens() or {})
    merged.update(data)
    merged["app_key"] = app_key
    save_tokens(merged)
    return merged


def validate_post_refresh(tokens: dict[str, Any]) -> tuple[bool, list[str]]:
    """Returns (data_status_ok, problems)."""
    problems = []
    if tokens.get("user_type") != 0:
        problems.append(f"user_type is {tokens.get('user_type')!r}, expected 0")
    if tokens.get("seller_base_region") != SHOP_EXPECTED_REGION:
        problems.append(
            f"seller_base_region is {tokens.get('seller_base_region')!r}, "
            f"expected {SHOP_EXPECTED_REGION!r}"
        )
    granted = set(tokens.get("granted_scopes") or [])
    missing = [s for s in REQUESTED_P0_SCOPES if s not in granted]
    if missing:
        problems.append(f"P0 scope(s) missing after refresh: {missing}")
    return (len(problems) == 0), problems


def ensure_fresh_token(app_secret: str) -> dict[str, Any]:
    """
    Return a tokens dict guaranteed to have a non-expiring-soon access
    token, refreshing proactively (24h buffer) if needed. Raises
    RuntimeError('REAUTHORIZATION REQUIRED') if the refresh token itself
    has expired.
    """
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("No tokens.json found. Run authorize.py first.")

    if refresh_token_expired(tokens):
        raise RuntimeError("REAUTHORIZATION REQUIRED")

    if not access_token_needs_refresh(tokens):
        return tokens

    app_key = tokens.get("app_key") or APP_KEY
    return do_refresh(app_key, app_secret, tokens["refresh_token"])


def main() -> int:
    tokens = load_tokens()
    if not tokens:
        print("No tokens.json found. Run authorize.py first.")
        return 1

    if refresh_token_expired(tokens):
        print("REAUTHORIZATION REQUIRED")
        return 1

    app_key = tokens.get("app_key") or APP_KEY
    app_secret = get_app_secret_or_prompt(app_key)

    try:
        data = do_refresh(app_key, app_secret, tokens["refresh_token"])
    except TikTokAPIError as e:
        print(f"Token refresh: FAIL — code={e.code} message={e.message}")
        return 1
    except Exception as e:
        print(f"Token refresh: FAIL — unexpected error: {e}")
        return 1

    print("Token refresh: PASS")
    print(f"access_token           : {mask(data.get('access_token'))} (hidden)")
    print(f"refresh_token          : {mask(data.get('refresh_token'))} (hidden)")
    print(f"access_token_expire_in : {data.get('access_token_expire_in')} (epoch)")
    print(f"refresh_token_expire_in: {data.get('refresh_token_expire_in')} (epoch)")

    ok, problems = validate_post_refresh(data)
    if not ok:
        print("\nDATA STATUS FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nDATA STATUS PASS — user_type=0, region=VN, all P0 scopes still granted.")
    print("Updated tokens.json saved (0600 permissions, atomic write).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
