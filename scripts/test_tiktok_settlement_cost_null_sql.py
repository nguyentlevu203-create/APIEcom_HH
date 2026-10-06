"""
P14-B D2 — sql/066_STAGED_tiktok_settlement_cost_null_semantics.sql.

Static checks (always) plus behavior checks that create both the sql/053
(current production) and sql/066 definitions of mart.v_ceo_ecom_daily on a
throwaway local PostgreSQL cluster with stub tables (skipped when no local
PostgreSQL binaries are available). Never touches Neon/production.

Run with: python3 -m pytest scripts/test_tiktok_settlement_cost_null_sql.py -v
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
V053 = (SQL_DIR / "053_p8_5_profit_status_precision.sql").read_text(encoding="utf-8")
V066 = (SQL_DIR / "066_STAGED_tiktok_settlement_cost_null_semantics.sql").read_text(encoding="utf-8")
PG_BIN = next((Path(p) for p in ("/Library/PostgreSQL/18/bin", "/usr/lib/postgresql/18/bin",
                                 "/usr/lib/postgresql/16/bin") if (Path(p) / "initdb").exists()), None)

FEE_COLUMNS = ["fixed_fee", "payment_fee", "vxp_fee", "infrastructure_fee", "affiliate_fee"]


def _body(sql):
    return sql[sql.index("CREATE OR REPLACE VIEW mart.v_ceo_ecom_daily AS"):]


# --- static -------------------------------------------------------------------

def test_066_is_staged_and_names_rollback():
    head = V066[:V066.index("CREATE OR REPLACE VIEW mart.v_ceo_ecom_daily AS")]
    assert "STAGED: not applied" in head
    assert "Rollback = re-run sql/053_p8_5_profit_status_precision.sql" in head


def test_066_changes_only_the_five_tiktok_fee_expressions():
    old = _body(V053).splitlines()
    new = _body(V066).splitlines()
    assert len(old) == len(new)
    changed = [(a, b) for a, b in zip(old, new) if a != b]
    assert sorted(re.search(r"END AS (\w+),", b).group(1) for _, b in changed) == sorted(FEE_COLUMNS)
    for a, b in changed:
        assert "inc.is_incomplete THEN NULL::numeric" in b
        # every original branch is still there, only the guard was inserted
        assert a.replace("CASE ", "").split(" END AS")[0].split("THEN ", 1)[1].split(" ")[0] in b


# --- behavior on a throwaway local cluster ---------------------------------------

STUB = """
CREATE SCHEMA core; CREATE SCHEMA mart;
CREATE TABLE mart.gold_channel_daily (business_date date, channel text, shop_id text, metric_name text,
    metric_value numeric(24,12), coverage_status text, updated_at timestamptz);
CREATE TABLE core.fact_order (channel text, shop_id text, order_id text, order_status text, business_date date);
CREATE TABLE core.fact_settlement (channel text, shop_id text, order_id text, settlement_type text,
    ingested_at timestamptz, commission_fee numeric(18,4), service_fee numeric(18,4),
    seller_transaction_fee numeric(18,4), actual_shipping_fee numeric(18,4), shopee_shipping_rebate numeric(18,4),
    buyer_paid_shipping_fee numeric(18,4), seller_return_refund numeric(18,4), drc_adjustable_refund numeric(18,4),
    seller_lost_compensation numeric(18,4));
