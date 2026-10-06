// P14-B - pure status/NULL semantics shared by the reporting tools.
//
// No I/O here: every function takes values already read from the
// approved mart views and decides value/status. Kept separate from
// queryService.ts so each rule has one definition and can be tested
// without a database.
//
// Doctrine: NULL != 0, UNKNOWN != ZERO, PARTIAL != COMPLETE,
// SOURCE_LAGGING != READY, no events != zero events unless source
// currentness proves it, Platform GMV != Net Sales.

type Row = Record<string, unknown>;

export function isKnown(v: unknown): boolean {
  return v !== null && v !== undefined;
}

// mart.v_ceo_ecom_daily.net_sales_status values meaning "the whole day is in".
// SHOPEE uses READY; TIKTOK uses the sql/044 settlement-completeness vocabulary.
export const NET_SALES_COMPLETE_STATUSES = new Set(["READY", "COMPLETE_SETTLEMENT_COVERAGE"]);

// The same set the sql/044 guard (mart.v_ceo_ecom_daily `incomplete` CTE)
// treats as "not settled yet" - net_sales/gm1/cm1 are already NULL there.
export const SETTLEMENT_LAG_STATUSES = new Set(["SOURCE_LAGGING", "NOT_SETTLED_YET", "NO_ELIGIBLE_ORDERS"]);

// ---------------------------------------------------------------------------
// D1 - period AOV/ASP
// ---------------------------------------------------------------------------

export interface PeriodDayInput {
  net_sales: unknown;
  net_sales_status: unknown;
  platform_gmv: unknown;
  orders: unknown;
  sold_units: unknown;
}

function reasonForUnknown(statuses: string[]): string {
  if (statuses.some((s) => SETTLEMENT_LAG_STATUSES.has(s))) return "SOURCE_LAGGING";
  const distinct = [...new Set(statuses)];
  return distinct.length === 1 ? distinct[0] : "MISSING_SOURCE";
}

/** SUM(numerator)/SUM(denominator) over the dates where BOTH are known.
 * value is null when no date has both (never the accumulator's 0), and
 * status is READY only when every date is usable and complete. */
function periodRatio(
  days: PeriodDayInput[],
  num: (d: PeriodDayInput) => unknown,
  numComplete: (d: PeriodDayInput) => boolean,
  numUnknownStatus: (d: PeriodDayInput) => string,
  den: (d: PeriodDayInput) => unknown,
): Row {
  let numSum = 0;
  let denSum = 0;
  let used = 0;
  let allComplete = true;
  const unknownStatuses: string[] = [];
  for (const d of days) {
    const n = num(d);
    const m = den(d);
    if (!isKnown(n)) unknownStatuses.push(numUnknownStatus(d));
    if (!isKnown(m)) unknownStatuses.push("MISSING_SOURCE");
    if (!isKnown(n) || !isKnown(m)) continue;
    used += 1;
    numSum += Number(n);
    denSum += Number(m);
    if (!numComplete(d)) allComplete = false;
  }
  if (used === 0) {
    return { value: null, status: reasonForUnknown(unknownStatuses), dates_used: 0, dates_total: days.length };
  }
  const value = denSum !== 0 ? numSum / denSum : null;
  let status: string;
  if (used < days.length || !allComplete) status = "PARTIAL_PERIOD_COVERAGE";
  else status = value === null ? "NOT_COMPUTABLE_ZERO_DENOMINATOR" : "READY";
  return { value, status, dates_used: used, dates_total: days.length };
}

