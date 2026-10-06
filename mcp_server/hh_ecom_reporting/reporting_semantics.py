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


_NOT_ZERO = "An empty result for this period is NOT zero activity."


def freshness_envelope(from_date: str, to_date: str, row_count: int, earliest_available_date: Optional[str],
                       latest_available_date: Optional[str], coverage: Optional[dict], source: str) -> dict:
    """D4 — coverage envelope for event-grain results. coverage=None means no
    incremental ingestion domain feeds the view; otherwise a dict with
    coverage_status / watermark_date / watermark_at / date_from from
    mart.v_ai_source_coverage. P14-B2: a period is known only inside a proven
    [lower, upper] window — max(date) alone never proves earlier history."""
    earliest, latest = earliest_available_date, latest_available_date
    base = {"earliest_available_date": earliest, "latest_available_date": latest, "source": source}
    loaded_covers = (earliest is not None and latest is not None
                     and from_date >= earliest and to_date <= latest)
    rng = f"{earliest or 'never'}..{latest or 'never'}"

    if coverage is None:
        fresh = {**base, "source_freshness_status": "MISSING_SOURCE"}
        if loaded_covers:
            return {**fresh, "status": "READY" if row_count > 0 else "NO_DATA",
                    "blocking_reason": None if row_count > 0 else
                    "Period lies inside the loaded history and returned no rows."}
        if row_count > 0:
            return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE",
                    "blocking_reason": f"No incremental ingestion domain feeds this view; data is loaded only for {rng}."}
        if earliest is not None and to_date < earliest:
            return {**fresh, "status": "MISSING_SOURCE",
                    "blocking_reason": f"Period predates the loaded history ({rng}). {_NOT_ZERO}"}
        if earliest is not None and from_date < earliest and latest is not None and to_date <= latest:
            return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE",
                    "blocking_reason": f"Period starts before the loaded history ({rng}). {_NOT_ZERO}"}
        return {**fresh, "status": "SOURCE_LAGGING",
                "blocking_reason": f"No incremental ingestion domain feeds this view; data is loaded only for {rng}. "
                                   f"{_NOT_ZERO}"}

    cs = coverage.get("coverage_status") or "MISSING_SOURCE"
    wm_date = coverage.get("watermark_date")
    lower = coverage.get("date_from")
    fresh = {**base, "source_freshness_status": cs, "coverage_start_date": lower,
             "watermark_at": coverage.get("watermark_at")}
    if cs == "NO_PERMISSION":
        return {**fresh, "status": "NO_PERMISSION", "blocking_reason": "Source has no API permission."}
    # A successful poll past to_date proves absence only inside the source's
    # proven window: never before its first loaded date, never without one.
    covered_by_poll = (cs == "CURRENT" and wm_date is not None and wm_date > to_date
                       and lower is not None and from_date >= lower)
    covered = loaded_covers or covered_by_poll
    if row_count > 0:
        return {**fresh, "status": "READY" if covered else "PARTIAL_PERIOD_COVERAGE",
                "blocking_reason": None if covered else
                f"Source data is proven only for {lower or earliest or 'never'}..{latest or 'never'}; "
                "dates outside it are unknown or may still arrive."}
    if covered:
        return {**fresh, "status": "NO_DATA",
                "blocking_reason": "Period lies inside the loaded history and returned no rows." if loaded_covers else
                f"Source is CURRENT, covered since {lower}, and was polled past the period end "
                f"({coverage.get('watermark_at')}); no events in this period."}
    lb = lower if lower is not None else earliest
    if lb is not None and to_date < lb:
        return {**fresh, "status": "MISSING_SOURCE",
                "blocking_reason": f"Period predates the source's proven coverage start ({lb}). {_NOT_ZERO}"}
    if lb is not None and from_date < lb:
        return {**fresh, "status": "PARTIAL_PERIOD_COVERAGE",
                "blocking_reason": f"Period starts before the source's proven coverage start ({lb}). {_NOT_ZERO}"}
    if cs == "CURRENT" and lb is None:
        return {**fresh, "status": "MISSING_SOURCE",
                "blocking_reason": f"Source is CURRENT but has no loaded date to prove a coverage start. {_NOT_ZERO}"}
    poll_in_period = wm_date is not None and wm_date >= from_date
    # mart.v_ai_source_coverage NO_DATA means "nothing ever loaded" — reported as
    # MISSING_SOURCE here, because this envelope's NO_DATA means "covered, no events".
    if cs == "CURRENT":
        status = "PARTIAL_PERIOD_COVERAGE" if poll_in_period else "SOURCE_LAGGING"
    else:
        status = "MISSING_SOURCE" if cs == "NO_DATA" else cs
    return {**fresh, "status": status,
            "blocking_reason": f"Source data available only through {latest or 'never'} (source status {cs}). "
                               f"{_NOT_ZERO}"}

    cs = coverage.get("coverage_status") or "MISSING_SOURCE"
    wm_date = coverage.get("watermark_date")
    fresh = {**base, "source_freshness_status": cs, "watermark_at": coverage.get("watermark_at")}
    if cs == "NO_PERMISSION":
        return {**fresh, "status": "NO_PERMISSION", "blocking_reason": "Source has no API permission."}
    covered_by_data = latest is not None and to_date <= latest
    covered_by_poll = cs == "CURRENT" and wm_date is not None and wm_date > to_date
    covered = covered_by_data or covered_by_poll
    if row_count > 0:
        return {**fresh, "status": "READY" if covered else "PARTIAL_PERIOD_COVERAGE",
                "blocking_reason": None if covered else
                f"Source data available only through {latest}; later dates in the period may still arrive."}
    if covered:
        return {**fresh, "status": "NO_DATA",
                "blocking_reason": "Period lies inside the loaded history and returned no rows." if covered_by_data else
                f"Source is CURRENT and was polled past the period end ({coverage.get('watermark_at')}); "
                "no events in this period."}
    poll_in_period = wm_date is not None and wm_date >= from_date
    # mart.v_ai_source_coverage NO_DATA means "nothing ever loaded" — reported as
    # MISSING_SOURCE here, because this envelope's NO_DATA means "covered, no events".
    if cs == "CURRENT":
        status = "PARTIAL_PERIOD_COVERAGE" if poll_in_period else "SOURCE_LAGGING"
    else:
        status = "MISSING_SOURCE" if cs == "NO_DATA" else cs
    return {**fresh, "status": status,
            "blocking_reason": f"Source data available only through {latest or 'never'} (source status {cs}). "
                               "An empty result for this period is NOT zero activity."}
