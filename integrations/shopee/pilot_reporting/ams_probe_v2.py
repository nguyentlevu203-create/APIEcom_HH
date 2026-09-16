#!/usr/bin/env python3
"""AMS ("Affiliate for order" app, Live Partner_id 2044772) read-only
permission probe. Uses ONLY the AMS_* keychain secrets (set by
setup_partner_key_ams.py + token_exchange_ams.py) — never touches the
production LIVE_PARTNER_KEY/ACCESS_TOKEN used by the rest of this
project. GET-only. No campaign is created/edited/paused.

Usage: python3 ams_probe_v2.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import keyring
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import API_HOST, KEYCHAIN_SERVICE  # noqa: E402
from auth.token_exchange_ams import (  # noqa: E402
    ACCOUNT_AMS_ACCESS_TOKEN,
    ACCOUNT_AMS_LIVE_PARTNER_KEY,
    ACCOUNT_AMS_SHOP_ID,
    AMS_PARTNER_ID,
)

OUT_PATH = Path(__file__).parent / "raw" / "ams_probe_v2_results.json"

ENDPOINTS = [
    "/api/v2/ams/get_performance_data_update_time",
    "/api/v2/ams/get_shop_performance",
    "/api/v2/ams/get_affiliate_performance",
    "/api/v2/ams/get_product_performance",
    "/api/v2/ams/get_conversion_report",
    "/api/v2/ams/get_campaign_key_metrics_performance",
    "/api/v2/ams/get_affiliate_list",
    "/api/v2/ams/get_open_campaign_product_list",
    "/api/v2/ams/get_validation_report",
    "/api/v2/ams/get_billing_report",
]


def get_secret(account: str):
    return keyring.get_password(KEYCHAIN_SERVICE, account)


def sign(path: str, timestamp: int, partner_key: str, access_token: str, shop_id: int) -> str:
    base_string = f"{AMS_PARTNER_ID}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).hexdigest()


def call(path: str, partner_key: str, access_token: str, shop_id: int) -> dict:
    timestamp = int(time.time())
    s = sign(path, timestamp, partner_key, access_token, shop_id)
    params = {
        "partner_id": AMS_PARTNER_ID,
        "timestamp": timestamp,
        "sign": s,
        "shop_id": shop_id,
        "access_token": access_token,
    }
    resp = requests.get(f"{API_HOST}{path}", params=params, timeout=20)
    try:
        data = resp.json()
    except ValueError:
        data = {"error": "non_json_response", "message": resp.text[:200]}
    data["_http_status"] = resp.status_code
    return data


def main() -> int:
    partner_key = get_secret(ACCOUNT_AMS_LIVE_PARTNER_KEY)
    access_token = get_secret(ACCOUNT_AMS_ACCESS_TOKEN)
    shop_id_raw = get_secret(ACCOUNT_AMS_SHOP_ID)

    if not partner_key or not access_token or not shop_id_raw:
        print("AMS_PERMISSION_TEST = ERROR (missing AMS partner key / access token / shop id — "
              "run setup_partner_key_ams.py then token_exchange_ams.py first)")
        return 1

    shop_id = int(shop_id_raw)
    results = []
    first_pass_path = None

    for path in ENDPOINTS:
        try:
            data = call(path, partner_key, access_token, shop_id)
        except Exception as exc:  # noqa: BLE001
            results.append({"endpoint": path, "result": "REQUEST_ERROR", "detail": type(exc).__name__})
            continue

        http_status = data.get("_http_status")
        error = data.get("error", "")
        message = data.get("message", "")
        request_id = data.get("request_id", "")
        schema_keys = sorted(k for k in data.keys() if k not in ("_http_status",))

        if not error and http_status == 200:
            result = "PASS"
            if first_pass_path is None:
                first_pass_path = path
        elif "permission" in str(error).lower() or "not_authorize" in str(error).lower():
            result = "NO_PERMISSION"
        elif "not_found" in str(error).lower() or http_status == 404:
            result = "NOT_FOUND"
        else:
            result = "FAIL"

        results.append({
            "endpoint": path, "http_status": http_status, "shopee_error_code": error,
            "message": message, "request_id": request_id, "result": result,
            "response_schema_keys": schema_keys,
        })

    del partner_key, access_token

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    for r in results:
        print(r.get("endpoint"), "->", r.get("result"), r.get("shopee_error_code", ""))

    overall = "PASS" if first_pass_path else (
        "NO_PERMISSION" if any(r["result"] == "NO_PERMISSION" for r in results) else "ERROR"
    )
    print(f"\nAMS_PERMISSION_TEST = {overall}")
    print(f"AMS_READ_ENDPOINT_PROVEN = {first_pass_path or 'NONE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
