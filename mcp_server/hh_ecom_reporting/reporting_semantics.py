"""
P14-B — status/NULL semantics shared with the Worker
(workers/hh-ecom-reporting-mcp/src/semantics.ts). Same rules, same status
vocabulary; only the fields this P8.0 reference exposes are ported (it has
no period_kpis and no product_funnel, so D1/D5 do not apply here).

Doctrine: NULL != 0, UNKNOWN != ZERO, PARTIAL != COMPLETE,
SOURCE_LAGGING != READY, no events != zero events unless source
currentness proves it.
"""
from __future__ import annotations

from typing import Optional

SETTLEMENT_LAG_STATUSES = {"SOURCE_LAGGING", "NOT_SETTLED_YET", "NO_ELIGIBLE_ORDERS"}

# mart.v_ceo_ecom_daily columns whose TikTok value is -SUM(core.fact_settlement_sku_fee.<fee>)
TIKTOK_SETTLEMENT_FEE_COLUMNS = {"fixed_fee", "payment_fee", "vxp_fee", "infrastructure_fee", "affiliate_fee"}

KNOWN_MISSING_COST_STATUS = {
    "ads_spend": "SEPARATE_API_REQUIRED",
    "booking_kol_koc": "MISSING_SOURCE",
    "live_inhouse_cost": "MISSING_SOURCE",
}


def cost_line(channel, column: str, value, net_sales_status) -> tuple:
    """D2 — (amount, status). TikTok settlement-derived fees follow that date's
    settlement completeness; an unsettled date never shows an unproven 0."""
    if channel == "TIKTOK" and column in TIKTOK_SETTLEMENT_FEE_COLUMNS:
        s = None if net_sales_status is None else str(net_sales_status)
        if s in SETTLEMENT_LAG_STATUSES:
            return None, s
        if value is None:
            return None, "MISSING_SOURCE"
        if s == "COMPLETE_SETTLEMENT_COVERAGE":
            return value, "READY"
        if s == "PARTIAL_SETTLEMENT_COVERAGE":
            return value, "PARTIAL_SETTLEMENT_COVERAGE"
        return value, "SETTLEMENT_COVERAGE_UNKNOWN"
    if value is not None:
        return value, "READY"
    return None, KNOWN_MISSING_COST_STATUS.get(column, "MISSING_SOURCE")


def cogs_envelope(total_cogs, sellable_cogs, promo_gift_cost, sellable_status, promo_status) -> dict:
    """D3 — completeness from Gold's coverage_status of sellable_cogs and
    promo_gift_cost (mart.v_ai_metric_status); a known partial amount is kept."""
    statuses = [None if s is None else str(s) for s in (sellable_status, promo_status)]
    first_not_ready = next((s for s in statuses if s is not None and s != "READY"), None)
    if sellable_cogs is None and promo_gift_cost is None:
        return {"value": None, "status": first_not_ready or "MISSING_SOURCE"}
    if "COGS_INCOMPLETE" in statuses:
        return {"value": total_cogs, "status": "COGS_INCOMPLETE"}
    if all(s == "READY" for s in statuses):
        return {"value": total_cogs, "status": "READY"}
    return {"value": total_cogs, "status": first_not_ready or "MISSING_SOURCE"}


COVERAGE_START_BASIS = "FIRST_LOADED_BUSINESS_DATE_CONSERVATIVE_BOUND"

_NOT_ZERO = "An empty result for this period is NOT zero activity."


