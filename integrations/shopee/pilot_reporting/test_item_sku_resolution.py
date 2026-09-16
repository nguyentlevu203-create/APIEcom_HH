"""
P5C regression test — Shopee no-variation item_sku resolution.

Guards against the exact bug found in P5B's production COGS recompute
(2026-09-12): for a Shopee order-item line with model_id=0 (no real
variation), Shopee's own get_order_detail response carries item_sku but
NOT model_sku — the ingest path (incr_worker.py / p2a_shopee_to_postgres.py)
used to read only model_sku, silently writing sku='' for every such line.
That gap alone accounted for 552 of P5B's 574 unresolved COGS lines.

These tests use REAL, previously-captured Shopee API evidence (no live
API call needed — order_detail_refetch.json is the exact raw response
P5A.1 fetched live on 2026-09-11) and the already-loaded production
mapping table, proving the full chain end-to-end:

    Shopee get_order_detail (item_sku, model_id=0)
      -> resolve_shopee_item_sku()
      -> core.map_platform_product (channel, shop_id, platform_identifier)
      -> core.dim_cogs (active unit_cost)

    python3 -m pytest test_item_sku_resolution.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import keyring
import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from incr_worker import resolve_shopee_item_sku  # noqa: E402

EVIDENCE_PATH = Path("/Users/VuIT/Desktop/APIClaude/artifacts/v0/_p5a1_raw/order_detail_refetch.json")
KNOWN_NO_VARIATION_ITEM_SKU = "DT02684"  # real, confirmed no-variation Shopee SKU (model_id=0)
SHOP_ID = "1472275791"


# --------------------------- unit-level cases ---------------------------

def test_uses_model_sku_when_variation_present():
    it = {"model_id": 315970228700, "model_sku": "ST01240", "item_sku": ""}
    resolved, evidence = resolve_shopee_item_sku(it)
    assert resolved == "ST01240"
    assert evidence == "model_sku"


def test_falls_back_to_item_sku_when_no_variation():
    it = {"model_id": 0, "model_sku": "", "item_sku": "DT02684"}
    resolved, evidence = resolve_shopee_item_sku(it)
    assert resolved == "DT02684"
    assert evidence == "item_sku"


def test_never_infers_from_product_name():
    it = {"model_id": 0, "model_sku": "", "item_sku": "", "item_name": "Sữa Tắm Thiên Nhiên 250ml"}
    resolved, evidence = resolve_shopee_item_sku(it)
    assert resolved is None
    assert evidence == "NONE"


def test_model_sku_wins_even_if_item_sku_also_present():
    # Real observed shape (order_detail_sample.json): a variation line can
    # carry both fields populated; model_sku must still win.
    it = {"model_id": 315970228700, "model_sku": "ST01240", "item_sku": "IGNORE_ME"}
    resolved, evidence = resolve_shopee_item_sku(it)
    assert resolved == "ST01240"
    assert evidence == "model_sku"


# --------------------------- end-to-end, real evidence -------------------

def _load_real_no_variation_lines() -> list[dict]:
    with open(EVIDENCE_PATH, encoding="utf-8") as f:
        orders = json.load(f)
    lines = []
    for o in orders:
        for it in o.get("item_list", []):
            if (it.get("item_sku") or "").strip() == KNOWN_NO_VARIATION_ITEM_SKU:
                lines.append(it)
    return lines


def test_real_evidence_file_still_has_the_known_case():
    lines = _load_real_no_variation_lines()
    assert len(lines) > 0, (
        f"expected at least one real order line with item_sku={KNOWN_NO_VARIATION_ITEM_SKU!r} "
        f"in {EVIDENCE_PATH} — evidence file missing/changed, test fixture needs updating"
    )
    for it in lines:
        assert it.get("model_id") == 0
        assert not (it.get("model_sku") or "").strip()


def test_resolution_matches_production_mapping_table():
    lines = _load_real_no_variation_lines()
    resolved, evidence = resolve_shopee_item_sku(lines[0])
    assert resolved == KNOWN_NO_VARIATION_ITEM_SKU
    assert evidence == "item_sku"

    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    conn = psycopg2.connect(url)
    del url
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT hh_sku, identifier_source FROM core.map_platform_product "
            "WHERE channel='SHOPEE' AND shop_id=%s AND platform_identifier=%s;",
            (SHOP_ID, resolved),
        )
        row = cur.fetchone()
        assert row is not None, (
            f"core.map_platform_product has no row for the resolved item_sku {resolved!r} — "
            f"the mapping table itself regressed, not just the fact-table join"
        )
        hh_sku, identifier_source = row
        assert identifier_source == "item_sku"

        cur.execute(
            "SELECT unit_cost FROM core.dim_cogs WHERE sku=%s AND is_active AND effective_from IS NULL;",
            (hh_sku,),
        )
        cogs_row = cur.fetchone()
        assert cogs_row is not None, f"hh_sku={hh_sku!r} resolved but has no active core.dim_cogs row"
        assert cogs_row[0] is not None and cogs_row[0] >= 0
    finally:
        conn.close()


def test_backfilled_fact_order_item_resolves_end_to_end():
    """Full chain through the ACTUAL fact table (not just the cached
    evidence file), for a real Shopee order-item line that had sku=''
    before the P5C backfill (_p5c_backfill_shopee.py):
    fact_order_item.sku -> map_platform_product -> dim_cogs."""
    order_id, order_item_id = "260908GVTYNE1R", "242580724254340"
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    conn = psycopg2.connect(url)
    del url
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT shop_id, sku, item_sku, model_sku, item_id, model_id FROM core.fact_order_item "
            "WHERE channel='SHOPEE' AND order_id=%s AND order_item_id=%s;",
            (order_id, order_item_id),
        )
        row = cur.fetchone()
        assert row is not None, f"fixture order-item ({order_id}, {order_item_id}) not found — check it still exists"
        shop_id, sku, item_sku, model_sku, item_id, model_id = row
        assert sku == KNOWN_NO_VARIATION_ITEM_SKU, f"expected backfilled sku={KNOWN_NO_VARIATION_ITEM_SKU!r}, got {sku!r}"
        assert item_sku == KNOWN_NO_VARIATION_ITEM_SKU
        assert not (model_sku or "").strip()
        assert model_id == "0"

        cur.execute(
            "SELECT hh_sku FROM core.map_platform_product WHERE channel='SHOPEE' AND shop_id=%s AND platform_identifier=%s;",
            (shop_id, sku),
        )
        m = cur.fetchone()
        assert m is not None
        cur.execute(
            "SELECT unit_cost FROM core.dim_cogs WHERE sku=%s AND is_active AND effective_from IS NULL;",
            (m[0],),
        )
        c = cur.fetchone()
        assert c is not None and c[0] is not None
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
