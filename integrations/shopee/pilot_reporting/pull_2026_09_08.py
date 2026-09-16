#!/usr/bin/env python3
"""
One-off READ-ONLY pull of Shopee data for the CEO pilot date 2026-09-08
(Asia/Ho_Chi_Minh). Uses only ShopeeClient.get() (GET-only, no write
endpoint exists in that module). Writes raw evidence under
pilot_reporting/raw/2026-09-08/ and a small summary JSON — no secrets in
any output.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shopee_client import ShopeeClient, status_from_response  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw" / "2026-09-08"
RAW_DIR.mkdir(parents=True, exist_ok=True)

VN_TZ = timezone(timedelta(hours=7))
DAY_START = datetime(2026, 9, 8, 0, 0, 0, tzinfo=VN_TZ)
DAY_END = datetime(2026, 9, 9, 0, 0, 0, tzinfo=VN_TZ)
TS_START = int(DAY_START.timestamp())
TS_END = int(DAY_END.timestamp())

client = ShopeeClient()
summary: dict = {"report_date": "2026-09-08", "tz": "Asia/Ho_Chi_Minh",
                  "extracted_at": datetime.now(timezone.utc).isoformat()}


def save(name: str, data) -> None:
    with open(RAW_DIR / name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 1) Orders created on 2026-09-08 (create_time window)
order_sns: list[str] = []
cursor = ""
pages = []
for _ in range(20):
    params = {
        "time_range_field": "create_time",
        "time_from": TS_START,
        "time_to": TS_END,
        "page_size": 100,
        "response_optional_fields": "order_status",
    }
    if cursor:
        params["cursor"] = cursor
    resp = client.get("/api/v2/order/get_order_list", params)
    pages.append({"http": resp.get("_http_status"), "error": resp.get("error"),
                   "count": len(resp.get("response", {}).get("order_list", []))})
    order_list = resp.get("response", {}).get("order_list", [])
    order_sns.extend([o["order_sn"] for o in order_list])
    more = resp.get("response", {}).get("more", False)
    cursor = resp.get("response", {}).get("next_cursor", "")
    if not more or not cursor:
        break
    time.sleep(0.3)

save("order_list_pages.json", pages)
save("order_sns.json", order_sns)
summary["orders_status"] = status_from_response({"error": ""}, empty_check=order_sns)
summary["order_count"] = len(order_sns)

# 2) Order detail in batches of 50 (Shopee limit)
order_details = []
for i in range(0, len(order_sns), 50):
    batch = order_sns[i:i + 50]
    resp = client.get("/api/v2/order/get_order_detail", {
        "order_sn_list": ",".join(batch),
        "response_optional_fields": (
            "order_status,total_amount,currency,item_list,payment_method,"
            "cancel_by,cancel_reason,create_time,update_time"
        ),
    })
    order_details.extend(resp.get("response", {}).get("order_list", []))
    time.sleep(0.3)
save("order_detail.json", order_details)
summary["order_detail_status"] = "PASS" if order_details or not order_sns else "PASS_EMPTY"

units = 0
gmv = 0
cancelled = 0
for od in order_details:
    if od.get("order_status") == "CANCELLED":
        cancelled += 1
    for it in od.get("item_list", []):
        units += it.get("model_quantity_purchased", 0)
    gmv += od.get("total_amount", 0) or 0
summary["units_sold_raw_sum_total_amount_items"] = units
summary["order_gross_value_sum_total_amount"] = gmv
summary["cancelled_orders"] = cancelled

# 3) Escrow detail per order (for GMV/fee candidates), only for a capped sample
#    to respect rate limits — full population if small enough.
escrow_details = []
cap = min(len(order_sns), 60)
for order_sn in order_sns[:cap]:
    resp = client.get("/api/v2/payment/get_escrow_detail", {"order_sn": order_sn})
    escrow_details.append(resp)
    time.sleep(0.25)
save("escrow_detail.json", escrow_details)
summary["escrow_pulled"] = len(escrow_details)
summary["escrow_capped"] = cap < len(order_sns)

merchant_subtotal_sum = 0
escrow_amount_sum = 0
escrow_ok = 0
for e in escrow_details:
    order_income = e.get("response", {}).get("order_income", {})
    if not order_income:
        continue
    escrow_ok += 1
    merchant_subtotal_sum += order_income.get("merchant_subtotal", 0) or 0
    escrow_amount_sum += order_income.get("escrow_amount", 0) or (order_income.get("escrow_amount_after_adjustment", 0) or 0)
summary["escrow_orders_with_income"] = escrow_ok
summary["gmv_candidate_merchant_subtotal_sum"] = merchant_subtotal_sum
summary["seller_receivable_candidate_escrow_amount_sum"] = escrow_amount_sum

# 4) Returns updated on 2026-09-08 (15-day max window enforced by API — use exact day)
resp = client.get("/api/v2/returns/get_return_list", {
    "create_time_from": TS_START, "create_time_to": TS_END, "page_size": 100,
})
save("return_list.json", resp)
summary["returns_status"] = status_from_response(resp, empty_check=resp.get("response", {}).get("return", []))
summary["returns_count"] = len(resp.get("response", {}).get("return", []))

# 5) Ads shop-level daily performance for 2026-09-08 (DD-MM-YYYY per prior validated format)
resp = client.get("/api/v2/ads/get_all_cpc_ads_daily_performance", {
    "start_date": "08-09-2026", "end_date": "08-09-2026",
})
save("ads_daily.json", resp)
summary["ads_status"] = status_from_response(resp)
summary["ads_raw"] = {k: resp.get(k) for k in (
    "impression", "clicks", "ctr", "expense", "direct_order", "broad_order",
    "direct_gmv", "broad_gmv", "direct_roas", "broad_roas",
) if k in resp}

# 6) Stock snapshot (item list -> model list) capped to a sample for OOS check
resp_items = client.get("/api/v2/product/get_item_list", {
    "offset": 0, "page_size": 100, "item_status": "NORMAL",
})
save("item_list.json", resp_items)
item_ids = [it["item_id"] for it in resp_items.get("response", {}).get("item", [])]
summary["active_item_count"] = len(item_ids)

oos_count = 0
model_samples = []
for item_id in item_ids[:40]:
    r = client.get("/api/v2/product/get_model_list", {"item_id": item_id})
    model_samples.append(r)
    for m in r.get("response", {}).get("model", []):
        stock = m.get("stock_info_v2", {}).get("summary_info", {}).get("total_available_stock")
        if stock == 0:
            oos_count += 1
    time.sleep(0.2)
save("model_list_sample.json", model_samples)
summary["oos_model_count_in_sampled_40_items"] = oos_count
summary["stock_sample_capped"] = len(item_ids) > 40

save("_summary_2026-09-08.json", summary)
print(json.dumps(summary, indent=2, ensure_ascii=False))
