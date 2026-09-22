#!/usr/bin/env python3
"""P6B — recover & validate the HH P&L contract (Shopee + TikTok).

Source priority followed (per task Section 1):
  1. Existing code/config that produced the closest thing to a validated
     CEO report found in this project:
       - integrations/shopee/pilot_reporting/reports/HH_SHOPEE_PNL_FIELD_MAPPING_V1.md
         (Shopee fee dictionary + a 30-order zero-residual rollup proof)
       - integrations/shopee/pilot_reporting/reports/net_sales_reconciliation.csv
         (the order-level evidence behind that proof)
       - integrations/tiktok_shop/pilot_reporting/net_sales_validation.py +
         reports/HH_TIKTOK_NET_SALES_VALIDATION_V2.md (TikTok candidate
         Net Sales formula, arithmetically validated across 3 dates)
  2. Existing validated-report-adjacent artifacts: the raw Shopee escrow
     snapshot for 2026-09-08 (98/98 orders, full field detail) and the
     TikTok order_finance_lifecycle_*.csv snapshots for 2026-09-01/05/07/08.
  3. Current production PostgreSQL (core.fact_order, core.fact_settlement,
     core.dim_cogs/dim_product/map_platform_product/dim_combo_bom via the
     unchanged P5 compute engine).

An exhaustive project-wide search (text grep across all source, plus an
openpyxl cell-by-cell scan of every .xlsx in the repo) for the task's
example reference numbers (92,117,000 / 48,318,000 / 21,372,000 /
14,171,000 / 184,003,000 / 397,252,520 / 35,351,150 / 173,743,150, the
CM2 figures, and the terms "DS hủy"/"VXP"/"Booking/KOL/KOC"/"Live
in-house"/"Đóng gói") found NONE of them anywhere in this project except
one exact match recovered independently below (TikTok 2026-09-01 =
14,171,000). This confirms: no signed-off "validated CEO Control Tower"
artifact with this exact GM1/CM1/CM2 structure exists in this repo — the
task's own wording ("Example reference only... do not hard-code") already
anticipated this. This script recovers the closest existing prior art
instead of inventing a new model.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _p5b_load import build_approved_master  # noqa: E402
from _p5b_recompute import fetch_snapshot, get_conn, run_pass  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
TT_DIR = ROOT / "integrations/tiktok_shop/pilot_reporting/normalized"
SHOPEE_ESCROW_98 = ROOT / "integrations/shopee/pilot_reporting/raw/2026-09-08/escrow_detail_full_98.json"

FINALIZED_STATES = {"SETTLED", "STATEMENT_ISSUED", "PAYMENT_PENDING", "PAID"}
D = lambda x: Decimal(str(x)) if x not in (None, "") else Decimal(0)  # noqa: E731


# =====================================================================
# 1. COGS-by-day (reuse the unchanged P5 engine — same as P6A)
# =====================================================================

def build_cogs_by_day():
    approved = build_approved_master()
    role_map = {sku: (item["default_role"], item["channel_role_override"]) for sku, item in approved.items()}
    conn = get_conn()
    cur = conn.cursor()
    snap = fetch_snapshot(cur)
    cur.execute("SELECT order_item_key, business_date FROM core.fact_order_item;")
    bd_by_key = dict(cur.fetchall())
    conn.close()
    lines = run_pass(snap, role_map)
    agg = {}
    for r, row in zip(lines, snap["order_items"]):
        if r["cost_status"] != "OK":
            continue
        key = (r["channel"], bd_by_key[row[0]])
        a = agg.setdefault(key, {"sellable": Decimal(0), "promo": Decimal(0), "packaging": Decimal(0)})
        a["sellable"] += r["sellable_product_cogs"]
        a["promo"] += r["promo_gift_cost"]
        a["packaging"] += r["packaging_cost"]
    return agg


# =====================================================================
# 2. TikTok Net Sales — order-date-joined, from PRODUCTION DB
# =====================================================================

def tiktok_net_sales_from_db(date_from, date_to):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fo.business_date, count(*) FILTER (WHERE fs.settlement_type='MATCHED'),
               sum(fs.revenue_amount) FILTER (WHERE fs.settlement_type='MATCHED'),
               sum(fs.fee_and_tax_amount) FILTER (WHERE fs.settlement_type='MATCHED'),
               sum(fs.shipping_cost_amount) FILTER (WHERE fs.settlement_type='MATCHED'),
               count(*) FILTER (WHERE fs.settlement_type='NOT_SETTLED_YET'),
               count(DISTINCT fo.order_id)
        FROM core.fact_order fo
        LEFT JOIN core.fact_settlement fs
          ON fs.channel=fo.channel AND fs.shop_id=fo.shop_id AND fs.order_id=fo.order_id
        WHERE fo.channel='TIKTOK' AND fo.business_date BETWEEN %s AND %s
        GROUP BY 1 ORDER BY 1;
        """,
        (date_from, date_to),
    )
    rows = cur.fetchall()
    conn.close()
    return {r[0]: {"matched": r[1], "revenue": r[2], "fee": r[3], "shipping": r[4],
                   "not_settled": r[5], "total_orders": r[6]} for r in rows}


