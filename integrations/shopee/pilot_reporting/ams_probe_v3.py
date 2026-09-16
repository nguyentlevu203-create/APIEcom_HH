#!/usr/bin/env python3
"""AMS ("Affiliate for order", Live Partner_id 2044772) V3 probe — corrected
endpoint paths + documented request parameters. Read-only GETs only. Uses
only AMS_* Keychain secrets, never touches production Shopee credentials.
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

OUT_PATH = Path(__file__).parent / "raw" / "ams_probe_v3_results.json"

partner_key = keyring.get_password(KEYCHAIN_SERVICE, ACCOUNT_AMS_LIVE_PARTNER_KEY)
access_token = keyring.get_password(KEYCHAIN_SERVICE, ACCOUNT_AMS_ACCESS_TOKEN)
shop_id = int(keyring.get_password(KEYCHAIN_SERVICE, ACCOUNT_AMS_SHOP_ID))


def call(path: str, extra: dict) -> dict:
    ts = int(time.time())
    base = f"{AMS_PARTNER_ID}{path}{ts}{access_token}{shop_id}"
    s = hmac.new(partner_key.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    params = {"partner_id": AMS_PARTNER_ID, "timestamp": ts, "sign": s, "shop_id": shop_id, "access_token": access_token}
    params.update(extra)
    r = requests.get(f"{API_HOST}{path}", params=params, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {"error": "non_json_response", "message": r.text[:300]}
    data["_http_status"] = r.status_code
    return data


def show(label, data):
    print(f"\n--- {label} ---")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
    return data


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True)
    args = ap.parse_args()

    results = {}
    if OUT_PATH.exists():
        results = json.loads(OUT_PATH.read_text(encoding="utf-8"))

    if args.step == "update_time":
        for mt in ["AmsMarker", "AMS_MARKER", "1"]:
            d = call("/api/v2/ams/get_performance_data_update_time", {"marker_type": mt})
            show(f"update_time marker_type={mt}", d)
            results.setdefault("update_time", {})[mt] = d

    elif args.step == "shop_performance":
        d = call("/api/v2/ams/get_shop_performance", {
            "channel": "AllChannel", "period_type": "Day",
            "start_date": "2026-09-08", "end_date": "2026-09-08",
            "order_type": "ConfirmedOrder",
        })
        show("shop_performance", d)
        results["shop_performance"] = d

    elif args.step == "affiliate_performance":
        d = call("/api/v2/ams/get_affiliate_performance", {
            "channel": "AllChannel", "period_type": "Day",
            "start_date": "2026-09-08", "end_date": "2026-09-08",
            "order_type": "ConfirmedOrder", "page_no": 1, "page_size": 20,
        })
        show("affiliate_performance", d)
        results["affiliate_performance"] = d

    elif args.step == "product_performance":
        d = call("/api/v2/ams/get_product_performance", {
            "channel": "AllChannel", "period_type": "Day",
            "start_date": "2026-09-08", "end_date": "2026-09-08",
            "order_type": "ConfirmedOrder", "page_no": 1, "page_size": 20,
        })
        show("product_performance", d)
        results["product_performance"] = d

    elif args.step == "content_performance":
        d = call("/api/v2/ams/get_content_performance", {
            "channel": "AllChannel", "period_type": "Day",
            "start_date": "2026-09-08", "end_date": "2026-09-08",
            "page_no": 1, "page_size": 20,
        })
        show("content_performance", d)
        results["content_performance"] = d

    elif args.step == "conversion_report":
        import datetime
        start = int(datetime.datetime(2026, 9, 8, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 8, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
        d = call("/api/v2/ams/get_conversion_report", {
            "page_no": 1, "page_size": 20,
            "place_order_time_start": start, "place_order_time_end": end,
        })
        show("conversion_report", d)
        results["conversion_report"] = d

    elif args.step == "validation_list":
        d = call("/api/v2/ams/get_validation_list", {"page_no": 1, "page_size": 20})
        show("validation_list", d)
        results["validation_list"] = d

    elif args.step == "validation_report":
        vl = results.get("validation_list", {})
        response = vl.get("response") or {}
        rows = response.get("validation_list") or response.get("list") or []
        if not rows:
            print("No validation_list rows available yet — run --step validation_list first.")
            sys.exit(1)
        row = rows[0]
        params = {
            "validation_id": row.get("validation_id"),
            "validation_month": row.get("validation_month"),
            "campaign_source": row.get("campaign_source"),
            "page_no": 1, "page_size": 20,
        }
        d = call("/api/v2/ams/get_validation_report", params)
        show(f"validation_report (no time range) params={params}", d)
        results["validation_report_no_time"] = d

    elif args.step == "query_affiliate_list":
        d = call("/api/v2/ams/query_affiliate_list", {"page_no": 1, "page_size": 20})
        show("query_affiliate_list", d)
        results["query_affiliate_list"] = d

    elif args.step == "managed_affiliate_list":
        d = call("/api/v2/ams/get_managed_affiliate_list", {"page_no": 1, "page_size": 20})
        show("managed_affiliate_list", d)
        results["managed_affiliate_list"] = d

    elif args.step == "open_campaign_added_product":
        d = call("/api/v2/ams/get_open_campaign_added_product", {"page_no": 1, "page_size": 20})
        show("open_campaign_added_product", d)
        results["open_campaign_added_product"] = d

    else:
        print("unknown step")
        sys.exit(1)

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
