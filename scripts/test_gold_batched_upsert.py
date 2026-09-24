"""
P11-FIX-4 — scripts/gold/_gold_ownership.guarded_upsert() batches its
UPSERT (execute_values) instead of one round trip per row. Same SQL,
same single transaction, same final state: duplicate keys collapse to
the LAST occurrence, exactly what the old sequential loop left behind.
No DB: cursor/connection and execute_values are fakes.

Run with: python3 -m pytest scripts/test_gold_batched_upsert.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "gold"))
import _gold_ownership as go  # noqa: E402


class _Cur:
    def __init__(self, ownership):
        self.ownership = ownership

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return list(self.ownership.items())


class _Conn:
    def __init__(self, ownership):
        self.cur = _Cur(ownership)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(go, "execute_values",
                        lambda cur, sql, argslist, page_size=None: calls.append((sql, list(argslist), page_size)))
    return calls


D1, D2 = date(2026, 9, 23), date(2026, 9, 24)


def test_batched_single_statement_single_commit(monkeypatch):
    calls = _capture(monkeypatch)
    conn = _Conn({"orders": "BASE_GOLD", "gmv": "BASE_GOLD"})
    rows = [(D1, "SHOPEE", "s1", "orders", 10, "DERIVABLE"), (D1, "SHOPEE", "s1", "gmv", 100, "DERIVABLE")]
    assert go.guarded_upsert(conn, "BASE_GOLD", rows) == 2
    assert len(calls) == 1
    sql, args, page_size = calls[0]
    assert "ON CONFLICT (business_date, channel, shop_id, metric_name) DO UPDATE" in sql
    assert "updated_at = now()" in sql
    assert args == rows
    assert page_size == go.UPSERT_PAGE_SIZE
    assert conn.commits == 1


def test_duplicate_keys_keep_last_occurrence(monkeypatch):
    calls = _capture(monkeypatch)
    conn = _Conn({"orders": "BASE_GOLD"})
    rows = [
        (D1, "SHOPEE", "s1", "orders", 10, "DERIVABLE"),
        (D2, "SHOPEE", "s1", "orders", 5, "DERIVABLE"),
        (D1, "SHOPEE", "s1", "orders", 11, "MISSING"),
    ]
    assert go.guarded_upsert(conn, "BASE_GOLD", rows) == 3  # reported count unchanged
    args = calls[0][1]
    assert (D1, "SHOPEE", "s1", "orders", 11, "MISSING") in args
    assert (D1, "SHOPEE", "s1", "orders", 10, "DERIVABLE") not in args
    assert len(args) == 2


def test_ownership_violation_writes_nothing(monkeypatch):
    calls = _capture(monkeypatch)
    conn = _Conn({"orders": "BASE_GOLD", "cm2": "PNL_ENRICHMENT"})
    with pytest.raises(go.OwnershipViolation):
        go.guarded_upsert(conn, "BASE_GOLD", [(D1, "SHOPEE", "s1", "cm2", 1, "DERIVABLE")])
    assert calls == [] and conn.commits == 0


def test_null_values_pass_through_unchanged(monkeypatch):
    calls = _capture(monkeypatch)
    conn = _Conn({"affiliate_commission_settled": "BASE_GOLD"})
    rows = [(D1, "TIKTOK", "t1", "affiliate_commission_settled", None, "NOT_SETTLED")]
    go.guarded_upsert(conn, "BASE_GOLD", rows)
    assert calls[0][1] == rows  # NULL stays NULL, never coerced to 0
