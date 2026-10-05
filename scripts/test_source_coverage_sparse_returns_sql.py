"""
P11 RECOVERY D — sql/065_STAGED_source_coverage_sparse_returns_watermark.sql.

Static checks (always) plus behavior checks that create both the 064 and
065 view definitions on a throwaway local PostgreSQL cluster with stub
tables (skipped when no local PostgreSQL binaries are available). Never
touches Neon/production.

Run with: python3 -m pytest scripts/test_source_coverage_sparse_returns_sql.py -v
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
V064 = (SQL_DIR / "064_STAGED_source_coverage_watermark_lag.sql").read_text(encoding="utf-8")
V065 = (SQL_DIR / "065_STAGED_source_coverage_sparse_returns_watermark.sql").read_text(encoding="utf-8")
HEALTHCHECK_SRC = (ROOT / "scripts" / "production_healthcheck.py").read_text(encoding="utf-8")
PG_BIN = next((Path(p) for p in ("/Library/PostgreSQL/18/bin", "/usr/lib/postgresql/18/bin",
                                 "/usr/lib/postgresql/16/bin") if (Path(p) / "initdb").exists()), None)


def _case(sql):
    return re.search(r"CASE\n(.*?)END AS coverage_status", sql, re.DOTALL).group(1)


def _columns(sql):
    select = sql[sql.index("SELECT dm.source_system AS platform"):sql.index("FROM domains dm")]
    select = re.sub(r"CASE\n.*?END AS coverage_status", "X AS coverage_status", select, flags=re.DOTALL)
    return re.findall(r"\bAS\s+(\w+)\s*(?:,|\n\s*FROM|$)", select)


# --- static ---------------------------------------------------------------------

def test_columns_and_vocabulary_unchanged_vs_064():
    assert _columns(V065) == _columns(V064)
    vocab = lambda sql: set(re.findall(r"(?:THEN|ELSE) '([A-Z_]+)'", _case(sql)))
    assert vocab(V065) == vocab(V064)


def test_only_tiktok_returns_is_sparse():
    block = re.search(r"sparse_sources AS \((.*?)\), domain_dates", V065, re.DOTALL).group(1)
    assert re.findall(r"\('(\w+)','(\w+)'\)", block) == [("TIKTOK", "returns")]


def test_staged_header_and_rollback_documented():
    assert "STAGED: not applied" in V065 and "Rollback = re-run sql/064" in V065
    assert V065.count("CREATE OR REPLACE VIEW mart.v_ai_source_coverage AS") == 1


def test_healthcheck_required_classification_unchanged():
    required = ast.literal_eval(re.search(r"REQUIRED_SOURCES = (\{.*?\})", HEALTHCHECK_SRC, re.DOTALL).group(1))
    optional = ast.literal_eval(re.search(r"OPTIONAL_SOURCES = (\{.*?\})", HEALTHCHECK_SRC, re.DOTALL).group(1))
    assert ("TIKTOK", "returns") in required and ("TIKTOK", "returns") not in optional


# --- behavior on a throwaway local cluster ---------------------------------------

STUB = """
CREATE SCHEMA core; CREATE SCHEMA control; CREATE SCHEMA mart;
CREATE TABLE core.fact_order (channel text, business_date date);
CREATE TABLE core.fact_return_refund (channel text, business_date date);
CREATE TABLE core.fact_settlement (channel text, business_date date);
CREATE TABLE core.fact_ads_daily (channel text, business_date date);
CREATE TABLE core.fact_affiliate_daily (channel text, business_date date);
CREATE TABLE core.fact_product_analytics_daily (channel text, business_date date);
CREATE TABLE core.fact_live_daily (channel text, business_date date);
CREATE TABLE core.fact_shopee_affiliate_conversion (business_date date);
CREATE TABLE core.fact_shop_traffic_daily (channel text, business_date date);
CREATE TABLE core.fact_inventory_snapshot (channel text, business_date date);
CREATE TABLE control.etl_run_log (source_system text, source_endpoint text, run_type text, status text,
    error_message text, started_at timestamptz);
