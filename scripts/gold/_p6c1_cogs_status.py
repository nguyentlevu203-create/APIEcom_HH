#!/usr/bin/env python3
"""P6C.1 Section 11 — failure-behavior guard: identify every
(channel, business_date) that still has at least one COGS-unresolved
order-item line, so PNL-enrichment writers can mark gm1/cm1 for that
date COGS_INCOMPLETE instead of a falsely-confident READY/DERIVABLE."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _p5b_load import build_approved_master  # noqa: E402
from _p5b_recompute import fetch_snapshot, get_conn, run_pass  # noqa: E402


def get_cogs_incomplete_dates() -> set[tuple[str, "date"]]:
    approved = build_approved_master()
    role_map = {sku: (item["default_role"], item["channel_role_override"]) for sku, item in approved.items()}
    conn = get_conn()
    cur = conn.cursor()
    snap = fetch_snapshot(cur)
    cur.execute("SELECT order_item_key, business_date FROM core.fact_order_item;")
    bd_by_key = dict(cur.fetchall())
    conn.close()
    lines = run_pass(snap, role_map)
    return {
        (r["channel"], bd_by_key[row[0]])
        for r, row in zip(lines, snap["order_items"])
        if r["cost_status"] != "OK"
    }


def downgrade_status(channel: str, bd, metric: str, status: str, incomplete: set) -> str:
    if metric in ("sellable_cogs", "promo_gift_cost", "packaging_cost", "total_cost", "gm1", "cm1", "cm2", "profit"):
        if (channel, bd) in incomplete:
            return "COGS_INCOMPLETE"
    return status
