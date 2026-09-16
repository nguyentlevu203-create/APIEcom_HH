#!/usr/bin/env python3
"""P6B.3 — backfill TikTok SKU-level fee detail for the already-tested
2026-09-01..2026-09-11 window.

Does NOT rediscover the order population (already correct since
P6B.1) — reuses the exact order_ids already confirmed MATCHED in
core.fact_settlement for that date range. Calls the SAME endpoint
(finance_order_statement_transactions) already in production use, this
time also parsing sku_transactions[].fee_tax_breakdown.fee.* (proven
real in P6B.2). Does not touch control.etl_sync_state.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PILOT_DIR = Path(__file__).resolve().parent
PIPELINES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pipelines"
sys.path.insert(0, str(PIPELINES_DIR))
sys.path.insert(0, str(PILOT_DIR))

import pilot_common  # noqa: E402
import incr_common as ic  # noqa: E402

WINDOW_FROM, WINDOW_TO = "2026-09-01", "2026-09-11"


def simple_log(prefix):
    def _log(msg):
        print(f"[{prefix}] {msg}", flush=True)
    return _log


def main():
    log = simple_log("P6B3_SKU_FEE")
    conn = ic.get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fo.order_id FROM core.fact_order fo
        JOIN core.fact_settlement fs ON fs.channel=fo.channel AND fs.shop_id=fo.shop_id AND fs.order_id=fo.order_id
        WHERE fo.channel='TIKTOK' AND fs.settlement_type='MATCHED'
          AND fo.business_date BETWEEN %s AND %s;
        """,
        (WINDOW_FROM, WINDOW_TO),
    )
    order_ids = [r[0] for r in cur.fetchall()]
    log(f"{len(order_ids)} already-MATCHED TikTok orders in {WINDOW_FROM}..{WINDOW_TO}")

    shop_id = None
    session = pilot_common.bootstrap_session()
    shop_id = session.shop_info.get("shop_id")

    etl_run_id = ic.start_run_log(cur, "TIKTOK", "sku_fee_backfill_p6b3", shop_id, run_type="historical_backfill_p6b3")
    conn.commit()

    sku_ctr = ic.Counters()
    order_no_sku, order_errors = 0, []
    for i, oid in enumerate(order_ids, start=1):
        def call(oid=oid):
            resp = session.client.read_domain(
                "finance_order_statement_transactions", session.access_token,
                shop_cipher=session.shop_info.get("shop_cipher"), path_params={"order_id": oid},
            )
            if not resp.ok:
                raise ic.TransientHTTPError(f"code={resp.code} message={resp.message}")
            return resp
        try:
            resp = ic.with_backoff(call, f"tiktok p6b3 finance {oid}", log)
        except Exception as e:  # noqa: BLE001
            order_errors.append({"order_id": oid, "error": str(e)})
            time.sleep(0.3)
            continue

        d = resp.data
        sku_txs = d.get("sku_transactions", [])
        if not sku_txs:
            order_no_sku += 1
            continue
        order_bd = ic.vn_date(ic.ts_from_epoch(d.get("order_create_time"))) if d.get("order_create_time") else None
        currency = d.get("currency")
        for tx in sku_txs:
            sku_id = tx.get("sku_id")
            if not sku_id:
                continue
            fee_tax = tx.get("fee_tax_breakdown", {}).get("fee", {})
            cur.execute(
                """
                INSERT INTO core.fact_settlement_sku_fee (
                    channel, shop_id, order_id, sku_id, statement_id, business_date,
                    revenue_amount, fixed_fee, payment_fee, vxp_fee, infrastructure_fee, affiliate_fee,
                    currency, finance_state, value_basis, source_system, source_record_id,
                    source_endpoint, source_updated_at, etl_run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'MATCHED','API_ACTUAL','TIKTOK',%s,
                    '/finance/202501/orders/{order_id}/statement_transactions (p6b3_sku_fee_backfill)', now(), %s)
                ON CONFLICT (channel, shop_id, order_id, sku_id) DO UPDATE SET
                    statement_id = EXCLUDED.statement_id, business_date = EXCLUDED.business_date,
                    revenue_amount = EXCLUDED.revenue_amount, fixed_fee = EXCLUDED.fixed_fee,
                    payment_fee = EXCLUDED.payment_fee, vxp_fee = EXCLUDED.vxp_fee,
                    infrastructure_fee = EXCLUDED.infrastructure_fee, affiliate_fee = EXCLUDED.affiliate_fee,
                    currency = EXCLUDED.currency, source_updated_at = now(), etl_run_id = EXCLUDED.etl_run_id
                RETURNING (xmax = 0) AS inserted;
                """,
                (
                    "TIKTOK", shop_id, oid, sku_id, tx.get("statement_id"), order_bd,
                    ic.dec(tx.get("revenue_amount")), ic.dec(fee_tax.get("platform_commission_amount")),
                    ic.dec(fee_tax.get("transaction_fee_amount")), ic.dec(fee_tax.get("voucher_xtra_service_fee_amount")),
                    ic.dec(fee_tax.get("vn_fix_infrastructure_fee")), ic.dec(fee_tax.get("affiliate_commission_amount")),
                    currency, f"{oid}:{sku_id}", etl_run_id,
                ),
            )
            sku_ctr.record(cur.fetchone()[0])
        if i % 50 == 0:
            conn.commit()
            log(f"progress {i}/{len(order_ids)} (committed)")
        time.sleep(0.15)

    conn.commit()
    ic.finish_run_log("TIKTOK", "sku_fee_backfill_p6b3", etl_run_id, "success",
                       rows_processed=sku_ctr.inserted + sku_ctr.updated)
    conn.close()

    result = {
        "orders_targeted": len(order_ids), "orders_no_sku_transactions": order_no_sku,
        "orders_failed": len(order_errors), "sku_fee_rows": sku_ctr.as_dict(),
        "failed_detail": order_errors[:10],
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
