#!/usr/bin/env python3
"""
Phase 10: single-command orchestrator.

    python3 run_all.py

Does, in order:
  1. load App Key (from tokens.json / config default)
  2. load App Secret from the macOS Keychain
  3. load tokens.json
  4. validate token (refresh-token-expired -> REAUTHORIZATION REQUIRED)
  5. refresh access token if within 24h of expiry
  6. validate P0 scopes
  7. Get Authorized Shops
  8. verify exact shop name + region VN (no fuzzy match)
  9. load shop_cipher
 10. test the 7 read-only domains (official endpoints only)
 11. save raw-data smoke-test snapshots for passing domains
 12. run the security audit
 13. generate HH_TIKTOK_OPEN_API_SETUP_REPORT.md
 14. print the short summary block

No authorization code is needed here — this only ever uses an existing
access_token/refresh_token. If both are expired, it stops safely and
prints REAUTHORIZATION REQUIRED without attempting anything further.
"""
from __future__ import annotations

import sys
from pathlib import Path

import refresh_token as refresh_mod
import report as report_mod
import security_audit
import test_authorized_shops as shops_mod
from config import APP_KEY, REQUESTED_P0_SCOPES
from keychain import get_app_secret, has_app_secret
import raw_data_smoke_test
from read_tests import run_all_domain_tests
from tiktok_client import AUTHORIZED_SHOPS_PATH, SecurityError, TikTokShopClient, _enforce_allowlist
from token_store import access_token_needs_refresh, load_tokens, refresh_token_expired

BASE_DIR = Path(__file__).resolve().parent


def self_test_allowlist_guard() -> bool:
    """Prove the read-only allowlist actually blocks a write-shaped call,
    without ever sending a network request, and does not falsely block a
    genuinely allowed one."""
    for method, path in [
        ("PUT", "/product/202502/products/search"),
        ("POST", "/order/202309/orders/cancel"),
        ("DELETE", AUTHORIZED_SHOPS_PATH),
    ]:
        try:
            _enforce_allowlist(method, path)
        except SecurityError:
            continue
        return False  # should have raised — guard is broken

    try:
        _enforce_allowlist("GET", AUTHORIZED_SHOPS_PATH)
    except SecurityError:
        return False  # a genuinely allowed call must NOT raise
    return True


def main() -> int:
    allowlist_guard_ok = self_test_allowlist_guard()

    tokens = load_tokens()
    if not tokens:
        print("No tokens.json found — run authorize.py first (needs a fresh authorization code).")
        return 1

    app_key = tokens.get("app_key") or APP_KEY

    if not has_app_secret(app_key):
        print("READY FOR APP SECRET INPUT")
        print("Run this yourself in Terminal.app (App Secret is never pasted into chat):")
        print(f"  cd {BASE_DIR}")
        print("  python3 setup_app_secret.py")
        return 2

    if refresh_token_expired(tokens):
        print("REAUTHORIZATION REQUIRED")
        return 1

    app_secret = get_app_secret(app_key)

    token_auto_refresh_status = "PASS_NOT_NEEDED"
    data_status_ok = True
    data_status_problems: list[str] = []

    if access_token_needs_refresh(tokens):
        try:
            tokens = refresh_mod.do_refresh(app_key, app_secret, tokens["refresh_token"])
            token_auto_refresh_status = "PASS"
        except Exception as e:
            token_auto_refresh_status = "FAIL"
            data_status_ok = False
            data_status_problems.append(f"refresh error: {e}")
        if token_auto_refresh_status == "PASS":
            data_status_ok, data_status_problems = refresh_mod.validate_post_refresh(tokens)

    granted_scopes = set(tokens.get("granted_scopes") or [])
    p0_status = {s: (s in granted_scopes) for s in REQUESTED_P0_SCOPES}

    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)

    shop_ok, shop_info = shops_mod.run(client, tokens["access_token"])

    read_results: list[dict] = []
    written_files: dict[str, str] = {}
    if shop_ok:
        read_results = run_all_domain_tests(
            client, tokens["access_token"], shop_info.get("shop_cipher"), granted_scopes
        )
        written_files = raw_data_smoke_test.write_snapshots(read_results, shop_info)
    else:
        print("\nBLOCKED: Authorized Shops did not PASS — skipping the 7 read-domain tests.")

    audit_result = security_audit.audit()

    verdict, blockers = report_mod.build_verdict(
        shop_ok, shop_info, token_auto_refresh_status, data_status_ok,
        all(p0_status.values()), audit_result, read_results, allowlist_guard_ok,
    )

    report_path = report_mod.generate(
        app_key=app_key,
        tokens=tokens,
        shop_ok=shop_ok,
        shop_info=shop_info,
        p0_status=p0_status,
        read_results=read_results,
        token_auto_refresh_status=token_auto_refresh_status,
        data_status_ok=data_status_ok,
        data_status_problems=data_status_problems,
        audit_result=audit_result,
        written_files=written_files,
        allowlist_guard_ok=allowlist_guard_ok,
        secret_in_keychain=True,
    )

    print()
    report_mod.print_summary(
        shop_ok=shop_ok,
        shop_info=shop_info,
        token_auto_refresh_status=token_auto_refresh_status,
        p0_status=p0_status,
        read_results=read_results,
        audit_result=audit_result,
        verdict=verdict,
    )
    print(f"\nReport: {report_path}")
    return 0 if verdict == "PRODUCTION API READY" else 1


if __name__ == "__main__":
    sys.exit(main())
