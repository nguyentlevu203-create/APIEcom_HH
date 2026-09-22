#!/usr/bin/env python3
"""P5B — production load of approved HH Product/COGS master, platform
mapping, and combo BOM into Neon, via hh_etl_writer only. Then a full
COGS recompute over every current Shopee+TikTok order item, reading from
the just-loaded production tables (not the P5A/P5A.1 CSVs directly).

Run twice (python3 _p5b_load.py load) to test idempotency; the compute
step (python3 _p5b_load.py compute) is separate and always safe to
re-run (read-only against the loaded tables)."""
from __future__ import annotations

import csv
import json
import sys
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from _gold_db import resolve_writer_url  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "artifacts" / "v0"
KEYMAP_DIR = Path(__file__).resolve().parent
SHOP_IDS = {"SHOPEE": "1472275791", "TIKTOK": "7495998229872806108"}


def D(v):
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def get_db_conn():
    # P11-QUINQUE — env-first (GitHub Actions has no OS Keychain), falls
    # back to Keychain on the local Mac path. See _gold_db.py.
    conn = psycopg2.connect(
        resolve_writer_url(), keepalives=1, keepalives_idle=20, keepalives_interval=10, keepalives_count=3,
    )
    conn.autocommit = False
    return conn


class Counters:
    def __init__(self):
        self.inserted = 0
        self.updated = 0
        self.unchanged = 0

    def record(self, result):
        if result is None:
            self.unchanged += 1
        elif result[0]:
            self.inserted += 1
        else:
            self.updated += 1

    def as_dict(self):
        return {"inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged}


# =====================================================================
# Build the approved master dataset (P5A CSVs + the final HH decisions
# from the user's message this phase — never re-derived by fuzzy logic).
# =====================================================================

EXCLUDED_UNAPPROVED = {"NONEAN:LUOCJARY", "NONEAN:luoc6k", "NONEAN:Luoc"}  # no HH decision given — not loaded


def build_approved_master():
    # P11-QUATER — _p5a_keymap.json is the one hard requirement here:
    # it's the only source for item_type, which every caller in the GOLD
    # chain actually needs (role_map = default_role/channel_role_override,
    # derived from item_type below). No fallback — if it's missing this
    # must fail loudly. Read from KEYMAP_DIR (this file's own directory,
    # scripts/gold/), NOT OUT_DIR (artifacts/v0/, still gitignored) —
    # deliberately separate from the two optional CSVs below so the one
    # real remaining portability gap is exactly "is
    # scripts/gold/_p5a_keymap.json present", nothing more diffuse. This
    # file is intentionally NOT committed by this checkpoint (business/
    # product-catalog data — see the P11-QUATER report); a human decision
    # is needed before that changes.
    with open(KEYMAP_DIR / "_p5a_keymap.json", encoding="utf-8") as f:
        keymap = json.load(f)

    # P11-QUATER — these two CSVs are the one-time historical LOAD's own
    # source data (load_product_and_cogs() below writes their values into
    # core.dim_product/core.dim_cogs); every caller GOLD_CHAIN actually
    # imports (fetch_snapshot/get_conn/run_pass in _p5b_recompute.py,
    # build_cogs_by_day/tiktok_net_sales_from_db in _p6b_pnl_build.py)
    # reads unit_cost from core.dim_cogs directly and never touches
    # pm.get(...)/cm.get(...) below — confirmed by reading every call
    # site. Optional here so a clean checkout (no local P5A CSVs) can
    # still compute role_map; main_load()'s own one-time re-load (run
    # manually, never by GOLD_CHAIN) still needs the real files present.
    product_master = {}
    product_master_path = OUT_DIR / "P5A_PRODUCT_MASTER_NORMALIZED.csv"
    if product_master_path.exists():
        with open(product_master_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                product_master[r["item_key"]] = r

    cogs_master = {}
    cogs_master_path = OUT_DIR / "P5A_COGS_MASTER_NORMALIZED.csv"
    if cogs_master_path.exists():
        with open(cogs_master_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cogs_master[r["item_key"]] = r

    approved = {}  # hh_sku -> row dict
    for item_key, info in keymap.items():
        if item_key in EXCLUDED_UNAPPROVED:
            continue
        hh_sku = info["hh_sku"]
        if not hh_sku:
            continue
        pm = product_master.get(item_key, {})
        cm = cogs_master.get(item_key, {})
        item_type = info["item_type"]

        master_product_type = {
            "SELLABLE_PRODUCT": "PRODUCT", "PROMO_GIFT": "PRODUCT",
            "PACKAGING_AUXILIARY": "PACKAGING_AUXILIARY", "REVIEW_REQUIRED": "PRODUCT",
        }[item_type]
        default_role = {
            "SELLABLE_PRODUCT": "SALE", "PROMO_GIFT": "PROMO_GIFT",
            "PACKAGING_AUXILIARY": "PACKAGING", "REVIEW_REQUIRED": "SALE",
        }[item_type]
        product_name = info["product_name"]
        source_product_name = None

        # ITEM_NAME_001: DT18054 gets its HH-approved canonical name;
        # "nhập lô air" preserved separately for traceability.
        if hh_sku == "DT18054":
            source_product_name = product_name  # "nhập lô air"
            product_name = "Sữa Dưỡng Thể Body Lotion Thiên Nhiên Le Petit Marseillais Hữu Cơ Nhiều Mùi Hương 250ml"

        approved[hh_sku] = {
            "hh_sku": hh_sku, "ean": info["ean"] or "", "item_key": item_key,
            "product_name": product_name, "source_product_name": source_product_name,
            "invoice_name": pm.get("invoice_name") or "", "category": pm.get("category") or "",
            "volume": pm.get("volume") or "", "unit": pm.get("unit") or "",
            "case_pack": pm.get("case_pack") or None, "pallet_pack": pm.get("pallet_pack") or None,
            "master_product_type": master_product_type, "default_role": default_role,
            "unit_cost_vnd": cm.get("unit_cost_vnd"), "source_row": cm.get("source_row") or pm.get("source_row"),
            "channel_role_override": None,
        }

    # ITEM_ROLE_001: 1 BÁNH-XP — channel-dependent role, approved.
    if "1 BÁNH-XP" in approved:
        approved["1 BÁNH-XP"]["channel_role_override"] = {"SHOPEE": "PROMO_GIFT", "TIKTOK": "SALE"}

    # MISSING_COST_001: Quà Tặng Túi — brand-new approved item, no EAN,
    # stable item_key only.
    #
    # P11-QUATER: the real HH-approved unit_cost_vnd figure is
    # deliberately redacted from this version-controlled copy (a real
    # cost figure hardcoded in source is exactly the class of thing this
    # checkpoint's dependency audit exists to keep out of git — see the
    # P11-QUATER report). Safe to redact here specifically because
    # role_map — the only thing GOLD_CHAIN's call path (build_cogs_by_day
    # / _p5b_recompute.py) ever extracts from build_approved_master()'s
    # output — only reads default_role/channel_role_override, confirmed
    # by reading every call site; unit_cost_vnd is never touched. The
    # real value is unchanged in the local-only artifacts/v0/ copy this
    # file was copied from, which main_load()'s one-time historical
    # reload (run manually, never by GOLD_CHAIN) still uses as-is.
    approved["Quà Tặng Túi"] = {
        "hh_sku": "Quà Tặng Túi", "ean": "", "item_key": "NONEAN:Quà Tặng Túi",
        "product_name": "Túi quà tặng sữa tắm dầu gội", "source_product_name": None,
        "invoice_name": "", "category": "", "volume": "", "unit": "",
        "case_pack": None, "pallet_pack": None,
        "master_product_type": "PACKAGING_AUXILIARY", "default_role": "PACKAGING",
        "unit_cost_vnd": None, "source_row": "MISSING_COST_001_HH_APPROVED_2026-09-11",
        "channel_role_override": None,
    }

    return approved


# =====================================================================
# P11-QUATER-BIS — role_map sourced from Neon, not the local keymap file
# =====================================================================

def build_role_map_from_db(cur) -> dict:
    """Replaces build_approved_master() for every GOLD_CHAIN caller that
    only ever wanted role_map (default_role, channel_role_override) —
    every one of them, confirmed by reading each call site (see the
    P11-QUATER report). core.dim_product.default_transaction_role
    already carries this exact classification: parity-tested 97/97
    exact match against every hh_sku _p5a_keymap.json covers before this
    replaced it (see the P11-QUATER-BIS report) — 100% row coverage,
    100% field parity, zero conflicts, zero gaps. No local file, no
    business/product-catalog data leaves Neon.

    The 2 HH-approved exceptions build_approved_master() also hardcoded
    (never sourced from any file — pure code, already safe to commit)
    carry over unchanged:
      - "1 BÁNH-XP": channel-dependent role override (approved).
      - "Quà Tặng Túi": brand-new approved item, no EAN — its role
        (PACKAGING) is data; its real HH-approved unit_cost_vnd is
        deliberately NOT reproduced here (redacted in
        build_approved_master() too — see MISSING_COST_001 above) since
        no caller of this function ever needs unit_cost_vnd from here;
        it comes from core.dim_cogs in fetch_snapshot() instead.
    """
    cur.execute(
        "SELECT sku, default_transaction_role FROM core.dim_product "
        "WHERE default_transaction_role IS NOT NULL;"
    )
    role_map = {sku: (role, None) for sku, role in cur.fetchall()}

    if "1 BÁNH-XP" in role_map:
        default_role, _ = role_map["1 BÁNH-XP"]
        role_map["1 BÁNH-XP"] = (default_role, {"SHOPEE": "PROMO_GIFT", "TIKTOK": "SALE"})
    if "Quà Tặng Túi" not in role_map:
        role_map["Quà Tặng Túi"] = ("PACKAGING", None)

    return role_map


# =====================================================================
# Load dim_product / dim_cogs
# =====================================================================

def load_product_and_cogs(cur, approved: dict, etl_run_id: str) -> dict:
    product_ctr, cogs_ctr = Counters(), Counters()
    for hh_sku, item in approved.items():
        cur.execute(
            """
            INSERT INTO core.dim_product (
                sku, product_name, barcode, is_active, master_product_type,
                invoice_name, volume, unit, case_pack, pallet_pack, source_product_name,
                source_system, source_record_id, source_updated_at, etl_run_id
            ) VALUES (%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (sku) DO UPDATE SET
                product_name = EXCLUDED.product_name, barcode = EXCLUDED.barcode,
                master_product_type = EXCLUDED.master_product_type, invoice_name = EXCLUDED.invoice_name,
                volume = EXCLUDED.volume, unit = EXCLUDED.unit, case_pack = EXCLUDED.case_pack,
                pallet_pack = EXCLUDED.pallet_pack, source_product_name = EXCLUDED.source_product_name,
                source_updated_at = now(), etl_run_id = EXCLUDED.etl_run_id
            WHERE core.dim_product.product_name IS DISTINCT FROM EXCLUDED.product_name
               OR core.dim_product.barcode IS DISTINCT FROM EXCLUDED.barcode
               OR core.dim_product.master_product_type IS DISTINCT FROM EXCLUDED.master_product_type
               OR core.dim_product.invoice_name IS DISTINCT FROM EXCLUDED.invoice_name
               OR core.dim_product.source_product_name IS DISTINCT FROM EXCLUDED.source_product_name
            RETURNING (xmax = 0) AS inserted;
            """,
            (hh_sku, item["product_name"], item["ean"] or None, item["master_product_type"],
             item["invoice_name"] or None, item["volume"] or None, item["unit"] or None,
             D(item["case_pack"]), D(item["pallet_pack"]), item["source_product_name"],
             "P5B_HH_APPROVED", item["source_row"], etl_run_id),
        )
        product_ctr.record(cur.fetchone())

        unit_cost = D(item["unit_cost_vnd"])
        cur.execute(
            """
            INSERT INTO core.dim_cogs (
                sku, effective_from, effective_to, unit_cost, currency,
                cost_basis, cost_date_rule, is_active, source_system, source_record_id
            ) VALUES (%s,NULL,NULL,%s,'VND',%s,'TIME_INDEPENDENT',TRUE,%s,%s)
            ON CONFLICT (sku) WHERE effective_from IS NULL DO UPDATE SET
                unit_cost = EXCLUDED.unit_cost, cost_basis = EXCLUDED.cost_basis,
                is_active = EXCLUDED.is_active, source_record_id = EXCLUDED.source_record_id
            WHERE core.dim_cogs.unit_cost IS DISTINCT FROM EXCLUDED.unit_cost
               OR core.dim_cogs.cost_basis IS DISTINCT FROM EXCLUDED.cost_basis
            RETURNING (xmax = 0) AS inserted;
            """,
            (hh_sku, unit_cost, "HH_OFFICIAL_COGS", "P5B_HH_APPROVED", item["source_row"]),
        )
        cogs_ctr.record(cur.fetchone())

    return {"dim_product": product_ctr.as_dict(), "dim_cogs": cogs_ctr.as_dict()}


# =====================================================================
# Load core.dim_combo_bom (57 patterns: 4 confirmed + 53 HH-approved)
# =====================================================================

def load_combo_bom(cur, etl_run_id: str) -> dict:
    rows = list(csv.DictReader(open(OUT_DIR / "P5A3_BOM_PATTERN_REVIEW.csv", newline="", encoding="utf-8")))
    ctr = Counters()
    for r in rows:
        approval_status = r["approval_status"]
        if approval_status == "HH_APPROVAL_REQUIRED":
            approval_status = "HH_APPROVED"  # COMBO_BOM_001 = APPROVE ALL 53 PATTERNS
        cur.execute(
            """
            INSERT INTO core.dim_combo_bom (
                bom_pattern_id, bom_description, component_hh_sku, component_ean,
                component_item_key, component_qty, component_role, approval_status,
                evidence_source, source_system, etl_run_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (bom_pattern_id, component_hh_sku) DO UPDATE SET
                bom_description = EXCLUDED.bom_description, component_ean = EXCLUDED.component_ean,
                component_qty = EXCLUDED.component_qty, approval_status = EXCLUDED.approval_status,
                etl_run_id = EXCLUDED.etl_run_id
            WHERE core.dim_combo_bom.component_qty IS DISTINCT FROM EXCLUDED.component_qty
               OR core.dim_combo_bom.approval_status IS DISTINCT FROM EXCLUDED.approval_status
            RETURNING (xmax = 0) AS inserted;
            """,
            (r["bom_pattern_id"], r["bom_description"], r["component_hh_sku"],
             r["component_ean_or_item_key"] if r["component_ean_or_item_key"] and r["component_ean_or_item_key"][0].isdigit() else None,
             r["component_ean_or_item_key"], D(r["component_qty"]), r["component_role"],
             approval_status, r["evidence_source"], "P5B_HH_APPROVED", etl_run_id),
        )
        ctr.record(cur.fetchone())
    return {"dim_combo_bom": ctr.as_dict(), "source_rows": len(rows)}


# =====================================================================
# Load core.map_platform_product
# =====================================================================

def load_platform_mapping(cur, approved: dict, etl_run_id: str) -> dict:
    hh_sku_set = set(approved.keys())
    combo_bom_rows = list(csv.DictReader(open(OUT_DIR / "P5A3_BOM_PATTERN_REVIEW.csv", newline="", encoding="utf-8")))
    combo_pattern_by_sku_string = {}  # (channel, sku_string) -> bom_pattern_id  [derived from platform_combo_skus]
    for r in combo_bom_rows:
        for combo_key in r["platform_combo_skus"].split(" | "):
            combo_key = combo_key.strip()
            if not combo_key:
                continue
            parts = combo_key.split(":", 2)
            if len(parts) == 3:
                ch, shop, sku_str = parts
                combo_pattern_by_sku_string[(ch, sku_str)] = r["bom_pattern_id"]

    ctr = Counters()
    rows_written = 0

    # 1) Direct, non-blank fact_order_item.sku values (both channels)
    cur.execute("SELECT DISTINCT channel, shop_id, sku FROM core.fact_order_item WHERE sku IS NOT NULL AND sku <> '';")
    for channel, shop_id, sku in cur.fetchall():
        combo_pattern = combo_pattern_by_sku_string.get((channel, sku))
        if combo_pattern:
            cur.execute(
                """
                INSERT INTO core.map_platform_product (
                    channel, shop_id, platform_identifier, identifier_source, hh_sku, ean, item_key,
                    mapping_method, mapping_confidence, is_combo, bom_pattern_id, source_system, etl_run_id
                ) VALUES (%s,%s,%s,'seller_sku',%s,NULL,%s,'COMBO_BOM','HIGH',TRUE,%s,'P5B_HH_APPROVED',%s)
                ON CONFLICT (channel, shop_id, platform_identifier) DO UPDATE SET
                    is_combo = TRUE, bom_pattern_id = EXCLUDED.bom_pattern_id, etl_run_id = EXCLUDED.etl_run_id
                WHERE core.map_platform_product.bom_pattern_id IS DISTINCT FROM EXCLUDED.bom_pattern_id
                   OR core.map_platform_product.is_combo IS DISTINCT FROM TRUE
                RETURNING (xmax = 0) AS inserted;
                """,
                (channel, shop_id, sku, combo_pattern, combo_pattern, combo_pattern, etl_run_id),
            )
            ctr.record(cur.fetchone())
            rows_written += 1
            continue
        if sku.upper() in {s.upper() for s in hh_sku_set}:
            matched_hh_sku = next(s for s in hh_sku_set if s.upper() == sku.upper())
            item_key = approved[matched_hh_sku]["item_key"]
            ean = approved[matched_hh_sku]["ean"] or None
            cur.execute(
                """
                INSERT INTO core.map_platform_product (
                    channel, shop_id, platform_identifier, identifier_source, hh_sku, ean, item_key,
                    mapping_method, mapping_confidence, is_combo, bom_pattern_id, source_system, etl_run_id
                ) VALUES (%s,%s,%s,'seller_sku',%s,%s,%s,'SELLER_SKU_TO_HH_SKU','HIGH',FALSE,NULL,'P5B_HH_APPROVED',%s)
                ON CONFLICT (channel, shop_id, platform_identifier) DO UPDATE SET
                    hh_sku = EXCLUDED.hh_sku, ean = EXCLUDED.ean, item_key = EXCLUDED.item_key,
                    etl_run_id = EXCLUDED.etl_run_id
                WHERE core.map_platform_product.hh_sku IS DISTINCT FROM EXCLUDED.hh_sku
                RETURNING (xmax = 0) AS inserted;
                """,
                (channel, shop_id, sku, matched_hh_sku, ean, item_key, etl_run_id),
            )
            ctr.record(cur.fetchone())
            rows_written += 1
        # else: not in the approved master (still-unresolved / not yet approved) — no mapping row, correctly

    # 2) Shopee blank-sku closure (P5A.1) — Section 7's explicit item_sku rule
    closure_rows = list(csv.DictReader(open(OUT_DIR / "P5A1_PLATFORM_MAPPING_CLOSURE.csv", newline="", encoding="utf-8")))
    seen_shopee_ident = set()
    for r in closure_rows:
        if r["mapping_status"] not in ("DETERMINISTICALLY_RESOLVED", "COMBO"):
            continue
        ident = r["resolved_platform_sku"]
        key = ("SHOPEE", r["shop_id"], ident)
        if key in seen_shopee_ident:
            continue
        seen_shopee_ident.add(key)
        combo_pattern = combo_pattern_by_sku_string.get(("SHOPEE", ident))
        if r["mapping_status"] == "COMBO" or combo_pattern:
            cur.execute(
                """
                INSERT INTO core.map_platform_product (
                    channel, shop_id, platform_identifier, identifier_source, item_id, model_id,
                    hh_sku, ean, item_key, mapping_method, mapping_confidence, is_combo, bom_pattern_id,
                    source_system, etl_run_id
                ) VALUES ('SHOPEE',%s,%s,%s,%s,%s,%s,NULL,%s,'COMBO_BOM','HIGH',TRUE,%s,'P5B_HH_APPROVED',%s)
                ON CONFLICT (channel, shop_id, platform_identifier) DO UPDATE SET
                    is_combo = TRUE, bom_pattern_id = EXCLUDED.bom_pattern_id, item_id = EXCLUDED.item_id,
                    model_id = EXCLUDED.model_id, etl_run_id = EXCLUDED.etl_run_id
                WHERE core.map_platform_product.bom_pattern_id IS DISTINCT FROM EXCLUDED.bom_pattern_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (r["shop_id"], ident, r["evidence_field"], r["item_id"] or None, r["model_id"] or None,
                 combo_pattern or ident, combo_pattern or ident, combo_pattern or ident, etl_run_id),
            )
            ctr.record(cur.fetchone())
            rows_written += 1
            continue
        matched = next((s for s in hh_sku_set if s.upper() == ident.strip().upper()), None)
        if not matched:
            continue  # not approved — no row, correctly excluded
        item = approved[matched]
        cur.execute(
            """
            INSERT INTO core.map_platform_product (
                channel, shop_id, platform_identifier, identifier_source, item_id, model_id,
                hh_sku, ean, item_key, mapping_method, mapping_confidence, is_combo, bom_pattern_id,
                source_system, etl_run_id
            ) VALUES ('SHOPEE',%s,%s,%s,%s,%s,%s,%s,%s,'SELLER_SKU_TO_HH_SKU','HIGH',FALSE,NULL,'P5B_HH_APPROVED',%s)
            ON CONFLICT (channel, shop_id, platform_identifier) DO UPDATE SET
                hh_sku = EXCLUDED.hh_sku, ean = EXCLUDED.ean, item_key = EXCLUDED.item_key,
                item_id = EXCLUDED.item_id, model_id = EXCLUDED.model_id, etl_run_id = EXCLUDED.etl_run_id
            WHERE core.map_platform_product.hh_sku IS DISTINCT FROM EXCLUDED.hh_sku
            RETURNING (xmax = 0) AS inserted;
            """,
            (r["shop_id"], ident, r["evidence_field"], r["item_id"] or None, r["model_id"] or None,
             matched, item["ean"] or None, item["item_key"], etl_run_id),
        )
        ctr.record(cur.fetchone())
        rows_written += 1

    return {"map_platform_product": ctr.as_dict(), "rows_written": rows_written}


def main_load():
    approved = build_approved_master()
    conn = get_db_conn()
    cur = conn.cursor()
    etl_run_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO control.etl_run_log (etl_run_id, source_system, source_endpoint, run_type, status)
           VALUES (%s,'P5B','production_master_load','p5b_production','running');""",
        (etl_run_id,),
    )
    conn.commit()

    result = {"etl_run_id": etl_run_id, "approved_item_count": len(approved)}
    try:
        result.update(load_product_and_cogs(cur, approved, etl_run_id))
        conn.commit()
        result.update(load_combo_bom(cur, etl_run_id))
        conn.commit()
        result.update(load_platform_mapping(cur, approved, etl_run_id))
        conn.commit()

        rows_processed = sum(
            v["inserted"] + v["updated"] + v["unchanged"] for k, v in result.items()
            if isinstance(v, dict) and "inserted" in v
        )
        cur.execute(
            """UPDATE control.etl_run_log SET status='success', finished_at=now(), rows_processed=%s WHERE etl_run_id=%s;""",
            (rows_processed, etl_run_id),
        )
        conn.commit()
        result["status"] = "success"
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        cur2 = conn.cursor()
        cur2.execute(
            """UPDATE control.etl_run_log SET status='fail', finished_at=now(), error_message=%s WHERE etl_run_id=%s;""",
            (str(e)[:2000], etl_run_id),
        )
        conn.commit()
        result["status"] = "fail"
        result["error"] = str(e)
        conn.close()
        print(json.dumps(result, indent=2, default=str))
        raise

    conn.close()
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main_load()
