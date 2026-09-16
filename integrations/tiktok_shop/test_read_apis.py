#!/usr/bin/env python3
"""
Phase 8: read-only connectivity tests for Order / Finance / Analytics /
Product / Return-Refund / Affiliate / Bestsellers — official TikTok Shop
Partner Center endpoints only (see tiktok_client.READ_ENDPOINTS).

Only runs if tokens.json and shop_info.json already exist. A domain whose
required scope isn't in granted_scopes is SKIPPED_SCOPE_NOT_GRANTED, never
called, never FAIL. Every call is GET or a "search"-style POST — no write
endpoint exists in this codebase's allowlist (see tiktok_client.SecurityError).
"""
from __future__ import annotations

import sys

from config import APP_KEY
from keychain import get_app_secret_or_prompt
from read_tests import run_all_domain_tests
from tiktok_client import TikTokShopClient
from token_store import load_shop_info, load_tokens


def main() -> int:
    tokens = load_tokens()
    shop = load_shop_info()
    if not tokens:
        print("No tokens.json found. Run authorize.py first.")
        return 1
    if not shop:
        print("No shop_info.json found. Run test_authorized_shops.py first.")
        return 1

    granted_scopes = set(tokens.get("granted_scopes") or [])
    app_key = tokens.get("app_key") or APP_KEY
    app_secret = get_app_secret_or_prompt(app_key)
    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)

    results = run_all_domain_tests(client, tokens["access_token"], shop.get("shop_cipher"), granted_scopes)

    print(f"{'DOMAIN':14} {'METHOD':6} {'HTTP':5} {'CODE':10} {'ROWS':6} STATUS")
    for r in results:
        print(
            f"{r['domain']:14} {r['method']:6} {str(r['http_status'] or '-'): <5} "
            f"{str(r['tiktok_code'] or '-'): <10} {str(r['rows'] if r['rows'] is not None else '-'): <6} "
            f"{r['status']}"
        )
        if r["message"]:
            print(f"    message: {r['message']}")
        if r["request_id"]:
            print(f"    request_id: {r['request_id']}")

    ok_statuses = {"PASS", "PASS_EMPTY", "SKIPPED_SCOPE_NOT_GRANTED", "SKIPPED_MARKET_NOT_SUPPORTED"}
    print("\nNote: no write/update/delete endpoint was called by this script.")
    return 0 if all(r["status"] in ok_statuses for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
