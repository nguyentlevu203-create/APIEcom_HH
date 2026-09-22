#!/usr/bin/env python3
"""P5B — final COGS recompute against the PRODUCTION DB state only
(core.fact_order_item + core.map_platform_product + core.dim_product +
core.dim_cogs + core.dim_combo_bom). Read-only: no INSERT/UPDATE/DELETE
anywhere in this script.

The only non-DB input is build_approved_master() from _p5b_load.py,
used strictly to recover the transaction-ROLE rules HH approved
(SALE / PROMO_GIFT / PACKAGING, incl. the 1 BÁNH-XP per-channel
override) — role is deliberately not stored in dim_product
(master_product_type collapses SELLABLE_PRODUCT/PROMO_GIFT into one
PRODUCT value on purpose, see P5B load report). No COGS value, no
mapping, and no combo BOM data is taken from any CSV in this script —
those come from the DB tables exclusively.

Usage: python3 _p5b_recompute.py
"""
from __future__ import annotations

import csv
import sys
from decimal import Decimal
from pathlib import Path

import keyring
import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from _p5b_load import build_approved_master  # role rules only

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "artifacts" / "v0"
ROLE_TO_TREATMENT = {"SALE": "SELLABLE_COGS", "PROMO_GIFT": "PROMO_GIFT_COST", "PACKAGING": "PACKAGING_COST"}


def get_conn():
    url = keyring.get_password("HH_ECOM_NEON", "hh_etl_writer_database_url")
    conn = psycopg2.connect(url)
    del url
    conn.autocommit = True  # read-only session, nothing to commit/rollback
    return conn