CREATE TABLE control.etl_sync_state (source_system text, source_endpoint text, last_synced_at timestamptz,
    status text);
"""
DATE_TABLE = {
    ("SHOPEE", "orders"): "core.fact_order", ("TIKTOK", "orders"): "core.fact_order",
    ("SHOPEE", "returns"): "core.fact_return_refund", ("TIKTOK", "returns"): "core.fact_return_refund",
    ("SHOPEE", "finance"): "core.fact_settlement", ("TIKTOK", "finance"): "core.fact_settlement",
    ("SHOPEE", "ads"): "core.fact_ads_daily", ("TIKTOK", "affiliate"): "core.fact_affiliate_daily",
    ("TIKTOK", "product_analytics"): "core.fact_product_analytics_daily", ("TIKTOK", "live"): "core.fact_live_daily",
    ("TIKTOK", "shop_traffic"): "core.fact_shop_traffic_daily",
    ("SHOPEE", "product_inventory"): "core.fact_inventory_snapshot",
    ("TIKTOK", "product_inventory"): "core.fact_inventory_snapshot",
}


def _view_as(sql, name):
    return sql[sql.index("CREATE OR REPLACE VIEW"):].replace("mart.v_ai_source_coverage", f"mart.{name}", 1)


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
        cur.execute(_view_as(V064, "cov_064"))
        cur.execute(_view_as(V065, "cov_065"))
        yield cur
        conn.close()
    finally:
        subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(data, ignore_errors=True)


def _seed(cur, *, returns_days_ago=8, returns_watermark_hours=1, returns_last_run="success",
          orders_days_ago=0, orders_watermark_hours=1):
    """Every source: events today + fresh watermark + successful incremental,
    except the knobs for TIKTOK returns / TIKTOK orders. A days_ago of None
    means zero event rows all-time; a watermark of None means no
    etl_sync_state row; a last_run of None means no incremental run row."""
    cur.execute("TRUNCATE " + ", ".join(sorted(set(DATE_TABLE.values()) | {
        "core.fact_shopee_affiliate_conversion", "control.etl_run_log", "control.etl_sync_state"})))
    cur.execute("INSERT INTO core.fact_shopee_affiliate_conversion VALUES (CURRENT_DATE)")
    sources = list(DATE_TABLE) + [("SHOPEE", "affiliate_ams")]
    for system, endpoint in sources:
        days, wm_hours, last_run = 0, 1, "success"
        if (system, endpoint) == ("TIKTOK", "returns"):
            days, wm_hours, last_run = returns_days_ago, returns_watermark_hours, returns_last_run
        if (system, endpoint) == ("TIKTOK", "orders"):
            days, wm_hours = orders_days_ago, orders_watermark_hours
        if (system, endpoint) in DATE_TABLE and days is not None:
            cur.execute(f"INSERT INTO {DATE_TABLE[(system, endpoint)]} (channel, business_date) "
                        f"VALUES (%s, CURRENT_DATE - %s)", (system, days))
        if wm_hours is not None:
            cur.execute("INSERT INTO control.etl_sync_state VALUES (%s,%s, now() - make_interval(hours => %s), 'success')",
                        (system, endpoint, wm_hours))
        if last_run is not None:
            cur.execute("INSERT INTO control.etl_run_log VALUES (%s,%s,'incremental',%s,NULL, now() - interval '10 minutes')",
                        (system, endpoint, last_run))


def _status(cur, view):
    cur.execute(f"SELECT platform, source_name, coverage_status FROM mart.{view}")
    return {(p, s): st for p, s, st in cur.fetchall()}


def test_sparse_returns_current_watermark_old_event_is_current(pg):
    _seed(pg, returns_days_ago=8, returns_watermark_hours=1)
    assert _status(pg, "cov_064")[("TIKTOK", "returns")] == "STALE"   # the live false RED
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "CURRENT"
    pg.execute("SELECT latest_db_date = CURRENT_DATE - 8 FROM mart.cov_065 WHERE platform='TIKTOK' AND source_name='returns'")
    assert pg.fetchone()[0] is True  # event freshness still reported, informational


def test_sparse_returns_stale_watermark_is_stale(pg):
    _seed(pg, returns_days_ago=8, returns_watermark_hours=80)
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "STALE"


def test_sparse_returns_watermark_over_24h_is_lagging(pg):
    _seed(pg, returns_days_ago=8, returns_watermark_hours=30)
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "SOURCE_LAGGING"


def test_sparse_returns_recent_failed_run_is_not_current(pg):
    _seed(pg, returns_days_ago=8, returns_watermark_hours=1, returns_last_run="fail")
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "LAST_RUN_FAILED"


# --- zero events all-time (review fix) ---

def test_a_zero_events_current_watermark_successful_run_is_current(pg):
    _seed(pg, returns_days_ago=None, returns_watermark_hours=1, returns_last_run="success")
    assert _status(pg, "cov_064")[("TIKTOK", "returns")] == "NO_DATA"   # the reviewed bug
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "CURRENT"
    pg.execute("SELECT latest_db_date FROM mart.cov_065 WHERE platform='TIKTOK' AND source_name='returns'")
    assert pg.fetchone()[0] is None  # still informational, still NULL


def test_b_zero_events_no_watermark_is_no_data(pg):
    _seed(pg, returns_days_ago=None, returns_watermark_hours=None, returns_last_run=None)
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "NO_DATA"


def test_c_zero_events_failed_latest_run_is_never_current(pg):
    _seed(pg, returns_days_ago=None, returns_watermark_hours=1, returns_last_run="fail")
    status = _status(pg, "cov_065")[("TIKTOK", "returns")]
    assert status == "API_ERROR"  # 064 precedence: recent failed run with zero data
    _seed(pg, returns_days_ago=None, returns_watermark_hours=1, returns_last_run="success")
    pg.execute("INSERT INTO control.etl_run_log VALUES ('TIKTOK','returns','incremental','fail',NULL, now() - interval '4 days')")
    pg.execute("UPDATE control.etl_run_log SET started_at = now() - interval '5 days' "
               "WHERE source_system='TIKTOK' AND source_endpoint='returns' AND status='success'")
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "LAST_RUN_FAILED"  # older than recent_run's 3 days


def test_d_zero_events_watermark_over_72h_is_stale(pg):
    _seed(pg, returns_days_ago=None, returns_watermark_hours=80)
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "STALE"
    _seed(pg, returns_days_ago=None, returns_watermark_hours=30)
    assert _status(pg, "cov_065")[("TIKTOK", "returns")] == "SOURCE_LAGGING"


@pytest.mark.parametrize("orders_days_ago,orders_watermark_hours", [
    (0, 1), (5, 1), (0, 30), (0, 80), (2, 1), (None, 1), (None, None), (0, None)])
def test_dense_sources_identical_to_064(pg, orders_days_ago, orders_watermark_hours):
    _seed(pg, returns_days_ago=0, orders_days_ago=orders_days_ago, orders_watermark_hours=orders_watermark_hours)
    old, new = _status(pg, "cov_064"), _status(pg, "cov_065")
    assert {k: v for k, v in new.items() if k != ("TIKTOK", "returns")} == \
           {k: v for k, v in old.items() if k != ("TIKTOK", "returns")}
    if orders_days_ago == 5:
        assert new[("TIKTOK", "orders")] == "STALE"  # dense date rule still applies
    if orders_days_ago is None:
        assert new[("TIKTOK", "orders")] == "NO_DATA"  # dense zero-event rule unchanged, watermark or not