export function periodKpis(days: PeriodDayInput[]): Row {
  const netSales = (d: PeriodDayInput) => d.net_sales;
  const netSalesComplete = (d: PeriodDayInput) => NET_SALES_COMPLETE_STATUSES.has(String(d.net_sales_status));
  const netSalesUnknown = (d: PeriodDayInput) => (isKnown(d.net_sales_status) ? String(d.net_sales_status) : "MISSING_SOURCE");
  const gmv = (d: PeriodDayInput) => d.platform_gmv;
  const always = () => true;
  const missing = () => "MISSING_SOURCE";
  return {
    aov_net_sales: periodRatio(days, netSales, netSalesComplete, netSalesUnknown, (d) => d.orders),
    aov_platform_gmv: periodRatio(days, gmv, always, missing, (d) => d.orders),
    asp_net_sales: periodRatio(days, netSales, netSalesComplete, netSalesUnknown, (d) => d.sold_units),
    asp_platform_gmv: periodRatio(days, gmv, always, missing, (d) => d.sold_units),
    formula_basis: "SUM(numerator)/SUM(denominator) over the dates where both are known — never an average of " +
      "daily ratios, never a NULL counted as 0. dates_used < dates_total or a partially-settled date => " +
      "PARTIAL_PERIOD_COVERAGE.",
  };
}

// ---------------------------------------------------------------------------
// D2 - cost line status (TikTok settlement-derived fees)
// ---------------------------------------------------------------------------

// mart.v_ceo_ecom_daily columns whose TikTok value is -SUM(core.fact_settlement_sku_fee.<fee>)
// (tiktok_*_fee_actual, _p6b3_pnl_extend.py). Completeness = that date's settlement coverage,
// which the view already exposes as net_sales_status.
export const TIKTOK_SETTLEMENT_FEE_COLUMNS = new Set([
  "fixed_fee", "payment_fee", "vxp_fee", "infrastructure_fee", "affiliate_fee",
]);

export const KNOWN_MISSING_COST_STATUS: Record<string, string> = {
  ads_spend: "SEPARATE_API_REQUIRED",
  booking_kol_koc: "MISSING_SOURCE",
  live_inhouse_cost: "MISSING_SOURCE",
};

export function costLine(channel: unknown, column: string, value: unknown, netSalesStatus: unknown): { amount: unknown; status: string } {
  if (channel === "TIKTOK" && TIKTOK_SETTLEMENT_FEE_COLUMNS.has(column)) {
    const s = isKnown(netSalesStatus) ? String(netSalesStatus) : null;
    // Unsettled date: any number there is unproven (sql/066 also NULLs it in
    // the MART; this keeps the same answer before 066 is applied).
    if (s !== null && SETTLEMENT_LAG_STATUSES.has(s)) return { amount: null, status: s };
    if (!isKnown(value)) return { amount: null, status: "MISSING_SOURCE" };
    if (s === "COMPLETE_SETTLEMENT_COVERAGE") return { amount: value, status: "READY" };
    if (s === "PARTIAL_SETTLEMENT_COVERAGE") return { amount: value, status: "PARTIAL_SETTLEMENT_COVERAGE" };
    return { amount: value, status: "SETTLEMENT_COVERAGE_UNKNOWN" };
  }
  if (isKnown(value)) return { amount: value, status: "READY" };
  return { amount: null, status: KNOWN_MISSING_COST_STATUS[column] ?? "MISSING_SOURCE" };
}

// ---------------------------------------------------------------------------
// D3 - COGS status
// ---------------------------------------------------------------------------

/** Completeness comes from the Gold coverage_status of sellable_cogs and
 * promo_gift_cost (mart.v_ai_metric_status) - the same flag Gold uses to
 * mark gm1 COGS_INCOMPLETE. A known partial amount is kept, not hidden. */
