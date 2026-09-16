#!/usr/bin/env python3
"""
P5A.1 — fetch fresh order_detail for every Shopee order that currently
has at least one blank-sku line item in core.fact_order_item, to recover
item_id/model_id/model_sku (Section 1: "Use existing API/DB evidence in
this priority: item_id, model_id, model_sku if available, product_id/
variation identifier"). Read-only against Shopee (GET only, existing
ShopeeClient, unmodified) and read-only against Neon (SELECT only — no
UPSERT, no dim_product/dim_cogs write, per P5A.1's explicit
DATABASE_WRITTEN=NO gate).

Also fetches get_model_list for every distinct item_id referenced, to
check whether the CATALOG (as opposed to the order-line snapshot) has a
model_sku set — the two can differ if a SKU was added/removed from the
catalog after the order was placed.

Writes raw evidence to artifacts/v0/_p5a1_raw/ (this session's own
scratch evidence, not committed to any DB) and a flat JSON summary.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SHOPEE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHOPEE_DIR))
from shopee_client import ShopeeClient  # noqa: E402

OUT_DIR = Path("/Users/VuIT/Desktop/APIClaude/artifacts/v0")
RAW_DIR = OUT_DIR / "_p5a1_raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    with open(OUT_DIR / "_p5a1_blank_sku_order_ids.json", encoding="utf-8") as f:
        order_ids = json.load(f)

    client = ShopeeClient()
    order_details = []
    for i in range(0, len(order_ids), 50):
        batch = order_ids[i:i + 50]
        resp = client.get("/api/v2/order/get_order_detail", {
            "order_sn_list": ",".join(batch),
            "response_optional_fields": (
                "order_status,total_amount,currency,item_list,create_time,update_time"
            ),
        })
        if resp.get("error"):
            print(f"batch {i//50+1}: ERROR {resp.get('error')} {resp.get('message')}")
            continue
        rows = resp.get("response", {}).get("order_list", [])
        order_details.extend(rows)
        print(f"batch {i//50+1}: requested={len(batch)} returned={len(rows)}")
        time.sleep(0.3)

    with open(RAW_DIR / "order_detail_refetch.json", "w", encoding="utf-8") as f:
        json.dump(order_details, f, ensure_ascii=False, indent=2, default=str)

    # Collect distinct item_ids referenced by any line (not just blank-sku
    # ones — cheaper to fetch the model list once per item than to guess
    # which lines need it) to check the catalog's own model_sku.
    item_ids = set()
    for od in order_details:
        for it in od.get("item_list", []):
            if it.get("item_id"):
                item_ids.add(it["item_id"])
    print(f"distinct item_ids referenced: {len(item_ids)}")

    models_by_item = {}
    for idx, item_id in enumerate(sorted(item_ids), start=1):
        resp = client.get("/api/v2/product/get_model_list", {"item_id": item_id})
        models_by_item[str(item_id)] = resp.get("response", {}) if not resp.get("error") else {"error": resp.get("error")}
        if idx % 25 == 0:
            print(f"model_list fetched: {idx}/{len(item_ids)}")
        time.sleep(0.15)

    with open(RAW_DIR / "model_list_by_item.json", "w", encoding="utf-8") as f:
        json.dump(models_by_item, f, ensure_ascii=False, indent=2, default=str)

    print(f"orders fetched: {len(order_details)}, item catalog models fetched: {len(models_by_item)}")


if __name__ == "__main__":
    main()