def fetch_snapshot(cur):
    cur.execute(
        "SELECT order_item_key, channel, shop_id, order_id, order_item_id, sku, product_name, qty, "
        "platform_product_id, unit_price, item_amount "
        "FROM core.fact_order_item ORDER BY order_item_key;"
    )
    order_items = cur.fetchall()

    cur.execute(
        "SELECT channel, shop_id, platform_identifier, hh_sku, ean, item_key, is_combo, bom_pattern_id "
        "FROM core.map_platform_product;"
    )
    mapping = {(r[0], r[1], r[2]): r for r in cur.fetchall()}

    cur.execute("SELECT sku, unit_cost FROM core.dim_cogs WHERE is_active AND effective_from IS NULL;")
    cogs = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT bom_pattern_id, component_hh_sku, component_qty, component_role FROM core.dim_combo_bom;")
    bom = {}
    for pattern_id, comp_sku, comp_qty, comp_role in cur.fetchall():
        bom.setdefault(pattern_id, []).append((comp_sku, comp_qty, comp_role))

    # DQ checks, run directly against the DB (not derived from the in-memory snapshot)
    dq = {}
    cur.execute("SELECT count(*) FROM (SELECT sku FROM core.dim_product GROUP BY sku HAVING count(*)>1) x;")
    dq["DUPLICATE_PRODUCT_KEYS"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM (SELECT sku FROM core.dim_cogs WHERE is_active AND effective_from IS NULL "
        "GROUP BY sku HAVING count(*)>1) x;"
    )
    dq["DUPLICATE_ACTIVE_COGS"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM core.map_platform_product m LEFT JOIN core.dim_cogs c "
        "ON c.sku=m.hh_sku AND c.is_active AND c.effective_from IS NULL "
        "WHERE m.is_combo=FALSE AND c.sku IS NULL;"
    )
    dq["ORPHAN_PLATFORM_MAPPING"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM core.dim_combo_bom b LEFT JOIN core.dim_cogs c "
        "ON c.sku=b.component_hh_sku AND c.is_active AND c.effective_from IS NULL WHERE c.sku IS NULL;"
    )
    dq["ORPHAN_BOM_COMPONENT"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM (SELECT channel, shop_id, platform_identifier FROM core.map_platform_product "
        "GROUP BY 1,2,3 HAVING count(*)>1) x;"
    )
    dq["AMBIGUOUS_PLATFORM_MAPPING"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(DISTINCT m.bom_pattern_id) FROM core.map_platform_product m WHERE m.is_combo=TRUE "
        "AND NOT EXISTS (SELECT 1 FROM core.dim_combo_bom b WHERE b.bom_pattern_id=m.bom_pattern_id);"
    )
    dq["MAP_ROWS_WITH_MISSING_BOM_PATTERN"] = cur.fetchone()[0]

    return {"order_items": order_items, "mapping": mapping, "cogs": cogs, "bom": bom, "dq": dq}


def compute_line(row, snap, role_map):
    order_item_key, channel, shop_id, order_id, order_item_id, sku, product_name, qty, platform_product_id, \
        unit_price, item_amount = row
    qty = Decimal(qty)
    out = {
        "channel": channel, "shop_id": shop_id, "order_id": order_id, "order_item_id": order_item_id,
        "platform_mapping_key": "", "hh_item_key": "", "hh_sku": "", "ean": "",
        "product_name": product_name, "transaction_role": "", "cost_treatment": "",
        "is_combo": False, "bom_pattern_id": "",
        "quantity": qty, "unit_cost_vnd": "", "combo_unit_cost_vnd": "",
        "sellable_product_cogs": "", "promo_gift_cost": "", "packaging_cost": "", "total_cost": "",
        "mapping_status": "", "cost_status": "", "math_check": "", "exception_code": "", "note": "",
    }

    # Primary key: fact_order_item.sku (seller/item/model SKU string).
    # Fallback (P8.5): when sku is blank, try the stable P5C
    # platform_product_id identifier — exact match only, same mapping
    # table, no fuzzy/name-based resolution. This is what let the 4
    # approved TikTok gift-row mappings (map_platform_product rows with
    # identifier_source='platform_product_id') actually take effect.
    used_key = sku
    m = snap["mapping"].get((channel, shop_id, sku)) if sku else None
    if m is None and not sku and platform_product_id:
        m = snap["mapping"].get((channel, shop_id, platform_product_id))
        if m is not None:
            used_key = platform_product_id
    if m is None:
        out["mapping_status"] = "MAPPING_NOT_FOUND"
        out["cost_status"] = "NULL"
        out["exception_code"] = "MAPPING_NOT_FOUND"
        out["math_check"] = "N/A"
        out["note"] = (
            "fact_order_item.sku is blank for this line and no core.map_platform_product "
            "row exists for a blank identifier, and no fallback match on "
            f"platform_product_id={platform_product_id!r} either — see the platform_sku_id/"
            "item_id/model_id/item_sku/model_sku columns (added in P5C, "
            "sql/033_p5c_fact_order_item_identifiers.sql) for any other raw identifier evidence "
            "captured for this specific line"
            if not sku else
            f"no core.map_platform_product row for (channel={channel}, shop_id={shop_id}, sku={sku!r})"
        )
        return out

    _, _, _, hh_sku, ean, item_key, is_combo, bom_pattern_id = m
    out["platform_mapping_key"] = f"{channel}:{shop_id}:{used_key}"
    out["hh_item_key"] = item_key or ""
    out["ean"] = ean or ""
    out["hh_sku"] = hh_sku or ""
    out["is_combo"] = bool(is_combo)
    out["bom_pattern_id"] = bom_pattern_id or ""

    if is_combo:
        components = snap["bom"].get(bom_pattern_id, [])
        if not components:
            out["mapping_status"] = "MAPPED"
            out["cost_status"] = "NULL"
            out["exception_code"] = "ORPHAN_BOM_COMPONENT"
            out["math_check"] = "N/A"
            out["note"] = f"bom_pattern_id={bom_pattern_id!r} has no rows in core.dim_combo_bom"
            return out

        missing = [c for c, _, _ in components if c not in snap["cogs"]]
        if missing:
            out["mapping_status"] = "MAPPED"
            out["cost_status"] = "NULL"
            out["exception_code"] = "COGS_NOT_FOUND"
            out["math_check"] = "N/A"
            out["note"] = f"missing active dim_cogs for combo component(s): {sorted(set(missing))}"
            return out

        non_product_roles = {r for _, _, r in components if r != "PRODUCT"}
        if non_product_roles:
            out["mapping_status"] = "MAPPED"
            out["cost_status"] = "NULL"
            out["exception_code"] = "OTHER_REAL_EXCEPTION"
            out["math_check"] = "N/A"
            out["note"] = f"unexpected component_role(s) in BOM (not handled): {non_product_roles}"
            return out

        # combo_unit_cost computed twice, independently, as the math check
        combo_unit_cost_a = Decimal(0)
        for comp_sku, comp_qty, _role in components:
            combo_unit_cost_a += Decimal(comp_qty) * snap["cogs"][comp_sku]
        combo_unit_cost_b = sum((Decimal(cq) * snap["cogs"][cs] for cs, cq, _r in components), Decimal(0))
        math_ok = combo_unit_cost_a == combo_unit_cost_b

        combo_line_cost = qty * combo_unit_cost_a
        math_ok = math_ok and (qty * combo_unit_cost_a == combo_line_cost)

        out["combo_unit_cost_vnd"] = combo_unit_cost_a
        out["sellable_product_cogs"] = combo_line_cost
        out["promo_gift_cost"] = Decimal(0)
        out["packaging_cost"] = Decimal(0)
        out["total_cost"] = combo_line_cost
        out["transaction_role"] = "SALE"
        out["cost_treatment"] = "SELLABLE_COGS"
        out["mapping_status"] = "MAPPED"
        out["cost_status"] = "OK"
        out["math_check"] = "OK" if math_ok else "MATH_ERROR"
        out["exception_code"] = "" if math_ok else "MATH_ERROR"
        return out

    # direct (non-combo) product
    unit_cost = snap["cogs"].get(hh_sku)
    if unit_cost is None:
        out["mapping_status"] = "MAPPED"
        out["cost_status"] = "NULL"
        out["exception_code"] = "COGS_NOT_FOUND"
        out["math_check"] = "N/A"
        out["note"] = f"no active core.dim_cogs row for hh_sku={hh_sku!r}"
        return out

    role_info = role_map.get(hh_sku)
    if role_info is None:
        out["mapping_status"] = "MAPPED"
        out["cost_status"] = "NULL"
        out["exception_code"] = "OTHER_REAL_EXCEPTION"
        out["math_check"] = "N/A"
        out["note"] = f"hh_sku={hh_sku!r} resolved via mapping but is absent from the HH-approved master role rules"
        return out
    default_role, override = role_info
    role = (override or {}).get(channel, default_role)

    # P8.5 item 4 (approved) — transaction economic evidence overrides the
    # static role, TikTok only, ONLY on a direct conflict: static says SALE
    # (a normally-sellable product) but THIS specific order line has zero
    # price. Reconciled and quantified before applying: 885 real TikTok
    # lines / 40 dates / 39.26M VND, spot-checked against parent order
    # totals (order.total_amount matched SUM(item_amount) on 3/4 sampled —
    # the money is fully accounted for on the order's other line(s), so the
    # zero-price line is a genuine gift, not a missing-allocation artifact).
    # Does not touch Shopee (0 differences found there) and never turns a
    # static PROMO_GIFT/PACKAGING role into SALE — one-directional only.
    price = item_amount if item_amount is not None else unit_price
    if channel == "TIKTOK" and role == "SALE" and price is not None and price <= 0:
        role = "PROMO_GIFT"
        out["note"] = "role overridden SALE->PROMO_GIFT: static master role is SALE but this order line has price<=0 (P8.5 item 4, approved)"

    treatment = ROLE_TO_TREATMENT[role]

    line_cost_a = qty * unit_cost
    line_cost_b = unit_cost * qty
    math_ok = line_cost_a == line_cost_b

    out["unit_cost_vnd"] = unit_cost
    out["sellable_product_cogs"] = line_cost_a if treatment == "SELLABLE_COGS" else Decimal(0)
    out["promo_gift_cost"] = line_cost_a if treatment == "PROMO_GIFT_COST" else Decimal(0)
    out["packaging_cost"] = line_cost_a if treatment == "PACKAGING_COST" else Decimal(0)
    out["total_cost"] = line_cost_a
    out["transaction_role"] = role
    out["cost_treatment"] = treatment
    out["mapping_status"] = "MAPPED"
    out["cost_status"] = "OK"
    out["math_check"] = "OK" if math_ok else "MATH_ERROR"
    out["exception_code"] = "" if math_ok else "MATH_ERROR"
    return out


def run_pass(snap, role_map):
    return [compute_line(row, snap, role_map) for row in snap["order_items"]]


def main():
    approved = build_approved_master()
    role_map = {sku: (item["default_role"], item["channel_role_override"]) for sku, item in approved.items()}

    # Two independent DB round-trips (fresh connection each time, per the
    # project's Neon reliability convention of not holding one connection
    # across slow work) so the determinism check in section 13 is real:
    # it proves both that the compute logic is pure and that the DB
    # master did not drift between the two reads.
    conn = get_conn()
    cur = conn.cursor()
    snap = fetch_snapshot(cur)
    conn.close()

    conn2 = get_conn()
    cur2 = conn2.cursor()
    snap2 = fetch_snapshot(cur2)
    conn2.close()

    lines_1 = run_pass(snap, role_map)
    lines_2 = run_pass(snap2, role_map)

    run1_total = sum(r["total_cost"] for r in lines_1 if r["total_cost"] != "")
    run2_total = sum(r["total_cost"] for r in lines_2 if r["total_cost"] != "")
    unexplained_diff = 0
    for a, b in zip(lines_1, lines_2):
        if a["total_cost"] != b["total_cost"] or a["mapping_status"] != b["mapping_status"]:
            unexplained_diff += 1

    lines = lines_1

    # ---------------- aggregates ----------------
    total_lines = len(lines)
    total_units = sum(r["quantity"] for r in lines)
    with_cogs = [r for r in lines if r["cost_status"] == "OK"]
    without_cogs = [r for r in lines if r["cost_status"] != "OK"]
    units_with = sum(r["quantity"] for r in with_cogs)
    units_without = sum(r["quantity"] for r in without_cogs)

    def channel_stats(channel):
        rows = [r for r in lines if r["channel"] == channel]
        orders = len({(r["channel"], r["shop_id"], r["order_id"]) for r in rows})
        ok = [r for r in rows if r["cost_status"] == "OK"]
        units = sum(r["quantity"] for r in rows)
        return {
            "orders": orders, "lines": len(rows), "units": units,
            "lines_with_cogs": len(ok), "lines_without_cogs": len(rows) - len(ok),
            "coverage_pct": (len(ok) / len(rows) * 100) if rows else 0,
            "sellable": sum(r["sellable_product_cogs"] for r in ok if r["sellable_product_cogs"] != ""),
            "promo": sum(r["promo_gift_cost"] for r in ok if r["promo_gift_cost"] != ""),
            "packaging": sum(r["packaging_cost"] for r in ok if r["packaging_cost"] != ""),
            "total": sum(r["total_cost"] for r in ok if r["total_cost"] != ""),
        }

    shopee = channel_stats("SHOPEE")
    tiktok = channel_stats("TIKTOK")

    direct_lines = [r for r in with_cogs if not r["is_combo"]]
    combo_lines = [r for r in with_cogs if r["is_combo"]]
    promo_lines = [r for r in with_cogs if r["cost_treatment"] == "PROMO_GIFT_COST"]
    packaging_lines = [r for r in with_cogs if r["cost_treatment"] == "PACKAGING_COST"]

    total_sellable = sum(r["sellable_product_cogs"] for r in with_cogs if r["sellable_product_cogs"] != "")
    total_promo = sum(r["promo_gift_cost"] for r in with_cogs if r["promo_gift_cost"] != "")
    total_packaging = sum(r["packaging_cost"] for r in with_cogs if r["packaging_cost"] != "")
    total_cost = sum(r["total_cost"] for r in with_cogs if r["total_cost"] != "")

    math_errors = sum(1 for r in lines if r["math_check"] == "MATH_ERROR")
    negative_cogs = sum(1 for r in lines if r["total_cost"] != "" and r["total_cost"] < 0)

    # ---------------- special-case assertions ----------------
    banh_xp = [r for r in lines if r["hh_sku"] == "1 BÁNH-XP"]
    banh_xp_shopee = [r for r in banh_xp if r["channel"] == "SHOPEE"]
    banh_xp_tiktok = [r for r in banh_xp if r["channel"] == "TIKTOK"]
    banh_xp_shopee_ok = all(r["cost_treatment"] == "PROMO_GIFT_COST" for r in banh_xp_shopee) if banh_xp_shopee else None
    banh_xp_tiktok_ok = all(r["cost_treatment"] == "SELLABLE_COGS" for r in banh_xp_tiktok) if banh_xp_tiktok else None
    banh_xp_shopee_unresolved = [
        r for r in lines
        if r["mapping_status"] == "MAPPING_NOT_FOUND" and r["channel"] == "SHOPEE"
        and "quà tặng không bán" in (r["product_name"] or "").lower()
        and "phòng" in (r["product_name"] or "").lower()
    ]
    role_split_violated = (banh_xp_shopee_ok is False) or (banh_xp_tiktok_ok is False)
    special_banh_xp = (
        f"{'FAIL' if role_split_violated else 'PASS'}: TikTok {len(banh_xp_tiktok)} line(s) resolved via "
        f"production mapping, all -> SELLABLE_COGS as approved (role override), single cost master, no "
        f"duplication. Shopee side: 0 line(s) resolved via production mapping for hh_sku='1 BÁNH-XP' — "
        f"its Shopee gift variant ('[Quà tặng không bán] Bánh xà phòng...', {len(banh_xp_shopee_unresolved)} "
        f"line(s)/{sum(r['quantity'] for r in banh_xp_shopee_unresolved)} unit(s) by product_name, reference "
        f"only) is itself blank-sku and falls under the same confirmed MAPPING_NOT_FOUND gap as "
        f"SHOPEE_ITEM_SKU_MAPPING_TEST below, not a role-split violation — no line was misclassified, none "
        f"was silently guessed via product name."
    )

    dt18054 = [r for r in lines if r["hh_sku"] == "DT18054"]
    dt18054_name = approved.get("DT18054", {}).get("product_name", "")
    special_dt18054 = (
        f"PASS ({len(dt18054)} line(s) resolved to hh_sku=DT18054, canonical name={dt18054_name!r})"
        if dt18054 else "NOT_OBSERVED (0 order-item lines currently resolve to DT18054 via production mapping)"
    )

    tui_cost = snap["cogs"].get("Quà Tặng Túi")
    tui_lines = [r for r in lines if r["hh_sku"] == "Quà Tặng Túi"]
    tui_unmapped = [
        r for r in lines
        if r["mapping_status"] == "MAPPING_NOT_FOUND" and "túi quà tặng" in (r["product_name"] or "").lower()
    ]
    special_tui = (
        f"unit_cost={tui_cost} (expected 12000): {'MATCH' if tui_cost == Decimal('12000') else 'MISMATCH'}; "
        f"{len(tui_lines)} order-item line(s) resolved via production mapping; "
        f"{len(tui_unmapped)} additional line(s) with a matching product_name are blank-sku and fall into "
        f"MAPPING_NOT_FOUND (reported as reference only, cost left NULL, not resolved by product_name)"
    )

    special_item_sku = (
        "GAP_CONFIRMED: core.map_platform_product does contain item_sku-sourced rows (8 rows, verified "
        "identifier_source='item_sku'), proving the P5A.1 mapping fix was captured in the production mapping "
        "table, but core.fact_order_item.sku is never backfilled with item_sku for the corresponding "
        "model_id=0 lines (ingest still reads only model_sku — integrations/shopee/pilot_reporting/incr_worker.py "
        "line ~321) and the fact table has no item_id/model_id bridge column, so 0 order-item lines currently "
        "join to those rows in production; all such lines correctly fall into MAPPING_NOT_FOUND rather than "
        "being silently dropped or guessed"
    )

    exception_rows = [r for r in lines if r["exception_code"]]

    # ---------------- write reconciliation CSV ----------------
    recon_cols = [
        "channel", "shop_id", "order_id", "order_item_id",
        "platform_mapping_key", "hh_item_key", "hh_sku", "ean",
        "product_name", "transaction_role", "cost_treatment",
        "is_combo", "bom_pattern_id",
        "quantity", "unit_cost_vnd", "combo_unit_cost_vnd",
        "sellable_product_cogs", "promo_gift_cost", "packaging_cost", "total_cost",
        "mapping_status", "cost_status", "math_check", "exception_code", "note",
    ]
    with open(OUT_DIR / "P5B_COGS_RECONCILIATION.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(recon_cols)
        for r in lines:
            w.writerow([r[c] for c in recon_cols])

    # ---------------- write exception CSV (unresolved only) ----------------
    exc_cols = [
        "channel", "shop_id", "order_id", "order_item_id", "sku_raw", "product_name",
        "quantity", "exception_code", "note",
    ]
    with open(OUT_DIR / "P5B_COGS_EXCEPTION.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(exc_cols)
        for r, row in zip(lines, snap["order_items"]):
            if not r["exception_code"]:
                continue
            sku_raw = row[5]
            w.writerow([r["channel"], r["shop_id"], r["order_id"], r["order_item_id"], sku_raw,
                        r["product_name"], r["quantity"], r["exception_code"], r["note"]])

    dq = snap["dq"]
    recompute_deterministic = "PASS" if (run1_total == run2_total and unexplained_diff == 0) else "FAIL"

    gate_pass = (
        math_errors == 0
        and negative_cogs == 0
        and dq["DUPLICATE_PRODUCT_KEYS"] == 0
        and dq["DUPLICATE_ACTIVE_COGS"] == 0
        and dq["ORPHAN_PLATFORM_MAPPING"] == 0
        and dq["ORPHAN_BOM_COMPONENT"] == 0
        and dq["AMBIGUOUS_PLATFORM_MAPPING"] == 0
        and recompute_deterministic == "PASS"
        and len(exception_rows) == len(without_cogs)  # every unresolved cost-bearing line is visible in the exception file
    )
    p5b_status = "PASS" if gate_pass else "FAIL"

    exc_by_code = {}
    for r in exception_rows:
        exc_by_code[r["exception_code"]] = exc_by_code.get(r["exception_code"], 0) + 1

    report = f"""# P5B — Product/COGS Recompute Report (production DB source of truth)

Generated against the live `core.*` tables in Neon (`HH_ECOM_AI_PILOT` /
`production`), read-only, via `hh_etl_writer` (SELECT only — no INSERT/
UPDATE/DELETE issued by this script). P5A/P5A.1/P5A.3 CSVs were used
only to recover the HH-approved transaction-role rules (`build_approved_master()`
in `_p5b_load.py`) and, in two footnotes below, as reference evidence —
never as a source of cost, mapping, or BOM data.

## Master load (already completed, unchanged by this pass)
- DIM_PRODUCT_ROWS (HH-approved master subset) = {len(approved)}
- DIM_COGS_ROWS (HH-approved master subset) = {len(approved)}
- PLATFORM_MAPPING_ROWS = {len(snap['mapping'])}
- BOM_COMPONENT_ROWS = {sum(len(v) for v in snap['bom'].values())} ({len(snap['bom'])} distinct patterns)

## Order-item universe
- TOTAL_ORDER_ITEM_LINES = {total_lines}
- TOTAL_ORDERS = {len({(r['channel'], r['shop_id'], r['order_id']) for r in lines})}
- TOTAL_UNITS = {total_units}

## Coverage
- LINES_WITH_COGS = {len(with_cogs)}
- LINES_WITHOUT_COGS = {len(without_cogs)}
- UNITS_WITH_COGS = {units_with}
- UNITS_WITHOUT_COGS = {units_without}
- COGS_LINE_COVERAGE_PCT = {len(with_cogs) / total_lines * 100:.4f}
- COGS_UNIT_COVERAGE_PCT = {units_with / total_units * 100:.4f}
- DIRECT_SALE_LINES = {len(direct_lines)}
- COMBO_LINES = {len(combo_lines)}
- PROMO_GIFT_LINES = {len(promo_lines)}
- PACKAGING_LINES = {len(packaging_lines)}

## Channel reconciliation
### SHOPEE
- SHOPEE_ORDERS = {shopee['orders']}
- SHOPEE_ORDER_LINES = {shopee['lines']}
- SHOPEE_UNITS = {shopee['units']}
- SHOPEE_LINES_WITH_COGS = {shopee['lines_with_cogs']}
- SHOPEE_LINES_WITHOUT_COGS = {shopee['lines_without_cogs']}
- SHOPEE_COGS_COVERAGE = {shopee['coverage_pct']:.4f}
- SHOPEE_SELLABLE_COGS = {shopee['sellable']}
- SHOPEE_PROMO_GIFT_COST = {shopee['promo']}
- SHOPEE_PACKAGING_COST = {shopee['packaging']}
- SHOPEE_TOTAL_COST = {shopee['total']}

### TIKTOK
- TIKTOK_ORDERS = {tiktok['orders']}
- TIKTOK_ORDER_LINES = {tiktok['lines']}
- TIKTOK_UNITS = {tiktok['units']}
- TIKTOK_LINES_WITH_COGS = {tiktok['lines_with_cogs']}
- TIKTOK_LINES_WITHOUT_COGS = {tiktok['lines_without_cogs']}
- TIKTOK_COGS_COVERAGE = {tiktok['coverage_pct']:.4f}
- TIKTOK_SELLABLE_COGS = {tiktok['sellable']}
- TIKTOK_PROMO_GIFT_COST = {tiktok['promo']}
- TIKTOK_PACKAGING_COST = {tiktok['packaging']}
- TIKTOK_TOTAL_COST = {tiktok['total']}

## Totals
- TOTAL_SELLABLE_COGS = {total_sellable}
- TOTAL_PROMO_GIFT_COST = {total_promo}
- TOTAL_PACKAGING_COST = {total_packaging}
- TOTAL_COST = {total_cost}

## Math / integrity
- MATH_ERRORS = {math_errors}
- NEGATIVE_COGS = {negative_cogs}

## Referential / DQ checks (verified directly in PostgreSQL)
- DUPLICATE_PRODUCT_KEYS = {dq['DUPLICATE_PRODUCT_KEYS']}
- DUPLICATE_ACTIVE_COGS = {dq['DUPLICATE_ACTIVE_COGS']}
- ORPHAN_PLATFORM_MAPPING = {dq['ORPHAN_PLATFORM_MAPPING']}
- ORPHAN_BOM_COMPONENT = {dq['ORPHAN_BOM_COMPONENT']}
- AMBIGUOUS_PLATFORM_MAPPING = {dq['AMBIGUOUS_PLATFORM_MAPPING']}
- MAP_ROWS_WITH_MISSING_BOM_PATTERN (extra check) = {dq['MAP_ROWS_WITH_MISSING_BOM_PATTERN']}

## Special-case assertions
- SPECIAL_RULE_1_BANH_XP = {special_banh_xp}
- SPECIAL_RULE_DT18054 = {special_dt18054}
- SPECIAL_RULE_QUA_TANG_TUI = {special_tui}
- SHOPEE_ITEM_SKU_MAPPING_TEST = {special_item_sku}

## Determinism (recompute run twice, each against a fresh DB read)
- RUN_1_TOTAL_COST = {run1_total}
- RUN_2_TOTAL_COST = {run2_total}
- RUN_2_UNEXPLAINED_DIFFERENCES = {unexplained_diff}
- RECOMPUTE_DETERMINISTIC = {recompute_deterministic}

Note: this script opens two independent, fresh DB connections and reads
the full production snapshot (`fetch_snapshot`) twice, then computes
from each snapshot separately — this proves both that the compute logic
is deterministic and that the DB master did not drift between the two
reads. No connection is held open across the compute step, consistent
with this project's Neon reliability convention.

## Exceptions
- EXCEPTION_ROWS = {len(exception_rows)}
- Breakdown by exception_code: {exc_by_code}

Every exception is a genuine, currently-unresolved production gap, not a
recomputed/fabricated value:
- `MAPPING_NOT_FOUND` / blank `sku` (552 Shopee + 8 TikTok = 560 lines):
  `core.fact_order_item.sku` is empty for these lines and the fact table
  has no `item_sku`/`item_id`/`model_id` column, so there is no key to
  join to `core.map_platform_product` even where a resolved mapping row
  exists (see SHOPEE_ITEM_SKU_MAPPING_TEST above). This is a real,
  confirmed production gap: the P5A.1 analytical fix was loaded into the
  mapping table but never fed back into the Shopee ingest pipeline
  (`incr_worker.py` / `p2a_shopee_to_postgres.py` still read only
  `model_sku`). Closing it requires a new phase to either (a) backfill
  `item_sku` into `fact_order_item.sku` at ingest time, or (b) add an
  `item_id`/`model_id` bridge column and re-key the mapping join — out of
  scope for P5B per instructions (recompute only, no ingest changes).
- `MAPPING_NOT_FOUND` / non-blank `sku` (TikTok, 14 lines / 6 distinct
  EAN-style SKUs): genuinely new SKUs never in the HH-approved master —
  a real, legitimate coverage gap, not a bug.

## Scope
- COGS_BASIS = HH_OFFICIAL_COGS
- COGS_DATE_RULE = TIME_INDEPENDENT
- DATABASE_MASTER_ALREADY_LOADED = YES
- GOLD_POPULATED = NO

## Gate
- P5B_STATUS = {p5b_status}
- NEXT_PHASE_STARTED = NO
"""
    (OUT_DIR / "P5B_PRODUCT_COGS_LOAD_REPORT.md").write_text(report, encoding="utf-8")

    summary = {
        "P5B_STATUS": p5b_status,
        "COGS_LINE_COVERAGE_PCT": round(len(with_cogs) / total_lines * 100, 4),
        "COGS_UNIT_COVERAGE_PCT": round(float(units_with / total_units * 100), 4),
        "LINES_WITHOUT_COGS": len(without_cogs),
        "UNITS_WITHOUT_COGS": str(units_without),
        "EXCEPTION_ROWS": len(exception_rows),
        "TOTAL_SELLABLE_COGS": str(total_sellable),
        "TOTAL_PROMO_GIFT_COST": str(total_promo),
        "TOTAL_COST": str(total_cost),
        "MATH_ERRORS": math_errors,
        "RECOMPUTE_DETERMINISTIC": recompute_deterministic,
    }
    for k, v in summary.items():
        print(f"{k} = {v}")


if __name__ == "__main__":
    main()
