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

from psycopg2.extras import execute_values

UPSERT_PAGE_SIZE = 500


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

    # P11-FIX-4 — batched, same statement and same single transaction.
    # Row-by-row execute() cost one network round trip per row (~180ms
    # from a GitHub runner to Neon): _p6a_gold_build.py's ~2.9k rows took
    # 514s -> 561s -> >600s (TIMEOUT, run 35948968552) as history grew.
    # One ON CONFLICT batch cannot touch the same key twice, so duplicate
    # keys are collapsed first keeping the LAST occurrence — exactly the
    # final state the old sequential loop produced.
    last_by_key = {}
    for bd, channel, shop_id, metric, value, status in rows:
        last_by_key[(bd, channel, shop_id, metric)] = (bd, channel, shop_id, metric, value, status)
    execute_values(
        cur,
        """
        INSERT INTO mart.gold_channel_daily (business_date, channel, shop_id, metric_name, metric_value, coverage_status)
        VALUES %s
        ON CONFLICT (business_date, channel, shop_id, metric_name) DO UPDATE SET
            metric_value = EXCLUDED.metric_value, coverage_status = EXCLUDED.coverage_status,
            updated_at = now();
        """,
        list(last_by_key.values()),
        page_size=UPSERT_PAGE_SIZE,
    )
    conn.commit()
    return len(rows)