"""

LAG, ZERO, POS, SHOPEE_DAY = "2026-10-04", "2026-10-01", "2026-10-02", "2026-10-03"


def _view_as(sql, name):
    return _body(sql).replace("mart.v_ceo_ecom_daily", f"mart.{name}", 1)


def _gold(cur, day, channel, metrics):
    for name, (value, status) in metrics.items():
        cur.execute("INSERT INTO mart.gold_channel_daily VALUES (%s,%s,'s1',%s,%s,%s,now())",
                    (day, channel, name, value, status))


def _tiktok_day(cur, day, *, settled, fee):
    """settled=False: 5 orders, all IN_TRANSIT -> eligible_orders=0 -> SOURCE_LAGGING.
    settled=True: 5 COMPLETED orders all MATCHED -> COMPLETE_SETTLEMENT_COVERAGE."""
    for i in range(5):
        oid = f"{day}-{i}"
        cur.execute("INSERT INTO core.fact_order VALUES ('TIKTOK','s1',%s,%s,%s)",
                    (oid, "COMPLETED" if settled else "IN_TRANSIT", day))
        cur.execute("INSERT INTO core.fact_settlement (channel, shop_id, order_id, settlement_type, ingested_at) "
                    "VALUES ('TIKTOK','s1',%s,%s,now())", (oid, "MATCHED" if settled else "NOT_SETTLED_YET"))
    metrics = {"orders": (5, "READY"), "net_sales": (0 if not settled else 1000, "DERIVABLE"),
               "gm1": (10, "READY"), "cm1": (5, "READY")}
    for m in ("tiktok_fixed_fee_actual", "tiktok_payment_fee_actual", "tiktok_vxp_fee_actual",
              "tiktok_infrastructure_fee_actual", "tiktok_affiliate_fee_actual"):
        metrics[m] = (fee, "READY")
    _gold(cur, day, "TIKTOK", metrics)


@pytest.fixture(scope="module")
def pg():
    if PG_BIN is None:
        pytest.skip("no local PostgreSQL binaries")
    psycopg2 = pytest.importorskip("psycopg2")
    data = Path(tempfile.mkdtemp(prefix="hhpg"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run([str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                   check=True, capture_output=True)
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-l", str(data / "log"), "-o",
                    f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''", "start"],
                   check=True, capture_output=True)
    try:
        conn = psycopg2.connect(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(STUB)
        cur.execute(_view_as(V053, "ceo_053"))
        cur.execute(_view_as(V066, "ceo_066"))
        _tiktok_day(cur, LAG, settled=False, fee=0)
        _tiktok_day(cur, ZERO, settled=True, fee=0)
        _tiktok_day(cur, POS, settled=True, fee=1234)
        for i in range(3):
            cur.execute("INSERT INTO core.fact_order VALUES ('SHOPEE','s1',%s,'COMPLETED',%s)", (f"sp{i}", SHOPEE_DAY))
            cur.execute("INSERT INTO core.fact_settlement VALUES ('SHOPEE','s1',%s,'escrow_estimate',now(),"
                        "-100,-20,-30,10,5,1,0,0,0)", (f"sp{i}",))
        _gold(cur, SHOPEE_DAY, "SHOPEE", {"orders": (3, "READY"), "net_sales": (900, "READY"), "gm1": (400, "READY")})
        yield cur
        conn.close()
    finally:
        subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(data, ignore_errors=True)


def _row(cur, view, day, channel):
    cur.execute(f"SELECT * FROM mart.{view} WHERE business_date = %s AND channel = %s", (day, channel))
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


def test_column_contract_unchanged(pg):
    def cols(view):
        pg.execute("SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                   "WHERE a.attrelid = %s::regclass AND a.attnum > 0 ORDER BY a.attnum", (f"mart.{view}",))
        return pg.fetchall()
    assert cols("ceo_053") == cols("ceo_066")
    assert len(cols("ceo_066")) == 74


def test_settlement_lagging_fee_is_null_not_zero(pg):
    old, new = _row(pg, "ceo_053", LAG, "TIKTOK"), _row(pg, "ceo_066", LAG, "TIKTOK")
    assert new["net_sales_status"] == "SOURCE_LAGGING" and new["net_sales"] is None
    for c in FEE_COLUMNS:
        assert old[c] == 0          # the reviewed defect: unproven zero
        assert new[c] is None


def test_complete_settlement_actual_zero_stays_zero(pg):
    new = _row(pg, "ceo_066", ZERO, "TIKTOK")
    assert new["net_sales_status"] == "COMPLETE_SETTLEMENT_COVERAGE"
    for c in FEE_COLUMNS:
        assert new[c] == 0


def test_complete_settlement_positive_fee_unchanged(pg):
    old, new = _row(pg, "ceo_053", POS, "TIKTOK"), _row(pg, "ceo_066", POS, "TIKTOK")
    for c in FEE_COLUMNS:
        assert new[c] == old[c] == 1234


def test_every_non_fee_column_identical(pg):
    for day in (LAG, ZERO, POS):
        old, new = _row(pg, "ceo_053", day, "TIKTOK"), _row(pg, "ceo_066", day, "TIKTOK")
        assert {k: v for k, v in old.items() if k not in FEE_COLUMNS} == {k: v for k, v in new.items() if k not in FEE_COLUMNS}


def test_shopee_rows_identical(pg):
    old, new = _row(pg, "ceo_053", SHOPEE_DAY, "SHOPEE"), _row(pg, "ceo_066", SHOPEE_DAY, "SHOPEE")
    assert old == new
    assert new["fixed_fee"] == -300 and new["payment_fee"] == -90