def freshness_envelope(from_date: str, to_date: str, row_count: int, first_loaded_date: Optional[str],
                       last_loaded_date: Optional[str], coverage: Optional[dict], source: str) -> dict:
    """D4 — coverage envelope for event-grain results. coverage=None means no
    incremental ingestion domain feeds the view; otherwise a dict with
    coverage_status / watermark_date / watermark_at / date_from from
    mart.v_ai_source_coverage (date_from = MIN(business_date) ever loaded:
    evidence of the first loaded date, not proof every later date was ingested).

    P14-C: semantic NO_DATA only when coverage is CURRENT, the last successful
    poll passed to_date and from_date is not before the evidence start.
    MIN/MAX loaded dates never prove it — a hole inside them may never have
    been ingested. Rows without that proof stay PARTIAL_PERIOD_COVERAGE."""
    first, last = first_loaded_date, last_loaded_date
    base = {"first_loaded_date": first, "last_loaded_date": last, "source": source}
    rng = f"{first or 'never'}..{last or 'never'}"

    if coverage is None:
        # No incremental watermark: MIN/MAX are informational, no approved
        # historical coverage contract exists, so nothing here is READY or NO_DATA.
        fresh = {**base, "source_freshness_status": "MISSING_SOURCE"}
        no_wm = (f"No incremental ingestion domain feeds this view; rows are loaded for {rng} "
                 "with no continuous-coverage proof.")
        if row_count > 0:
            return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE", "blocking_reason": no_wm}
        if first is None or last is None or to_date < first:
            return {**fresh, "status": "MISSING_SOURCE",
                    "blocking_reason": f"{no_wm} Period is before any loaded date. {_NOT_ZERO}"}
        if from_date > last:
            return {**fresh, "status": "SOURCE_LAGGING",
                    "blocking_reason": f"{no_wm} Period is after the last loaded date. {_NOT_ZERO}"}
        if from_date >= first and to_date <= last:
            return {**fresh, "status": "MISSING_SOURCE",
                    "blocking_reason": f"{no_wm} Period is a gap inside the loaded range. {_NOT_ZERO}"}
        return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE",
                "blocking_reason": f"{no_wm} Period crosses the loaded range. {_NOT_ZERO}"}

    cs = coverage.get("coverage_status") or "MISSING_SOURCE"
    start = coverage.get("date_from")
    wm = coverage.get("watermark_date")
    fresh = {**base, "source_freshness_status": cs, "coverage_evidence_start_date": start,
             "coverage_start_basis": COVERAGE_START_BASIS, "watermark_at": coverage.get("watermark_at")}
    if cs == "NO_PERMISSION":
        return {**fresh, "status": "NO_PERMISSION", "blocking_reason": "Source has no API permission."}
    proven = cs == "CURRENT" and wm is not None and wm > to_date and start is not None and from_date >= start
    if proven:
        return {**fresh, "status": "READY" if row_count > 0 else "NO_DATA",
                "blocking_reason": None if row_count > 0 else
                f"Source is CURRENT, evidence starts {start}, and a successful poll passed the period end "
                f"({coverage.get('watermark_at')}); no events in this period."}
    not_proven = (f"Operational completeness for this period is not proven (source status {cs}, "
                  f"watermark {coverage.get('watermark_at') or 'none'}, evidence start {start or 'none'}).")
    if row_count > 0:
        return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE", "blocking_reason": not_proven}
    if start is None or to_date < start:
        return {**fresh, "status": "MISSING_SOURCE",
                "blocking_reason": f"{not_proven} Period is before the evidence start. {_NOT_ZERO}"}
    if from_date < start:
        return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE",
                "blocking_reason": f"{not_proven} Period crosses the evidence start. {_NOT_ZERO}"}
    # CURRENT whose poll reached into the period: partly known; not yet reached
    # (e.g. AMS T-2 latency): lagging. MART coverage NO_DATA means "never
    # loaded" — MISSING_SOURCE here, because this envelope's NO_DATA means
    # "proven, no events".
    if cs == "CURRENT":
        status = "PARTIAL_PERIOD_COVERAGE" if wm is not None and wm >= from_date else "SOURCE_LAGGING"
    else:
        status = "MISSING_SOURCE" if cs == "NO_DATA" else cs
    return {**fresh, "status": status, "blocking_reason": f"{not_proven} {_NOT_ZERO}"}
