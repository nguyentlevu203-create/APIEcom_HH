"""
Phase 11: raw-data smoke-test snapshots.

Writes one small JSON file per successfully-tested domain under
    data/raw/tiktok/YYYY-MM-DD/

This is a smoke-test snapshot to prove the data shape/pipeline works end
to end — NOT a full ETL extraction. No token or secret is ever written
here (the underlying API response is already redacted by read_tests.py
before it reaches this module).
"""
from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
RAW_DATA_ROOT = BASE_DIR / "data" / "raw" / "tiktok"

FILENAME_BY_DOMAIN = {
    "orders": "orders.json",
    "finance": "finance_statements.json",
    "analytics": "shop_analytics.json",
    "product": "products.json",
    "return_refund": "returns.json",
    "affiliate": "affiliate_orders.json",
    "bestsellers": "bestsellers.json",
}

SNAPSHOT_ELIGIBLE_STATUSES = {"PASS", "PASS_EMPTY"}


def write_snapshots(results: list[dict[str, Any]], shop_info: dict[str, Any]) -> dict[str, str]:
    """Returns {domain: written_file_path} for every domain actually written."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = RAW_DATA_ROOT / today
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    for r in results:
        domain = r["domain"]
        if r["status"] not in SNAPSHOT_ELIGIBLE_STATUSES:
            continue
        filename = FILENAME_BY_DOMAIN[domain]
        record = {
            "source": "TikTok Shop Open API",
            "shop_name": shop_info.get("shop_name"),
            "shop_region": shop_info.get("region"),
            "shop_id": shop_info.get("shop_id"),
            "api_path": r["path"],
            "api_version": _extract_version(r["path"]),
            "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
            "period": _describe_period(domain),
            "tiktok_request_id": r.get("request_id"),
            "data": r.get("data", {}),
        }
        path = out_dir / filename
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        written[domain] = str(path)

    return written


def _extract_version(path: str) -> str:
    for part in path.strip("/").split("/"):
        if part.isdigit() and len(part) == 6:
            return part
    return "unknown"


def _describe_period(domain: str) -> str:
    if domain in ("orders", "return_refund", "affiliate"):
        return "last_7_days"
    if domain == "analytics":
        return "yesterday_vn"
    return "n/a"
