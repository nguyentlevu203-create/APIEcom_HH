#!/usr/bin/env python3
"""
Shop-wide Payments cache — NOT date-namespaced (Payments is a shop-level
ledger, not a per-report-date resource). Pulls the full available history
(TikTok's Get Payments has no long retention limit documented; we cap at a
generous trailing window + page cap, matching what live testing showed is
the shop's whole history) and writes normalized/payments_all.csv.

Used by finance_lifecycle.py / reconciliation_v2.py to resolve
statement.payment_id -> Payments.id (V2.1 Section 1).

    python3 collect_payments_history.py
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

from pilot_common import NORMALIZED_DIR, RAW_DIR, PilotStopError, bootstrap_session, now_utc_iso, write_raw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiktok_client import NetworkError  # noqa: E402

MAX_PAGES = 20
WINDOW_DAYS = 400  # comfortably exceeds the shop's observed full history (365d = 164 rows, no more)


def main() -> int:
    try:
        session = bootstrap_session()
    except PilotStopError as e:
        print(str(e))
        return 1

    now = int(time.time())
    start = now - WINDOW_DAYS * 24 * 3600

    rows: list[dict] = []
    page_token = None
    page = 0
    while True:
        page += 1
        query = {
            "page_size": "50", "sort_field": "create_time", "sort_order": "DESC",
            "create_time_ge": str(start), "create_time_lt": str(now),
        }
        if page_token:
            query["page_token"] = page_token
        try:
            resp = session.client.read_domain(
                "finance_payments", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"), extra_query=query,
            )
        except NetworkError as e:
            print(f"page {page}: FAIL_NETWORK {e}")
            break

        write_raw(
            "payments_history", f"payments_history_page_{page:03d}.json",
            endpoint="finance_payments", api_version="202605", shop_info=session.shop_info,
            request_time_utc=now_utc_iso(), response=resp, page_number=page,
            next_page_token=resp.data.get("next_page_token") if resp.ok else None,
        )

        if not resp.ok:
            print(f"page {page}: code={resp.code} message={resp.message}")
            break

        page_rows = resp.data.get("payments", [])
        rows.extend(page_rows)
        next_token = resp.data.get("next_page_token")
        print(f"page {page}: {len(page_rows)} rows, next={bool(next_token)}")
        if not next_token or page >= MAX_PAGES:
            break
        page_token = next_token
        time.sleep(0.15)

    def amt(d, key="value"):
        v = d.get(key) if isinstance(d, dict) else d
        return v

    out_rows = []
    for p in rows:
        amount = p.get("amount") or {}
        settlement = p.get("settlement_amount") or {}
        before_fx = p.get("payment_amount_before_exchange") or {}
        bank = p.get("bank_account") or ""
        out_rows.append({
            "id": p.get("id"),
            "status": p.get("status"),
            "create_time": p.get("create_time"),
            "paid_time": p.get("paid_time"),
            "currency": amount.get("currency") if isinstance(amount, dict) else None,
            "amount_value": amt(amount),
            "settlement_amount_value": amt(settlement),
            "payment_amount_before_exchange_value": amt(before_fx),
            "exchange_rate": p.get("exchange_rate"),
            # TikTok already masks this ("********9999") — keep as-is, do
            # not store more than what the API itself returns.
            "bank_account_masked": bank,
        })

    path = NORMALIZED_DIR / "payments_all.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "id", "status", "create_time", "paid_time", "currency", "amount_value",
            "settlement_amount_value", "payment_amount_before_exchange_value",
            "exchange_rate", "bank_account_masked",
        ])
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    print(f"\n{len(out_rows)} payments written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
