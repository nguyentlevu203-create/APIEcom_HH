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

export interface FreshnessCoverage {
  coverage_status: string | null;
  watermark_date: string | null;
  watermark_at: string | null;
  // mart.v_ai_source_coverage.date_from = MIN(business_date) ever loaded for
  // this ingestion domain. Evidence of the first loaded date only: it does
  // NOT prove that every date after it was successfully ingested.
  date_from: string | null;
}

export interface FreshnessInput {
  fromDate: string;
  toDate: string;
  rowCount: number;
  // MIN/MAX(business_date) of the queried view itself (same filter as the
  // tool). Data bounds only - never proof of continuous coverage inside them.
  firstLoadedDate: string | null;
  lastLoadedDate: string | null;
  // null = no incremental ingestion domain feeds this view at all
  coverage: FreshnessCoverage | null;
  source: string;
}

export const COVERAGE_START_BASIS = "FIRST_LOADED_BUSINESS_DATE_CONSERVATIVE_BOUND";

const NOT_ZERO = "An empty result for this period is NOT zero activity.";

/** P14-C - semantic NO_DATA is proven only by an operational watermark:
 * coverage CURRENT, last successful poll past toDate, and fromDate not
 * before the evidence start. MIN/MAX loaded dates never prove it - a hole
 * inside them may simply never have been ingested. Rows without that proof
 * are real ACTUAL rows but the period stays PARTIAL_PERIOD_COVERAGE. */
export function freshnessEnvelope(f: FreshnessInput): Row {
  const first = f.firstLoadedDate;
  const last = f.lastLoadedDate;
  const base = { first_loaded_date: first, last_loaded_date: last, source: f.source };
  const range = `${first ?? "never"}..${last ?? "never"}`;

  if (f.coverage === null) {
    // No incremental watermark: MIN/MAX are informational, no approved
    // historical coverage contract exists, so nothing here is ever READY or NO_DATA.
    const fresh = { ...base, source_freshness_status: "MISSING_SOURCE" };
    const noWatermark = `No incremental ingestion domain feeds this view; rows are loaded for ${range} ` +
      "with no continuous-coverage proof.";
    if (f.rowCount > 0) return { ...fresh, status: "PARTIAL_PERIOD_COVERAGE", blocking_reason: noWatermark };
    if (first === null || last === null || f.toDate < first) {
      return { ...fresh, status: "MISSING_SOURCE", blocking_reason: `${noWatermark} Period is before any loaded date. ${NOT_ZERO}` };
    }
    if (f.fromDate > last) {
      return { ...fresh, status: "SOURCE_LAGGING", blocking_reason: `${noWatermark} Period is after the last loaded date. ${NOT_ZERO}` };
    }
    if (f.fromDate >= first && f.toDate <= last) {
      return { ...fresh, status: "MISSING_SOURCE", blocking_reason: `${noWatermark} Period is a gap inside the loaded range. ${NOT_ZERO}` };
    }
    return { ...fresh, status: "PARTIAL_PERIOD_COVERAGE", blocking_reason: `${noWatermark} Period crosses the loaded range. ${NOT_ZERO}` };
  }

  const cs = f.coverage.coverage_status ?? "MISSING_SOURCE";
  const start = f.coverage.date_from;
  const wm = f.coverage.watermark_date;
  const fresh = { ...base, source_freshness_status: cs, coverage_evidence_start_date: start,
    coverage_start_basis: COVERAGE_START_BASIS, watermark_at: f.coverage.watermark_at };
  if (cs === "NO_PERMISSION") {
    return { ...fresh, status: "NO_PERMISSION", blocking_reason: "Source has no API permission." };
  }
  const proven = cs === "CURRENT" && wm !== null && wm > f.toDate && start !== null && f.fromDate >= start;
  if (proven) {
    return { ...fresh, status: f.rowCount > 0 ? "READY" : "NO_DATA",
      blocking_reason: f.rowCount > 0 ? null
        : `Source is CURRENT, evidence starts ${start}, and a successful poll passed the period end (${f.coverage.watermark_at}); ` +
          "no events in this period." };
  }
  const notProven = `Operational completeness for this period is not proven (source status ${cs}, ` +
    `watermark ${f.coverage.watermark_at ?? "none"}, evidence start ${start ?? "none"}).`;
  if (f.rowCount > 0) return { ...fresh, status: "PARTIAL_PERIOD_COVERAGE", blocking_reason: notProven };
  if (start === null || f.toDate < start) {
    return { ...fresh, status: "MISSING_SOURCE", blocking_reason: `${notProven} Period is before the evidence start. ${NOT_ZERO}` };
  }
  if (f.fromDate < start) {
    return { ...fresh, status: "PARTIAL_PERIOD_COVERAGE", blocking_reason: `${notProven} Period crosses the evidence start. ${NOT_ZERO}` };
  }
  // CURRENT whose poll reached into the period: partly known; not yet reached
  // (e.g. AMS T-2 latency): lagging. MART coverage NO_DATA means "never
  // loaded" - MISSING_SOURCE here, because this envelope's NO_DATA means
  // "proven, no events".
  const status = cs === "CURRENT" ? (wm !== null && wm >= f.fromDate ? "PARTIAL_PERIOD_COVERAGE" : "SOURCE_LAGGING")
    : cs === "NO_DATA" ? "MISSING_SOURCE" : cs;
  return { ...fresh, status, blocking_reason: `${notProven} ${NOT_ZERO}` };
}

// ---------------------------------------------------------------------------
// D5 - product funnel date coverage
// ---------------------------------------------------------------------------

export function datesWithData(dates: unknown): string[] {
  if (!Array.isArray(dates)) return [];
  return dates.map(String).sort();
}

/** P14-B2 - product_funnel exposes exactly one semantic state: its coverage
 * status, with READY (covered and has rows) shown as API_ACTUAL. Never an
 * independent rows-based NO_DATA that could contradict SOURCE_LAGGING. */
export function productFunnelStatus(coverageStatus: unknown): string {
  const s = String(coverageStatus);
  return s === "READY" ? "API_ACTUAL" : s;
}
