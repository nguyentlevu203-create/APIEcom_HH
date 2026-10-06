// P14-B - regression tests for D1-D5 semantics. Pure functions only, no DB.
// Run: npm test  (node --test, Node >= 23 strips TypeScript types natively)

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cogsEnvelope, costLine, datesWithData, freshnessEnvelope, periodKpis, productFunnelStatus, type PeriodDayInput,
} from "../src/semantics.ts";

const day = (o: Partial<PeriodDayInput>): PeriodDayInput => ({
  net_sales: null, net_sales_status: null, platform_gmv: null, orders: null, sold_units: null, ...o,
});

// --- D1 -------------------------------------------------------------------

test("D1: single TikTok day with SOURCE_LAGGING null net_sales -> AOV/ASP null, not 0", () => {
  const k = periodKpis([day({ net_sales: null, net_sales_status: "SOURCE_LAGGING", platform_gmv: 1008200, orders: 3, sold_units: 4 })]);
  assert.equal((k.aov_net_sales as any).value, null);
  assert.equal((k.asp_net_sales as any).value, null);
  assert.equal((k.aov_net_sales as any).status, "SOURCE_LAGGING");
  assert.equal((k.asp_net_sales as any).status, "SOURCE_LAGGING");
  // GMV ratios still computed - GMV is a different, known input
  assert.equal((k.aov_platform_gmv as any).value, 1008200 / 3);
  assert.equal((k.aov_platform_gmv as any).status, "READY");
});

test("D1: all net_sales null with no status -> MISSING_SOURCE, value null", () => {
  const k = periodKpis([day({ orders: 5, sold_units: 5 }), day({ orders: 2, sold_units: 2 })]);
  assert.deepEqual([(k.aov_net_sales as any).value, (k.aov_net_sales as any).status], [null, "MISSING_SOURCE"]);
});

test("D1: partial period -> uses only dates with both inputs and is PARTIAL, never READY", () => {
  const k = periodKpis([
    day({ net_sales: 1000, net_sales_status: "COMPLETE_SETTLEMENT_COVERAGE", orders: 10, sold_units: 20 }),
    day({ net_sales: null, net_sales_status: "SOURCE_LAGGING", orders: 90, sold_units: 90 }),
  ]);
  const aov = k.aov_net_sales as any;
  assert.equal(aov.value, 100); // 1000/10, not 1000/100
  assert.equal(aov.status, "PARTIAL_PERIOD_COVERAGE");
  assert.deepEqual([aov.dates_used, aov.dates_total], [1, 2]);
});

test("D1: a partially settled date is not complete even when every value is present", () => {
  const k = periodKpis([day({ net_sales: 500, net_sales_status: "PARTIAL_SETTLEMENT_COVERAGE", orders: 5, sold_units: 5 })]);
  assert.equal((k.aov_net_sales as any).status, "PARTIAL_PERIOD_COVERAGE");
});

test("D1: complete inputs proving zero -> 0 READY", () => {
  const k = periodKpis([day({ net_sales: 0, net_sales_status: "READY", platform_gmv: 0, orders: 4, sold_units: 4 })]);
  assert.deepEqual([(k.aov_net_sales as any).value, (k.aov_net_sales as any).status], [0, "READY"]);
});

test("D1: complete Shopee period -> READY", () => {
  const k = periodKpis([
    day({ net_sales: 4121000, net_sales_status: "READY", platform_gmv: 3312663, orders: 15, sold_units: 19 }),
  ]);
  assert.equal((k.aov_net_sales as any).status, "READY");
  assert.equal((k.aov_net_sales as any).value, 4121000 / 15);
});

// --- D2 -------------------------------------------------------------------

test("D2: settlement lag -> fee null with the lag status (even if the MART still says 0)", () => {
  for (const s of ["SOURCE_LAGGING", "NOT_SETTLED_YET", "NO_ELIGIBLE_ORDERS"]) {
    assert.deepEqual(costLine("TIKTOK", "payment_fee", 0, s), { amount: null, status: s });
    assert.deepEqual(costLine("TIKTOK", "fixed_fee", null, s), { amount: null, status: s });
  }
});