def tiktok_net_sales_from_csv(date_str):
    path = TT_DIR / f"order_finance_lifecycle_{date_str}.csv"
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fin = [r for r in rows if r["finance_state"] in FINALIZED_STATES]
    revenue = sum((D(r["final_revenue"]) for r in fin), Decimal(0))
    fee = sum((D(r["final_fee"]) for r in fin), Decimal(0))
    ship = sum((D(r["final_shipping"]) for r in fin), Decimal(0))
    return {"total_rows": len(rows), "finalized": len(fin), "revenue": revenue, "fee": fee, "shipping": ship}


def tiktok_orders_in_csv_missing_from_db(date_str):
    orders_path = TT_DIR / f"orders_{date_str}.csv"
    if not orders_path.exists():
        return None
    with open(orders_path, newline="", encoding="utf-8") as f:
        csv_orders = list(csv.DictReader(f))
    ids = [r["order_id"] for r in csv_orders]
    status_by_id = {r["order_id"]: r["status"] for r in csv_orders}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT order_id FROM core.fact_order WHERE channel='TIKTOK' AND order_id = ANY(%s);", (ids,))
    db_ids = {r[0] for r in cur.fetchall()}
    conn.close()
    missing = [oid for oid in ids if oid not in db_ids]
    return {
        "csv_total": len(ids),
        "db_found": len(db_ids),
        "missing_count": len(missing),
        "missing_status_breakdown": dict(Counter(status_by_id[m] for m in missing)),
    }


# =====================================================================
# 3. Shopee Net Sales/GM1/CM1 — full-day reproduction from raw escrow
#    evidence (2026-09-08 only; the only date with complete per-order
#    field detail captured anywhere in this project).
# =====================================================================

def shopee_pnl_2026_09_08():
    with open(SHOPEE_ESCROW_98, encoding="utf-8") as f:
        escrow = json.load(f)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT order_id, order_status FROM core.fact_order WHERE channel='SHOPEE' AND business_date='2026-09-08';")
    status_by_id = dict(cur.fetchall())
    conn.close()

    if set(status_by_id) != set(escrow.keys()):
        raise AssertionError("Shopee 09-08 order population mismatch between fact_order and escrow evidence")

    gross = ds_huy = seller_disc = seller_vouch = refund = Decimal(0)
    commission = service = txn = shipping_net = ads_embed = escrow_total = Decimal(0)
    for oid, status in status_by_id.items():
        oi = escrow[oid]["response"]["order_income"]
        gross += D(oi["order_original_price"])
        escrow_total += D(oi["escrow_amount_after_adjustment"])
        if status == "CANCELLED":
            ds_huy += D(oi["order_original_price"])
            continue
        seller_disc += D(oi["seller_discount"])
        seller_vouch += D(oi["voucher_from_seller"])
        refund += D(oi["seller_return_refund"]) + D(oi["drc_adjustable_refund"]) + D(oi["seller_lost_compensation"])
        commission += D(oi["commission_fee"])
        service += D(oi["service_fee"])
        txn += D(oi["seller_transaction_fee"])
        shipping_net += D(oi["actual_shipping_fee"]) - D(oi["shopee_shipping_rebate"]) - D(oi["buyer_paid_shipping_fee"])
        ads_embed += D(oi["order_ams_commission_fee"]) + D(oi["ads_escrow_top_up_fee_or_technical_support_fee"])

    net_sales = gross - ds_huy - seller_disc - seller_vouch + refund
    cm1_proxy = net_sales - commission - service - txn - shipping_net
    cm2_proxy = cm1_proxy - ads_embed
    residual = cm2_proxy - escrow_total

    return {
        "orders": len(status_by_id), "gross": gross, "ds_huy": ds_huy, "seller_disc": seller_disc,
        "seller_vouch": seller_vouch, "refund": refund, "net_sales": net_sales,
        "commission": commission, "service": service, "txn": txn, "shipping_net": shipping_net,
        "cm1_proxy_pre_cogs": cm1_proxy, "ads_embed": ads_embed, "cm2_proxy_pre_cogs": cm2_proxy,
        "escrow_total": escrow_total, "residual_vs_escrow": residual,
    }
