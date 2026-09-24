"""
P11-FIX-4 — static checks on sql/064_STAGED_source_coverage_watermark_lag.sql
(no DB). The view must not report CURRENT while an incremental watermark
lags or the last incremental run ended PARTIAL_CATCHUP, and must keep
049's exact column list (CREATE OR REPLACE VIEW cannot change it).

Run with: python3 -m pytest scripts/test_source_coverage_watermark_sql.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
NEW = (SQL_DIR / "064_STAGED_source_coverage_watermark_lag.sql").read_text(encoding="utf-8")
OLD = (SQL_DIR / "049_p8_5_source_coverage_semantics_fix.sql").read_text(encoding="utf-8")


def _case_block(sql: str) -> str:
    return re.search(r"CASE\n(.*?)END AS coverage_status", sql, re.DOTALL).group(1)


def _output_columns(sql: str) -> list[str]:
    select = sql[sql.index("SELECT dm.source_system AS platform"):sql.index("FROM domains dm")]
    select = re.sub(r"CASE\n.*?END AS coverage_status", "X AS coverage_status", select, flags=re.DOTALL)
    return re.findall(r"\bAS\s+(\w+)\s*(?:,|\n\s*FROM|$)", select)


def test_column_list_identical_to_049():
    assert _output_columns(NEW) == _output_columns(OLD)
    assert "coverage_status" in _output_columns(NEW)


def test_no_new_status_vocabulary():
    old = set(re.findall(r"THEN '([A-Z_]+)'", _case_block(OLD))) | {"STALE", "SOURCE_LAGGING", "CURRENT"}
    new = set(re.findall(r"THEN '([A-Z_]+)'", _case_block(NEW))) | set(re.findall(r"ELSE '([A-Z_]+)'", _case_block(NEW)))
    assert new <= old


def test_watermark_and_partial_rules_come_before_current():
    case = _case_block(NEW)
    current = case.index("THEN 'CURRENT'\n        ELSE")
    for rule in ("INTERVAL '72 hours' THEN 'STALE'",
                 "LIKE 'PARTIAL_CATCHUP%' THEN 'SOURCE_LAGGING'",
                 "INTERVAL '24 hours' THEN 'SOURCE_LAGGING'"):
        assert case.index(rule) < current, rule
    # date-based STALE is still checked (and before the softer rules)
    assert case.index("(CURRENT_DATE - c.latest_db_date) > 3 THEN 'STALE'") < case.index("PARTIAL_CATCHUP")


def test_partial_catchup_read_from_latest_incremental_run_only():
    assert re.search(r"last_incremental AS \(.*?WHERE run_type = 'incremental'", NEW, re.DOTALL)
    assert "li.error_message LIKE 'PARTIAL_CATCHUP%'" in NEW
    assert "LEFT JOIN last_incremental li" in NEW


def test_affiliate_ams_day_grain_watermark_excluded():
    case = _case_block(NEW)
    for line in case.splitlines():
        if "s.last_synced_at <" in line:
            assert "dm.source_endpoint <> 'affiliate_ams'" in line


def test_staged_not_auto_applied():
    assert "STAGED" in NEW.splitlines()[1]