test("D2: complete settlement actual zero -> 0 READY; positive -> amount READY", () => {
  assert.deepEqual(costLine("TIKTOK", "infrastructure_fee", 0, "COMPLETE_SETTLEMENT_COVERAGE"), { amount: 0, status: "READY" });
  assert.deepEqual(costLine("TIKTOK", "fixed_fee", 1341230, "COMPLETE_SETTLEMENT_COVERAGE"), { amount: 1341230, status: "READY" });
});

test("D2: partial settlement keeps the partial amount but never READY", () => {
  assert.deepEqual(costLine("TIKTOK", "vxp_fee", 42, "PARTIAL_SETTLEMENT_COVERAGE"), { amount: 42, status: "PARTIAL_SETTLEMENT_COVERAGE" });
});

test("D2: non-settlement TikTok lines and every Shopee line keep the previous rule", () => {
  assert.deepEqual(costLine("TIKTOK", "hh_internal_packaging_cost", 0, "SOURCE_LAGGING"), { amount: 0, status: "READY" });
  assert.deepEqual(costLine("TIKTOK", "ads_spend", null, "SOURCE_LAGGING"), { amount: null, status: "SEPARATE_API_REQUIRED" });
  assert.deepEqual(costLine("SHOPEE", "fixed_fee", 806405, "READY"), { amount: 806405, status: "READY" });
  assert.deepEqual(costLine("SHOPEE", "vxp_fee", null, "READY"), { amount: null, status: "MISSING_SOURCE" });
});

// --- D3 -------------------------------------------------------------------

test("D3: known partial COGS + unresolved lines -> value kept, COGS_INCOMPLETE", () => {
  assert.deepEqual(cogsEnvelope(880767.38, 880767.38, 0, "COGS_INCOMPLETE", "COGS_INCOMPLETE"),
    { value: 880767.38, status: "COGS_INCOMPLETE" });
  assert.deepEqual(cogsEnvelope(100, 100, 0, "READY", "COGS_INCOMPLETE"), { value: 100, status: "COGS_INCOMPLETE" });
});

test("D3: fully resolved -> READY", () => {
  assert.deepEqual(cogsEnvelope(100, 90, 10, "READY", "READY"), { value: 100, status: "READY" });
});

test("D3: no COGS components at all -> NULL, not the view's COALESCE 0", () => {
  assert.deepEqual(cogsEnvelope(0, null, null, null, null), { value: null, status: "MISSING_SOURCE" });
});

// --- D4 -------------------------------------------------------------------

test("D4: stale video source (no incremental domain) + later date -> SOURCE_LAGGING, not zero", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0,
    earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-09-12", coverage: null, source: "mart.v_ai_video_daily" });
  assert.equal(e.status, "SOURCE_LAGGING");
  assert.equal(e.source_freshness_status, "MISSING_SOURCE");
  assert.equal(e.latest_available_date, "2026-09-12");
  assert.match(String(e.blocking_reason), /NOT zero activity/);
});

test("D4: current sparse source polled past the period + no events -> NO_DATA, not lag", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-05", toDate: "2026-10-05", rowCount: 0, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-03",
    coverage: { coverage_status: "CURRENT", watermark_date: "2026-10-06", watermark_at: "2026-10-06T08:46:18+07:00", date_from: "2026-09-01" },
    source: "mart.v_ai_affiliate_creator_daily" });
  assert.equal(e.status, "NO_DATA");
  assert.equal(e.source_freshness_status, "CURRENT");
});

test("D4: current source whose poll has not passed the period end -> PARTIAL, not NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-05",
    coverage: { coverage_status: "CURRENT", watermark_date: "2026-10-06", watermark_at: "2026-10-06T10:02:19+07:00", date_from: "2026-09-01" },
    source: "x" });
  assert.equal(e.status, "PARTIAL_PERIOD_COVERAGE");
});

test("D4: CURRENT source whose poll has not reached the period yet -> SOURCE_LAGGING", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-04",
    coverage: { coverage_status: "CURRENT", watermark_date: "2026-10-04", watermark_at: "2026-10-04T23:59:59+07:00", date_from: "2026-09-01" },
    source: "mart.v_ai_affiliate_creator_daily" });
  assert.equal(e.status, "SOURCE_LAGGING");
  assert.equal(e.source_freshness_status, "CURRENT");
});

