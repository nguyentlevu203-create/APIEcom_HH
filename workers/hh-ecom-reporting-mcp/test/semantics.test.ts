// P14-B/P14-C - regression tests for D1-D5 semantics. Pure functions only, no DB.
// Run: npm test  (node --test, Node >= 23 strips TypeScript types natively)

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cogsEnvelope, costLine, datesWithData, freshnessEnvelope, periodKpis, productFunnelStatus, type PeriodDayInput,
} from "../src/semantics.ts";
import { readFileSync } from "node:fs";

const V = JSON.parse(readFileSync(new URL("./parity_vectors.json", import.meta.url), "utf8"));

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

// --- D4 / P14-C: NO_DATA only from an operational watermark ---------------

type Cov = { coverage_status: string; watermark_date: string | null; watermark_at: string | null; date_from: string | null };
const cov = (cs: string, wm: string | null, start: string | null): Cov =>
  ({ coverage_status: cs, watermark_date: wm, watermark_at: wm && `${wm}T08:00:00+07:00`, date_from: start });
const env = (from: string, to: string, rowCount: number, first: string | null, last: string | null, coverage: Cov | null) =>
  freshnessEnvelope({ fromDate: from, toDate: to, rowCount, firstLoadedDate: first, lastLoadedDate: last, coverage, source: "x" });
const LAG = cov("SOURCE_LAGGING", "2026-10-06", "2026-09-01");
const CUR = cov("CURRENT", "2026-10-06", "2026-09-01");

test("A: SOURCE_LAGGING, MIN=09-01 MAX=09-12, request 09-05, rows=0 -> NOT NO_DATA", () => {
  const e = env("2026-09-05", "2026-09-05", 0, "2026-09-01", "2026-09-12", LAG);
  assert.notEqual(e.status, "NO_DATA");
  assert.equal(e.status, "SOURCE_LAGGING");
  assert.match(String(e.blocking_reason), /NOT zero activity/);
});

test("B: same source, rows>0 -> PARTIAL_PERIOD_COVERAGE, never READY", () => {
  assert.equal(env("2026-09-05", "2026-09-05", 3, "2026-09-01", "2026-09-12", LAG).status, "PARTIAL_PERIOD_COVERAGE");
});

test("C: no incremental domain, hole inside MIN..MAX, rows=0 -> NOT NO_DATA", () => {
  const e = env("2026-09-05", "2026-09-05", 0, "2026-09-01", "2026-09-12", null);
  assert.notEqual(e.status, "NO_DATA");
  assert.equal(e.status, "MISSING_SOURCE");
  assert.match(String(e.blocking_reason), /gap inside the loaded range/);
});

test("D: no incremental domain, rows>0 -> PARTIAL_PERIOD_COVERAGE (no historical coverage contract)", () => {
  assert.equal(env("2026-09-05", "2026-09-05", 2, "2026-09-01", "2026-09-12", null).status, "PARTIAL_PERIOD_COVERAGE");
});

test("E: CURRENT, evidence_start<=from, watermark>to, rows=0 -> NO_DATA; rows>0 -> READY", () => {
  const e = env("2026-10-05", "2026-10-05", 0, "2026-09-03", "2026-10-03", CUR);
  assert.equal(e.status, "NO_DATA");
  assert.equal(e.coverage_evidence_start_date, "2026-09-01");
  assert.equal(e.coverage_start_basis, "FIRST_LOADED_BUSINESS_DATE_CONSERVATIVE_BOUND");
  assert.equal(env("2026-10-01", "2026-10-05", 9, "2026-09-03", "2026-10-03", CUR).status, "READY");
});

test("F: CURRENT whose watermark has not passed to -> PARTIAL or SOURCE_LAGGING, never NO_DATA", () => {
  assert.equal(env("2026-10-06", "2026-10-06", 0, "2026-09-01", "2026-10-05", CUR).status, "PARTIAL_PERIOD_COVERAGE");
  assert.equal(env("2026-10-06", "2026-10-06", 0, "2026-09-01", "2026-10-04", cov("CURRENT", "2026-10-04", "2026-09-01")).status,
    "SOURCE_LAGGING");
  assert.equal(env("2026-10-01", "2026-10-06", 5, "2026-09-01", "2026-10-05", CUR).status, "PARTIAL_PERIOD_COVERAGE");
});

test("G: period before the evidence lower bound -> MISSING_SOURCE (incremental and static)", () => {
  assert.equal(env("2026-08-20", "2026-08-25", 0, "2026-09-01", "2026-10-03", CUR).status, "MISSING_SOURCE");
  assert.equal(env("2026-08-20", "2026-08-25", 0, "2026-09-01", "2026-09-12", null).status, "MISSING_SOURCE");
});

