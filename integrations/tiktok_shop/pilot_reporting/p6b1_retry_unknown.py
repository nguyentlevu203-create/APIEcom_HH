#!/usr/bin/env python3
"""P6B.1 — retry TikTok settlement rows stuck at settlement_type='UNKNOWN'.

Root cause confirmed live: the bulk historical backfill's finance calls
were not treated as retryable when TikTok returned code=36009002 ("Too
many requests. A dependent service is temporarily rate limited.") —
with_backoff only retries on network exceptions, and the backfill script
returned a non-ok response object without raising, so it was recorded as
a hard UNKNOWN instead of being retried. Confirmed transient: re-querying
one such order live immediately succeeded. This script re-fetches every
currently-UNKNOWN TikTok settlement row in the P6B.1 window with a slower
pace and real retry-on-rate-limit.
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


def simple_log(prefix):
    def _log(msg):
        print(f"[{prefix}] {msg}", flush=True)
    return _log


def main():
    log = simple_log("P6B1_RETRY")
    conn = ic.get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id FROM core.fact_settlement WHERE channel='TIKTOK' AND settlement_type='UNKNOWN';"
    )
    order_ids = [r[0] for r in cur.fetchall()]
    log(f"{len(order_ids)} orders currently UNKNOWN")

    session = pilot_common.bootstrap_session()
    ctr = ic.Counters()
    still_unknown = []
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
            resp = ic.with_backoff(call, f"tiktok retry {oid}", log)
        except Exception as e:  # noqa: BLE001
            log(f"{oid}: still failing after retries: {e}")
            still_unknown.append(oid)
            time.sleep(0.5)
            continue

        has_real = bool(resp.data.get("sku_transactions"))
        if has_real:
            d = resp.data
            status, settlement_amount = "MATCHED", ic.dec(d.get("settlement_amount"))
            revenue, fee, ship, currency = (
                ic.dec(d.get("revenue_amount")), ic.dec(d.get("fee_and_tax_amount")),
                ic.dec(d.get("shipping_cost_amount")), d.get("currency"),
            )
        else:
            status, settlement_amount, revenue, fee, ship, currency = "NOT_SETTLED_YET", None, None, None, None, None

        cur.execute(
            """UPDATE core.fact_settlement SET settlement_amount=%s, settlement_type=%s,
                   currency=%s, revenue_amount=%s, fee_and_tax_amount=%s, shipping_cost_amount=%s
               WHERE channel='TIKTOK' AND settlement_id=%s;""",
            (settlement_amount, status, currency, revenue, fee, ship, oid),
        )
        ctr.record((True,))
        if i % 30 == 0:
            conn.commit()
            log(f"retried {i}/{len(order_ids)}")
        time.sleep(0.4)  # slower pace than the bulk backfill to avoid re-triggering the rate limit

    conn.commit()
    conn.close()
    result = {"retried": len(order_ids), "resolved": len(order_ids) - len(still_unknown),
              "still_unknown": still_unknown}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