test("D4: MART says SOURCE_LAGGING -> stays SOURCE_LAGGING for an uncovered empty period", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-04",
    coverage: { coverage_status: "SOURCE_LAGGING", watermark_date: "2026-10-06", watermark_at: "t", date_from: "2026-09-01" }, source: "mart.v_ai_live_daily" });
  assert.equal(e.status, "SOURCE_LAGGING");
});

test("D4: empty period inside loaded history -> NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-09-20", toDate: "2026-09-20", rowCount: 0, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-04",
    coverage: { coverage_status: "SOURCE_LAGGING", watermark_date: "2026-10-06", watermark_at: "t", date_from: "2026-09-01" }, source: "x" });
  assert.equal(e.status, "NO_DATA");
});

test("D4: NO_PERMISSION remains NO_PERMISSION", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0, earliestAvailableDate: null, latestAvailableDate: null,
    coverage: { coverage_status: "NO_PERMISSION", watermark_date: null, watermark_at: null, date_from: null }, source: "x" });
  assert.equal(e.status, "NO_PERMISSION");
});

test("D4: rows returned but period extends past loaded data -> PARTIAL_PERIOD_COVERAGE", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-01", toDate: "2026-10-06", rowCount: 12, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-04",
    coverage: { coverage_status: "SOURCE_LAGGING", watermark_date: "2026-10-06", watermark_at: "t", date_from: "2026-09-01" }, source: "x" });
  assert.equal(e.status, "PARTIAL_PERIOD_COVERAGE");
});

// --- P14-B2: lower bound of proven coverage -------------------------------

const HIST = { earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-09-12", coverage: null, source: "x" };
const CURRENT_SINCE = (dateFrom: string | null) =>
  ({ coverage_status: "CURRENT", watermark_date: "2026-10-06", watermark_at: "2026-10-06T08:00:00+07:00", date_from: dateFrom });

test("B2-A: empty period before the first loaded date -> NOT NO_DATA", () => {
  const e = freshnessEnvelope({ ...HIST, fromDate: "2026-08-20", toDate: "2026-08-25", rowCount: 0 });
  assert.notEqual(e.status, "NO_DATA");
  assert.equal(e.status, "MISSING_SOURCE");
  assert.equal(e.earliest_available_date, "2026-09-01");
  assert.match(String(e.blocking_reason), /predates the loaded history/);
});

test("B2-B: empty period inside proven loaded history -> NO_DATA", () => {
  const e = freshnessEnvelope({ ...HIST, fromDate: "2026-09-05", toDate: "2026-09-05", rowCount: 0 });
  assert.equal(e.status, "NO_DATA");
});

test("B2-C: period overlapping the lower boundary -> PARTIAL, never READY/NO_DATA", () => {
  for (const rowCount of [0, 3]) {
    const e = freshnessEnvelope({ ...HIST, fromDate: "2026-08-30", toDate: "2026-09-05", rowCount });
    assert.equal(e.status, "PARTIAL_PERIOD_COVERAGE", `rowCount=${rowCount}`);
  }
});

test("B2-C: max(date) alone never proves coverage (no earliest -> not NO_DATA)", () => {
  const e = freshnessEnvelope({ ...HIST, earliestAvailableDate: null, fromDate: "2026-09-05", toDate: "2026-09-05", rowCount: 0 });
  assert.notEqual(e.status, "NO_DATA");
});

test("B2-D: CURRENT sparse source polled past, period inside proven window, no events -> NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-05", toDate: "2026-10-05", rowCount: 0,
    earliestAvailableDate: "2026-09-03", latestAvailableDate: "2026-10-03", coverage: CURRENT_SINCE("2026-09-01"), source: "x" });
  assert.equal(e.status, "NO_DATA");
  assert.equal(e.coverage_start_date, "2026-09-01");
});

test("B2-E: CURRENT source polled past, period predates proven coverage start -> NOT NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-08-20", toDate: "2026-08-25", rowCount: 0,
    earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-03", coverage: CURRENT_SINCE("2026-09-01"), source: "x" });
  assert.notEqual(e.status, "NO_DATA");
  assert.equal(e.status, "MISSING_SOURCE");
});