test("H: period crossing the lower bound -> PARTIAL_PERIOD_COVERAGE, with or without rows", () => {
  for (const rows of [0, 4]) assert.equal(env("2026-08-30", "2026-09-05", rows, "2026-09-01", "2026-10-03", CUR).status, "PARTIAL_PERIOD_COVERAGE");
  assert.equal(env("2026-08-30", "2026-09-05", 0, "2026-09-01", "2026-09-12", null).status, "PARTIAL_PERIOD_COVERAGE");
  assert.equal(env("2026-09-10", "2026-09-15", 0, "2026-09-01", "2026-09-12", null).status, "PARTIAL_PERIOD_COVERAGE");
});

test("I: MART coverage NO_DATA (never loaded) -> semantic MISSING_SOURCE / source_freshness NO_DATA", () => {
  const e = env("2026-10-06", "2026-10-06", 0, null, null, cov("NO_DATA", null, null));
  assert.deepEqual([e.status, e.source_freshness_status], ["MISSING_SOURCE", "NO_DATA"]);
});

test("static video source after its last loaded date -> SOURCE_LAGGING, not zero", () => {
  const e = env("2026-10-06", "2026-10-06", 0, "2026-09-01", "2026-09-12", null);
  assert.equal(e.status, "SOURCE_LAGGING");
  assert.equal(e.last_loaded_date, "2026-09-12");
  assert.equal(e.first_loaded_date, "2026-09-01");
});

test("NO_PERMISSION remains NO_PERMISSION", () => {
  assert.equal(env("2026-10-06", "2026-10-06", 0, null, null, cov("NO_PERMISSION", null, null)).status, "NO_PERMISSION");
});

test("P14-C doctrine: MIN/MAX bounds alone never yield NO_DATA or READY", () => {
  const days = ["2026-08-20", "2026-09-01", "2026-09-05", "2026-09-12", "2026-09-20"];
  for (const from of days) for (const to of days) {
    if (to < from) continue;
    for (const rows of [0, 1]) {
      const s = env(from, to, rows, "2026-09-01", "2026-09-12", null).status;
      assert.ok(s !== "NO_DATA" && s !== "READY", `${from}..${to} rows=${rows} -> ${s}`);
      for (const cs of ["SOURCE_LAGGING", "STALE", "LAST_RUN_FAILED", "NO_DATA", "API_ERROR"]) {
        const s2 = env(from, to, rows, "2026-09-01", "2026-09-12", cov(cs, "2026-10-06", "2026-09-01")).status;
        assert.ok(s2 !== "NO_DATA" && s2 !== "READY", `${cs} ${from}..${to} rows=${rows} -> ${s2}`);
      }
    }
  }
});

test("lower-bound fields: evidence start + basis exposed; no misleading 'available' names", () => {
  const e = env("2026-10-05", "2026-10-05", 0, "2026-09-03", "2026-10-03", CUR);
  assert.equal(e.coverage_start_basis, "FIRST_LOADED_BUSINESS_DATE_CONSERVATIVE_BOUND");
  for (const k of ["coverage_start_date", "earliest_available_date", "latest_available_date"]) assert.ok(!(k in e), k);
});

// --- J: product_funnel status is the coverage status -----------------------

test("J: product_funnel top-level status agrees with coverage for every envelope vector", () => {
  for (const c of V.freshness) {
    const covStatus = String(freshnessEnvelope({ ...c.in, source: "x" }).status);
    const pf = productFunnelStatus(covStatus);
    assert.equal(pf, covStatus === "READY" ? "API_ACTUAL" : covStatus, c.case);
    if (pf === "NO_DATA") assert.equal(covStatus, "NO_DATA", c.case);
  }
});

test("J: product_funnel mapping is one-to-one with coverage", () => {
  for (const [cs, pf] of V.product_funnel) assert.equal(productFunnelStatus(cs), pf);
});

test("J: empty product period + SOURCE_LAGGING coverage never shows top-level NO_DATA", () => {
  const cs = String(env("2026-10-05", "2026-10-05", 0, "2026-09-01", "2026-10-03", cov("SOURCE_LAGGING", "2026-10-04", "2026-09-01")).status);
  assert.equal(cs, "SOURCE_LAGGING");
  assert.equal(productFunnelStatus(cs), "SOURCE_LAGGING");
});

// --- D5 -------------------------------------------------------------------

test("D5: dates_with_data derived from actual rows; empty -> []", () => {
  assert.deepEqual(datesWithData(["2026-10-06", "2026-10-04"]), ["2026-10-04", "2026-10-06"]);
  assert.deepEqual(datesWithData(null), []);
});

// --- shared parity vectors (also run by the Python reference test) ----------

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
    assert.deepEqual([e.status, e.source_freshness_status], [c.status, c.source_freshness_status], c.case);
  }
});