export function cogsEnvelope(
  totalCogs: unknown, sellableCogs: unknown, promoGiftCost: unknown,
  sellableStatus: unknown, promoStatus: unknown,
): { value: unknown; status: string } {
  const statuses = [sellableStatus, promoStatus].map((s) => (isKnown(s) ? String(s) : null));
  if (!isKnown(sellableCogs) && !isKnown(promoGiftCost)) {
    // view's total_cogs is COALESCE(..,0)+COALESCE(..,0): 0 here would be fabricated.
    return { value: null, status: statuses.find((s) => s !== null && s !== "READY") ?? "MISSING_SOURCE" };
  }
  if (statuses.includes("COGS_INCOMPLETE")) return { value: totalCogs, status: "COGS_INCOMPLETE" };
  if (statuses.every((s) => s === "READY")) return { value: totalCogs, status: "READY" };
  return { value: totalCogs, status: statuses.find((s) => s !== null && s !== "READY") ?? "MISSING_SOURCE" };
}

// ---------------------------------------------------------------------------
// D4 - freshness envelope for event-grain tools
// ---------------------------------------------------------------------------

export interface FreshnessInput {
  fromDate: string;
  toDate: string;
  rowCount: number;
  latestAvailableDate: string | null;
  // null = no incremental ingestion domain feeds this view at all
  coverage: { coverage_status: string | null; watermark_date: string | null; watermark_at: string | null } | null;
  source: string;
}

export function freshnessEnvelope(f: FreshnessInput): Row {
  const latest = f.latestAvailableDate;
  const base = { latest_available_date: latest, source: f.source };

  if (f.coverage === null) {
    const inRange = latest !== null && f.toDate <= latest;
    if (f.rowCount > 0) {
      return { ...base, status: inRange ? "READY" : "PARTIAL_PERIOD_COVERAGE", source_freshness_status: "MISSING_SOURCE",
        blocking_reason: inRange ? null : `No incremental ingestion domain feeds this view; data exists only through ${latest}.` };
    }
    return { ...base, status: inRange ? "NO_DATA" : "SOURCE_LAGGING", source_freshness_status: "MISSING_SOURCE",
      blocking_reason: inRange
        ? "Period lies inside the loaded history and returned no rows."
        : `No incremental ingestion domain feeds this view; data exists only through ${latest ?? "never"}. ` +
          "An empty result for this period is NOT zero activity." };
  }

  const cs = f.coverage.coverage_status ?? "MISSING_SOURCE";
  const freshness = { ...base, source_freshness_status: cs, watermark_at: f.coverage.watermark_at };
  if (cs === "NO_PERMISSION") {
    return { ...freshness, status: "NO_PERMISSION", blocking_reason: "Source has no API permission." };
  }
  const coveredByData = latest !== null && f.toDate <= latest;
  const coveredByPoll = cs === "CURRENT" && f.coverage.watermark_date !== null && f.coverage.watermark_date > f.toDate;
  const covered = coveredByData || coveredByPoll;
  if (f.rowCount > 0) {
    return { ...freshness, status: covered ? "READY" : "PARTIAL_PERIOD_COVERAGE",
      blocking_reason: covered ? null : `Source data available only through ${latest}; later dates in the period may still arrive.` };
  }
  if (covered) {
    return { ...freshness, status: "NO_DATA",
      blocking_reason: coveredByData
        ? "Period lies inside the loaded history and returned no rows."
        : `Source is CURRENT and was polled past the period end (${f.coverage.watermark_at}); no events in this period.` };
  }
  // A CURRENT source whose poll already reached into the period: partly known.
  // One whose poll has not reached the period yet (e.g. AMS T-2 latency): lagging.
  const pollInPeriod = f.coverage.watermark_date !== null && f.coverage.watermark_date >= f.fromDate;
  return { ...freshness, status: cs !== "CURRENT" ? cs : pollInPeriod ? "PARTIAL_PERIOD_COVERAGE" : "SOURCE_LAGGING",
    blocking_reason: `Source data available only through ${latest ?? "never"} (source status ${cs}). ` +
      "An empty result for this period is NOT zero activity." };
}

// ---------------------------------------------------------------------------
// D5 - product funnel date coverage
// ---------------------------------------------------------------------------

export function datesWithData(dates: unknown): string[] {
  if (!Array.isArray(dates)) return [];
  return dates.map(String).sort();
}