test("B2-E: CURRENT source polled past with no provable coverage start -> conservative, not NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-05", toDate: "2026-10-05", rowCount: 0,
    earliestAvailableDate: null, latestAvailableDate: null, coverage: CURRENT_SINCE(null), source: "x" });
  assert.equal(e.status, "MISSING_SOURCE");
});

// --- P14-B2: product_funnel status is the coverage status -------------------

const pfEnvelope = (rowCount: number, coverage: ReturnType<typeof CURRENT_SINCE> | Record<string, unknown>, from: string, to: string) =>
  freshnessEnvelope({ fromDate: from, toDate: to, rowCount, earliestAvailableDate: "2026-09-01", latestAvailableDate: "2026-10-03",
    coverage: coverage as never, source: "mart.v_ai_product_traffic_daily" });

test("B2-F: empty product period + SOURCE_LAGGING coverage never shows top-level NO_DATA", () => {
  const cov = pfEnvelope(0, { coverage_status: "SOURCE_LAGGING", watermark_date: "2026-10-04", watermark_at: "t", date_from: "2026-09-01" },
    "2026-10-05", "2026-10-05");
  assert.equal(cov.status, "SOURCE_LAGGING");
  assert.equal(productFunnelStatus(cov.status), "SOURCE_LAGGING");
  assert.notEqual(productFunnelStatus(cov.status), "NO_DATA");
});

test("B2-G: empty product period + proven covered CURRENT period -> NO_DATA both places", () => {
  const cov = pfEnvelope(0, CURRENT_SINCE("2026-09-01"), "2026-10-05", "2026-10-05");
  assert.equal(cov.status, "NO_DATA");
  assert.equal(productFunnelStatus(cov.status), "NO_DATA");
});

test("B2: product_funnel status mapping is one-to-one with coverage", () => {
  assert.equal(productFunnelStatus("READY"), "API_ACTUAL");
  for (const s of ["NO_DATA", "SOURCE_LAGGING", "PARTIAL_PERIOD_COVERAGE", "MISSING_SOURCE", "NO_PERMISSION"]) {
    assert.equal(productFunnelStatus(s), s);
  }
});

test("B2: 3de9968 invariant - mart NO_DATA (never loaded) -> MISSING_SOURCE / source_freshness NO_DATA", () => {
  const e = freshnessEnvelope({ fromDate: "2026-10-06", toDate: "2026-10-06", rowCount: 0, earliestAvailableDate: null,
    latestAvailableDate: null, coverage: { coverage_status: "NO_DATA", watermark_date: null, watermark_at: null, date_from: null }, source: "x" });
  assert.deepEqual([e.status, e.source_freshness_status], ["MISSING_SOURCE", "NO_DATA"]);
});

// --- D5 -------------------------------------------------------------------

test("D5: dates_with_data derived from actual rows; empty -> []", () => {
  assert.deepEqual(datesWithData(["2026-10-06", "2026-10-04"]), ["2026-10-04", "2026-10-06"]);
  assert.deepEqual(datesWithData(null), []);
});

// --- shared parity vectors (also run by the Python reference test) ----------

import { readFileSync } from "node:fs";
const V = JSON.parse(readFileSync(new URL("./parity_vectors.json", import.meta.url), "utf8"));

test("parity vectors: costLine", () => {
  for (const c of V.cost_line) {
    const [ch, col, val, ns] = c.args;
    assert.deepEqual(costLine(ch, col, val, ns), { amount: c.amount, status: c.status }, JSON.stringify(c.args));
  }
});

test("parity vectors: cogsEnvelope", () => {
  for (const c of V.cogs) assert.deepEqual(cogsEnvelope(...(c.args as [unknown, unknown, unknown, unknown, unknown])), { value: c.value, status: c.status });
});

test("parity vectors: freshnessEnvelope", () => {
  for (const c of V.freshness) {
    const e = freshnessEnvelope({ ...c.in, source: "x" });
    assert.deepEqual([e.status, e.source_freshness_status], [c.status, c.source_freshness_status], JSON.stringify(c.in));
  }
});
