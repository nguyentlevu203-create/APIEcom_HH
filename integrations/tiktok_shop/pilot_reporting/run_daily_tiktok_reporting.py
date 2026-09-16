#!/usr/bin/env python3
"""
TIKTOK PRODUCTION DAILY DATA PIPELINE V1 — single entrypoint.

    python3 run_daily_tiktok_reporting.py [--report-date YYYY-MM-DD]

report_date defaults to shop-local (Asia/Ho_Chi_Minh) D-1 if omitted.

Runs steps 01-17 in strict sequence (no ad-hoc parallelism — every step
that calls the API reuses collect.py's existing rate-limit-aware retry
logic, already proven across this project's prior runs). Does not modify
any already-PASS reconciliation logic (finance_lifecycle.py,
reconciliation_v21.py, statement_payment_reconciliation.py) — this script
only orchestrates and adds the daily-pipeline layer on top (products/
inventory collection, business mart, CEO report, data quality gate,
observability, the 4 separated readiness flags).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pilot_common import LOGS_DIR, NORMALIZED_DIR, REPORTS_DIR, PilotStopError, bootstrap_session, now_utc_iso

import collect
import finance_lifecycle
import statement_payment_reconciliation as spr
import reconciliation_v21
import backfill
import business_mart
import ceo_daily_report
import cogs_mapping

try:
    from zoneinfo import ZoneInfo
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover
    VN_TZ = None


def shop_local_d1() -> str:
    now = datetime.now(VN_TZ) if VN_TZ else datetime.utcnow()
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def today_str() -> str:
    now = datetime.now(VN_TZ) if VN_TZ else datetime.utcnow()
    return now.strftime("%Y-%m-%d")


class Observability:
    """Section 11 — one manifest per run. No secrets recorded (only
    counts/status; collect.py's own Logger already guarantees this at the
    call level via mask()/redact())."""

    def __init__(self, run_id: str, report_date: str):
        self.run_id = run_id
        self.report_date = report_date
        self.started_at = now_utc_iso()
        self.finished_at = None
        self.steps: list[dict[str, Any]] = []
        self.status = "RUNNING"

    def step(self, name: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.steps.append({
            "step": name, "status": status, "at": now_utc_iso(), "detail": detail or {},
        })

    def api_call_counts(self, log_path: Path) -> dict[str, int]:
        """Parses collect.py's own Logger output for this run — it never
        contains secrets, only method/domain/status lines."""
        calls = retries = code_429 = 0
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if " page " in line and ("rows=" in line or "code=" in line):
                    calls += 1
                if "transient code=" in line:
                    retries += 1
                if "36009002" in line:
                    code_429 += 1
        return {"api_calls": calls, "retries": retries, "rate_limit_hits_429": code_429}

    def finish(self, status: str) -> dict[str, Any]:
        self.finished_at = now_utc_iso()
        self.status = status
        return {
            "run_id": self.run_id, "report_date": self.report_date,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "status": self.status, "steps": self.steps,
        }


def run_pipeline(report_date: str, today: str) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    obs = Observability(run_id, report_date)
    log = collect.Logger(LOGS_DIR / f"daily_{report_date}_{run_id[:8]}.log")
    log.log(f"=== DAILY PIPELINE run_id={run_id} report_date={report_date} today={today} ===")

    domain_status: dict[str, str] = {}
    orders_result = None

    # ---- 01 Authorization/Token health ----
    try:
        session = bootstrap_session()
        obs.step("01_auth_token_health", "PASS", {"shop": session.shop_info.get("shop_name")})
    except PilotStopError as e:
        obs.step("01_auth_token_health", "FAIL", {"error": str(e)})
        manifest = obs.finish("FAIL")
        (REPORTS_DIR / f"run_manifest_{report_date}_{run_id[:8]}.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"run_id": run_id, "fatal": str(e), "manifest": manifest}

    # ---- 02-03 Orders incremental + detail ----
    try:
        orders_result = collect.collect_orders(log, session, report_date)
        collect.normalize_orders(report_date, orders_result["order_details"])
        domain_status["orders"] = orders_result["search_status"]
        domain_status["order_detail"] = orders_result["detail_status"]
        obs.step("02_03_orders", domain_status["orders"], {"order_count": len(orders_result["order_ids"])})
    except Exception as e:
        domain_status["orders"] = "FAIL"
        obs.step("02_03_orders", "FAIL", {"error": str(e)})

    # ---- 04 Returns ----
    try:
        domain_status["returns"] = collect.collect_returns(log, session, report_date)
        obs.step("04_returns", domain_status["returns"])
    except Exception as e:
        domain_status["returns"] = "FAIL"
        obs.step("04_returns", "FAIL", {"error": str(e)})

    # ---- 05 Products/Inventory ----
    try:
        domain_status["products"] = collect.collect_products(log, session, report_date)
        obs.step("05_products_inventory", domain_status["products"])
    except Exception as e:
        domain_status["products"] = "FAIL"
        obs.step("05_products_inventory", "FAIL", {"error": str(e)})

    # ---- 06 Affiliate ----
    try:
        domain_status["affiliate"] = collect.collect_affiliate(log, session, report_date)
        obs.step("06_affiliate", domain_status["affiliate"])
    except Exception as e:
        domain_status["affiliate"] = "FAIL"
        obs.step("06_affiliate", "FAIL", {"error": str(e)})

    # ---- 07-09 Analytics Product / Video / LIVE ----
    for step_name, fn in [
        ("07_analytics_product", collect.collect_product_analytics),
        ("08_analytics_video", collect.collect_video_analytics),
        ("09_analytics_live", collect.collect_live_analytics),
    ]:
        try:
            status = fn(log, session, report_date)
            domain_status[step_name] = status
            obs.step(step_name, status)
        except Exception as e:
            domain_status[step_name] = "FAIL"
            obs.step(step_name, "FAIL", {"error": str(e)})

    analytics_failed = any(domain_status.get(k) == "FAIL" for k in
                            ("07_analytics_product", "08_analytics_video", "09_analytics_live"))

    # ---- 10-12 Finance Unsettled / Statements / Transactions ----
    order_ids = orders_result["order_ids"] if orders_result else []
    try:
        finance_result = collect.collect_finance(log, session, report_date, order_ids)
        domain_status["finance"] = "PASS" if finance_result["statements_status"] in ("PASS", "PASS_EMPTY") else "FAIL"
        obs.step("10_12_finance", domain_status["finance"], {
            "statements": finance_result["statements_status"],
            "unsettled": finance_result["unsettled_status"],
        })
    except Exception as e:
        domain_status["finance"] = "FAIL"
        obs.step("10_12_finance", "FAIL", {"error": str(e)})

    summary_path = NORMALIZED_DIR / f"_collect_summary_{report_date}.json"
    summary_path.write_text(json.dumps({
        "report_date": report_date, "collected_at_utc": now_utc_iso(),
        "order_ids_count": len(order_ids), "domain_status": domain_status,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- 13 Rolling Backfill ----
    try:
        coverage = backfill.check_backfill_coverage(today)
        non_terminal = backfill.find_non_terminal_orders()
        recheck_results = backfill.recheck_orders(log, non_terminal)
        progressed = sum(1 for r in recheck_results if not r["still_non_terminal"])
        backfill_out = {
            "today": today, "backfill_dates_required": backfill.required_backfill_dates(today),
            "backfill_coverage": coverage, "backfill_mapping": "PASS" if all(coverage.values()) else "FAIL",
            "non_terminal_orders_checked": len(recheck_results), "progressed_to_settled": progressed,
        }
        (REPORTS_DIR / f"backfill_summary_{today}.json").write_text(
            json.dumps(backfill_out, indent=2, ensure_ascii=False), encoding="utf-8")
        obs.step("13_rolling_backfill", backfill_out["backfill_mapping"], backfill_out)
    except Exception as e:
        obs.step("13_rolling_backfill", "FAIL", {"error": str(e)})
        backfill_out = {"backfill_mapping": "FAIL", "error": str(e)}

    # ---- 14 Reconciliation ----
    try:
        lifecycle_path = finance_lifecycle.build_lifecycle(report_date)
        lifecycle_summary = finance_lifecycle.summarize(report_date, lifecycle_path)
        spr_rows = spr.build(report_date)
        spr_summary = spr.summarize(spr_rows)
        recon_text = reconciliation_v21.final_output(report_date, today)
        obs.step("14_reconciliation", lifecycle_summary["settlement_mapping"], {
            "statement_mapping": lifecycle_summary["statement_mapping"],
            "payment_mapping": lifecycle_summary["payment_mapping"],
        })
    except Exception as e:
        obs.step("14_reconciliation", "FAIL", {"error": str(e)})
        lifecycle_summary = {"settlement_mapping": "FAIL", "statement_mapping": "FAIL",
                              "payment_mapping": "UNRESOLVED_PLATFORM_SEMANTIC", "eligible": 0,
                              "settled": 0, "statement_issued": 0, "payment_pending": 0, "paid": 0,
                              "unknown": 0}
        spr_summary = {"payment_reconciliation_mapping": "FAIL"}

    # ---- 15 Business Mart ----
    try:
        mart = business_mart.build(report_date)
        obs.step("15_business_mart", "PASS")
    except Exception as e:
        obs.step("15_business_mart", "FAIL", {"error": str(e)})
        mart = None

    # ---- Readiness flags (Section 3, Section 12 failure policy) ----
    eligible = lifecycle_summary.get("eligible", 0)
    finalized = (lifecycle_summary.get("settled", 0) + lifecycle_summary.get("statement_issued", 0)
                 + lifecycle_summary.get("payment_pending", 0) + lifecycle_summary.get("paid", 0))
    coverage_pct = (finalized / eligible * 100) if eligible else 0

    if domain_status.get("orders") in ("FAIL", "FAIL_NETWORK", "FAIL_API"):
        commercial_reporting_readiness = "FAIL"
    elif analytics_failed:
        commercial_reporting_readiness = "AMBER"
    else:
        commercial_reporting_readiness = "READY"

    if lifecycle_summary.get("unknown", 0) > 0 or lifecycle_summary["settlement_mapping"] == "FAIL":
        finance_settlement_readiness = "FAIL"
    elif coverage_pct >= 100:
        finance_settlement_readiness = "READY"
    elif coverage_pct > 0:
        finance_settlement_readiness = "PARTIAL"
    else:
        finance_settlement_readiness = "WORKING"

    cash_reconciliation_readiness = "READY" if lifecycle_summary["payment_mapping"] == "PASS" else "UNRESOLVED"

    net_sales_status = reconciliation_v21.net_sales_semantic_mapping()["status"]
    if net_sales_status != "PASS":
        pnl_readiness = "NOT_READY"
    elif mart and mart["ECONOMICS"]["gross_margin_status"] == "READY":
        pnl_readiness = "READY"
    else:
        pnl_readiness = "PARTIAL"

    readiness = {
        "commercial_reporting_readiness": commercial_reporting_readiness,
        "finance_settlement_readiness": finance_settlement_readiness,
        "cash_reconciliation_readiness": cash_reconciliation_readiness,
        "pnl_readiness": pnl_readiness,
    }
    obs.step("readiness_computed", "PASS", readiness)

    # ---- 16 CEO Daily Output ----
    ceo_md_path = ceo_json_path = None
    if mart:
        try:
            ceo_md_path = ceo_daily_report.build_markdown(report_date, mart, readiness)
            ceo_json_path = ceo_daily_report.build_json(report_date, mart, readiness)
            obs.step("16_ceo_daily_output", "PASS", {"path": str(ceo_md_path)})
        except Exception as e:
            obs.step("16_ceo_daily_output", "FAIL", {"error": str(e)})
    else:
        obs.step("16_ceo_daily_output", "SKIPPED", {"reason": "business mart failed"})

    # ---- 17 Data Quality Gate ----
    dq_issues = []
    if orders_result:
        ids = orders_result["order_ids"]
        if len(ids) != len(set(ids)):
            dq_issues.append("duplicate order_id detected")
    if lifecycle_summary.get("unknown", 0) > 0:
        dq_issues.append(f"{lifecycle_summary['unknown']} UNKNOWN finance state(s)")
    secret_markers = ("access_token", "refresh_token", "app_secret")
    for p in NORMALIZED_DIR.glob(f"*_{report_date}.csv"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if any(m in text and "REDACTED" not in text for m in secret_markers):
            dq_issues.append(f"possible secret pattern in {p.name}")
    dq_status = "FAIL" if dq_issues else "PASS"
    obs.step("17_data_quality_gate", dq_status, {"issues": dq_issues})

    overall_status = "FAIL" if (dq_status == "FAIL" or commercial_reporting_readiness == "FAIL") else \
        ("AMBER" if commercial_reporting_readiness == "AMBER" else "PASS")

    manifest = obs.finish(overall_status)
    manifest["api_counts"] = obs.api_call_counts(log.path)
    manifest["reconciliation_coverage_pct"] = round(coverage_pct, 1)
    manifest["cogs_coverage_pct"] = mart["ECONOMICS"]["cogs_coverage_value_pct"] if mart else None
    manifest["readiness"] = readiness
    manifest_path = REPORTS_DIR / f"run_manifest_{report_date}_{run_id[:8]}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    log.log(f"=== DAILY PIPELINE done. status={overall_status} readiness={readiness} ===")

    return {
        "run_id": run_id, "report_date": report_date, "domain_status": domain_status,
        "lifecycle_summary": lifecycle_summary, "spr_summary": spr_summary, "mart": mart,
        "readiness": readiness, "manifest": manifest, "manifest_path": str(manifest_path),
        "ceo_md_path": str(ceo_md_path) if ceo_md_path else None,
        "overall_status": overall_status,
    }


def print_final_output(result: dict[str, Any]) -> None:
    r = result["readiness"]
    m = result["mart"]
    e = m["ECONOMICS"] if m else {}
    print("TIKTOK DAILY PRODUCTION V1")
    print("")
    print(f"Report date: {result['report_date']}")
    print(f"Run id: {result['run_id']}")
    print("")
    print(f"Commercial Reporting Readiness: {r['commercial_reporting_readiness']}")
    print(f"Finance Settlement Readiness: {r['finance_settlement_readiness']}")
    print(f"Cash Reconciliation Readiness: {r['cash_reconciliation_readiness']}")
    print(f"P&L Readiness: {r['pnl_readiness']}")
    print("")
    print(f"COGS: {'CONNECTED' if e.get('cogs_coverage_value_pct', 0) > 0 else 'NOT CONNECTED'} "
          f"({e.get('cogs_coverage_value_pct', 'N/A')}% value coverage)")
    print(f"GM: {e.get('gross_margin_status', 'N/A')}")
    print(f"CM1: {e.get('cm1_status', 'N/A')}")
    print(f"Ads: {e.get('ads_status', 'N/A')}")
    print(f"CM2: {e.get('cm2_final_status', 'N/A')}")
    print("")
    print(f"Overall run status: {result['overall_status']}")
    print(f"Manifest: {result['manifest_path']}")
    print(f"CEO report: {result['ceo_md_path']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-date", default=None)
    args = ap.parse_args()

    report_date = args.report_date or shop_local_d1()
    today = today_str()

    result = run_pipeline(report_date, today)
    if "fatal" in result:
        print(f"PIPELINE STOPPED: {result['fatal']}")
        return 1

    print_final_output(result)
    return 0 if result["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
