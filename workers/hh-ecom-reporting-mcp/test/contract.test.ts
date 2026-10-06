// P14-B - security/contract regressions over the Worker source. index.ts
// needs the Workers runtime to import, so its surface is checked as source.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MAX_DATE_RANGE_DAYS, ValidationError, validatePlatform, validateRange } from "../src/validation.ts";

const src = (f: string) => readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");
const code = (s: string) => s.replace(/^\s*\/\/.*$/gm, ""); // drop line comments
const index = src("index.ts");
const qs = src("queryService.ts");
const db = src("db.ts");

test("exactly the 7 approved tools, all read-only annotated", () => {
  const tools = [...index.matchAll(/registerTool\(\s*"(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(tools, [
    "get_ecom_overview", "get_cost_breakdown", "get_operations", "get_video_performance",
    "get_live_performance", "get_affiliate_performance", "get_data_coverage",
  ]);
  assert.equal((index.match(/readOnlyHint: true/g) ?? []).length, 7);
});

test("no arbitrary SQL tool or input, no write statements", () => {
  assert.doesNotMatch(code(index), /execute_sql|raw_sql|run_query|z\.object\(\{[^}]*\bsql\b/);
  for (const s of [qs, db]) assert.doesNotMatch(code(s), /\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\s/);
});

test("interpolated SQL fragments are code constants or $n placeholders only", () => {
  const interps = [...qs.matchAll(/\$\{([^}]+)\}/g)].map((m) => m[1].trim());
  const allowed = /^(cols|adsDetailCols|affiliateDetailCols|latestSql|params\.length \+ 1|n \+ [12]|latest(?: \?\? "never")?|f\.coverage\.watermark_at|cs)$/;
  for (const i of interps) assert.match(i, allowed, `unexpected interpolation: ${i}`);
});

test("no hardcoded September product-funnel date list remains", () => {
  assert.doesNotMatch(qs, /2026-09-04, 2026-09-08/);
});

test("invalid platform and >31-day range rejected", () => {
  assert.throws(() => validatePlatform("LAZADA; DROP TABLE x"), ValidationError);
  assert.throws(() => validateRange("2026-01-01", "2026-10-06"), ValidationError);
  assert.equal(MAX_DATE_RANGE_DAYS, 31);
});

test("auth unchanged: fail closed without token, 401 on mismatch, timing-safe compare", () => {
  assert.match(index, /if \(!env\.MCP_AUTH_TOKEN\) \{[\s\S]*?server_misconfigured[\s\S]*?status: 500/);
  assert.match(index, /if \(!timingSafeEqual\(provided, env\.MCP_AUTH_TOKEN\)\) \{\s*return Response\.json\(\{ error: "unauthorized" \}, \{ status: 401 \}\)/);
});

test("audit log carries metadata only, never connection string/token/payload", () => {
  const logged = [...db.matchAll(/log\(\{([\s\S]*?)\}\);/g)].map((m) => m[1]);
  assert.equal(logged.length, 2);
  for (const body of logged) {
    assert.doesNotMatch(body, /connectionString|HYPERDRIVE|MCP_AUTH_TOKEN|rows[,\s]|sql/);
  }
  assert.match(db, /No further detail is exposed/);
});
