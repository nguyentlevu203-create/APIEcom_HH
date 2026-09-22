#!/usr/bin/env python3
"""P6C.1 Section 8-9 — permanent Gold metric-ownership enforcement.

Every write to mart.gold_channel_daily must go through guarded_upsert()
with an explicit owner ('BASE_GOLD' or 'PNL_ENRICHMENT'). The allowed
metric set per owner is loaded from mart.gold_metric_ownership (the
DB-enforced catalog created by sql/040_p6c1_gold_ownership.sql) — a
script can only write metric_names registered to its own owner. This
makes the P6C overwrite bug structurally impossible to reintroduce: a
base-gold rebuild can no longer touch a PNL-owned metric_name even if a
future edit accidentally re-adds an emit() call for it.
"""
from __future__ import annotations

from typing import Iterable


class OwnershipViolation(Exception):
    pass


def load_ownership(cur) -> dict[str, str]:
    cur.execute("SELECT metric_name, owner FROM mart.gold_metric_ownership;")
    return dict(cur.fetchall())


def guarded_upsert(conn, owner: str, rows: Iterable[tuple]):
    """rows: iterable of (business_date, channel, shop_id, metric_name, value, status).
    Commits internally. Raises OwnershipViolation (no rows written) if any
    row's metric_name is not registered to `owner`."""
    cur = conn.cursor()
    ownership = load_ownership(cur)
    rows = list(rows)

    bad = [r[3] for r in rows if ownership.get(r[3]) != owner]
    if bad:
        raise OwnershipViolation(
            f"{owner} attempted to write metric(s) not owned by it: {sorted(set(bad))}. "
            f"Registered owners: { {m: ownership.get(m) for m in sorted(set(bad))} }"
        )

    for bd, channel, shop_id, metric, value, status in rows:
        cur.execute(
            """
            INSERT INTO mart.gold_channel_daily (business_date, channel, shop_id, metric_name, metric_value, coverage_status)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (business_date, channel, shop_id, metric_name) DO UPDATE SET
                metric_value = EXCLUDED.metric_value, coverage_status = EXCLUDED.coverage_status,
                updated_at = now();
            """,
            (bd, channel, shop_id, metric, value, status),
        )
    conn.commit()
    return len(rows)
