// P8.2 - business/query layer. Ported 1:1 from the P8.0 Python reference
// (mcp_server/hh_ecom_reporting/query_service.py) so field names, statuses,
// and warnings match exactly - the P8.0 local MCP is the reference
// implementation and its semantics are not redesigned here.

import type { Env } from "./db";
import { runQuery } from "./db";
import {
  ValidationError,
  validateAccountType,
  validateDate,
  validateLimit,
  validatePlatform,
  validateRange,
} from "./validation";
import { CROSSWALK } from "./crosswalkData";

type Row = Record<string, unknown>;
type Logger = (entry: Record<string, unknown>) => void;

// ---------------------------------------------------------------------------
// shared cost/status envelope helpers - reused, not reimplemented per tool
// ---------------------------------------------------------------------------

const KNOWN_MISSING_COST_STATUS: Record<string, string> = {
  ads_spend: "SEPARATE_API_REQUIRED",
  booking_kol_koc: "MISSING_SOURCE",
  live_inhouse_cost: "MISSING_SOURCE",
};

const COST_LINE_LABELS: [string, string][] = [
  ["fixed_fee", "platform_fixed_fee"],
  ["service_fee", "platform_service_fee"],
  ["payment_fee", "payment_fee"],
  ["vxp_fee", "vxp_fee"],
  ["infrastructure_fee", "infrastructure_fee"],
  ["affiliate_fee", "affiliate_commission_cost"],
  ["affiliate_commission", "shopee_affiliate_commission_cost"],
  ["ads_spend", "ads_spend"],
  ["hh_internal_packaging_cost", "hh_internal_packaging_cost"],
  ["platform_packaging_or_fulfillment_fee", "platform_packaging_or_fulfillment_fee"],
  ["cancel_return_logistics_cost", "cancel_return_logistics_cost"],
  ["booking_kol_koc", "booking_kol_koc_cost"],
  ["live_inhouse_cost", "live_inhouse_cost"],
  ["backoffice_cost", "backoffice_cost"],
];

function costStatus(column: string, value: unknown): string {
  if (value !== null && value !== undefined) return "READY";
  return KNOWN_MISSING_COST_STATUS[column] ?? "MISSING_SOURCE";
}

function cm2MissingSources(row: Row): string[] {
  const missing: string[] = [];
  if (row.ads_spend === null) missing.push("TIKTOK_ADS_SEPARATE_API_REQUIRED");
  if (row.booking_kol_koc === null) missing.push("BOOKING_KOL_KOC_MISSING_SOURCE");
  if (row.live_inhouse_cost === null) missing.push("LIVE_INHOUSE_COST_MISSING_SOURCE");
  if (row.backoffice_cost === null) missing.push("BACKOFFICE_COST_RULE_NOT_APPROVED");
  return missing;
}

// ---------------------------------------------------------------------------
// TOOL 1 - get_ecom_overview
// ---------------------------------------------------------------------------

export async function getEcomOverview(
  env: Env,
  log: Logger,
  args: { from_date: string; to_date: string; platform?: string | null }
) {
  const { fromDate, toDate } = validateRange(args.from_date, args.to_date);
  const platform = validatePlatform(args.platform ?? null);

  let sql =
    "SELECT g.business_date, g.channel, g.orders, g.units, g.platform_gmv, " +
    "g.net_sales, g.net_sales_status, g.net_sales_basis, " +
    "g.total_cogs, g.gm1, g.gm1_margin, g.gm1_status, " +
    "g.cm1, g.cm1_margin, g.cm1_status, " +
    "g.cm2, g.cm2_margin, g.cm2_status, g.cm2_known, g.cm2_known_status, " +
    "g.ads_spend, g.booking_kol_koc, g.live_inhouse_cost, g.backoffice_cost, " +
    // P8.5 — Shopee Affiliate financial wiring (approved 2026-09-15).
    // Order-level precedence (settlement > AMS conversion, never
    // summed); canonical value already reflects that precedence.
    "g.affiliate_commission, g.affiliate_commission_basis, " +
    "g.affiliate_commission_settled, g.affiliate_commission_order_level, " +
    "g.profit, g.profit_margin, g.profit_status, g.data_freshness_status, " +
    // P8.5 — commerce exposure completion: cancelled/refund already in
    // mart, sold_units/gift_units derived from product-grain (proven
    // reconciled: SUM(v_ai_product_daily.total_units) = SUM(core.fact_order_item.qty)).
    // LEFT JOIN preserves the real-zero-vs-missing distinction: no
    // matching product rows -> NULL (missing), matching rows summing to
    // 0 -> real 0.
    "g.cancelled_value, g.cancelled_value_status, " +
    "g.refund_orders, g.refund_value, g.refund_value_status, " +
    "p.sold_units, p.gift_units " +
    "FROM mart.v_ceo_ecom_daily g " +
    "LEFT JOIN (SELECT business_date, channel, SUM(sold_units) AS sold_units, SUM(gift_units) AS gift_units " +
    "FROM mart.v_ai_product_daily GROUP BY 1,2) p ON p.business_date = g.business_date AND p.channel = g.channel " +
    "WHERE g.business_date BETWEEN $1 AND $2";
  const params: unknown[] = [fromDate, toDate];
  if (platform) {
    sql += " AND g.channel = $3";
    params.push(platform);
  }
  sql += " ORDER BY g.business_date, g.channel";

  const rows = await runQuery(env, "get_ecom_overview", sql, params,
    { from_date: fromDate, to_date: toDate, platform }, log);

  // P8.5 — real TikTok shop-visitor data (proven live via
  // performance_per_hour, scope data.shop_analytics.public.read).
  // TikTok only — Shopee exposes no equivalent traffic endpoint.
  // 'visitors' is TikTok's own field name, never conflated with
  // impressions/clicks/views/sessions.
  let trafficByDate: Map<string, Record<string, unknown>> = new Map();
  if (!platform || platform === "TIKTOK") {
    const trafficRows = await runQuery(
      env, "get_ecom_overview_traffic",
      "SELECT business_date, visitors, customers, gmv_amount, gmv_currency, items_sold, value_basis " +
        "FROM mart.v_ai_shop_traffic_daily WHERE channel = 'TIKTOK' AND business_date BETWEEN $1 AND $2",
      [fromDate, toDate], { from_date: fromDate, to_date: toDate }, log
    );
    trafficByDate = new Map(trafficRows.map((t) => [String(t.business_date), t]));
  }

  const out = rows.map((r) => {
    const cm2Missing = cm2MissingSources(r);
    const traffic = r.channel === "TIKTOK" ? trafficByDate.get(String(r.business_date)) : undefined;
    return {
      platform: r.channel,
      business_date: r.business_date,
      orders: r.orders,
      units: r.units,
      platform_gmv: r.platform_gmv,
      shop_traffic: r.channel === "TIKTOK"
        ? (traffic
            ? { visitors: traffic.visitors, customers: traffic.customers, gmv_amount: traffic.gmv_amount,
                gmv_currency: traffic.gmv_currency, items_sold: traffic.items_sold,
                value_basis: traffic.value_basis, status: "API_ACTUAL" }
            : { visitors: null, status: "MISSING_SOURCE" })
        : { visitors: null, status: "NOT_EXPOSED_PUBLIC_API" },
      net_sales: { value: r.net_sales, status: r.net_sales_status, value_basis: r.net_sales_basis },
      cogs: { value: r.total_cogs, status: r.total_cogs !== null ? "READY" : "MISSING_SOURCE" },
      gm1: { value: r.gm1, status: r.gm1_status },
      gm1_margin_pct: r.gm1_margin,
      cm1: { value: r.cm1, status: r.cm1_status },
      cm1_margin_pct: r.cm1_margin,
      cm2: {
        value: r.cm2, status: r.cm2_status, missing_sources: cm2Missing.length ? cm2Missing : null,
        note: r.cm2_status === "PARTIAL_MISSING_INTERNAL_MARKETING_SOURCE"
          ? "cm2 value is the fully-known contribution as of today (Ads + Affiliate deducted); it is not final because Booking/KOL/KOC and Live/internal marketing costs have no approved source yet — see cm2_known for the same figure under its unambiguous name."
          : r.cm2_status === "PARTIAL_MISSING_TIKTOK_ADS_AND_INTERNAL_MARKETING"
          ? "cm2 value is the fully-known contribution as of today (Affiliate deducted; TikTok Ads is NOT deducted — source unavailable, BLOCKED_BY_DEVELOPER_PROFILE, never invented as 0); it is not final because TikTok Ads, Booking/KOL/KOC, and Live/internal marketing costs have no approved source yet — see cm2_known for the same figure under its unambiguous name."
          : null,
      },
      cm2_margin_pct: r.cm2_margin,
      // P8.5 — Shopee Affiliate + CM2 bridge (approved 2026-09-15).
      // cm2_known is numerically identical to cm2 whenever both are
      // computable — exposed under its own explicit name so an agent
      // can say "CM2 known is X, but final CM2 is still missing Y"
      // instead of treating a partial figure as final, or omitting it.
      cm2_known: { value: r.cm2_known, status: r.cm2_known_status },
      affiliate_commission: {
        value: r.affiliate_commission, status: r.affiliate_commission_basis,
        settled_component: r.affiliate_commission_settled,
        order_level_component: r.affiliate_commission_order_level,
        note: "Order-level precedence: settlement order_ams_commission_fee when non-zero, else AMS conversion commission for that order — never summed. Kept separate from platform/service/payment fees.",
      },
      profit: { value: r.profit, status: r.profit_status },
      profit_margin_pct: r.profit_margin,
      // P8.5 — commerce exposure completion.
      sold_units: r.sold_units,
      sold_units_status: r.sold_units !== null && r.sold_units !== undefined ? "DERIVED_VERIFIED" : "MISSING_SOURCE",
      gift_units: r.gift_units,
      cancelled_value: { value: r.cancelled_value, status: r.cancelled_value_status },
      refund_orders: r.refund_orders,
      refund_value: { value: r.refund_value, status: r.refund_value_status },
      data_freshness_status: r.data_freshness_status,
      source: "mart.v_ceo_ecom_daily",
    };
  });

  // P8.5 — traffic-only date fix. mart.v_ceo_ecom_daily is anchored on
  // mart.gold_channel_daily (built from core.fact_order), so a TikTok date
  // with 0 orders produces NO row there at all — even though real
  // shop-traffic data (visitors/customers) can still exist for that same
  // date (e.g. 2026-09-15: 0 orders but 1,175 real visitors, confirmed
  // live this pass). Without this, get_ecom_overview silently drops a
  // date an agent would reasonably expect to see. Add a synthetic
  // traffic-only row for any such date instead of losing it.
  const seenTikTokDates = new Set(out.filter((o) => o.platform === "TIKTOK").map((o) => String(o.business_date)));
  for (const [date, traffic] of trafficByDate) {
    if (seenTikTokDates.has(date)) continue;
    out.push({
      platform: "TIKTOK",
      business_date: date,
      orders: null, units: null, platform_gmv: null,
      shop_traffic: { visitors: traffic.visitors, customers: traffic.customers, gmv_amount: traffic.gmv_amount,
        gmv_currency: traffic.gmv_currency, items_sold: traffic.items_sold,
        value_basis: traffic.value_basis, status: "API_ACTUAL" },
      net_sales: { value: null, status: "NO_ORDERS_NO_GOLD_ROW", value_basis: null },
      cogs: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" },
      gm1: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" }, gm1_margin_pct: null,
      cm1: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" }, cm1_margin_pct: null,
      cm2: { value: null, status: "NO_ORDERS_NO_GOLD_ROW", missing_sources: null, note: null }, cm2_margin_pct: null,
      cm2_known: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" },
      affiliate_commission: { value: null, status: "NO_ORDERS_NO_GOLD_ROW", settled_component: null, order_level_component: null, note: "no Gold/commerce row exists for this date" },
      profit: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" }, profit_margin_pct: null,
      sold_units: null, sold_units_status: "NO_ORDERS_NO_GOLD_ROW", gift_units: null,
      cancelled_value: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" },
      refund_orders: null, refund_value: { value: null, status: "NO_ORDERS_NO_GOLD_ROW" },
      data_freshness_status: "FRESH",
      source: "mart.v_ai_shop_traffic_daily (traffic-only date — no Gold/commerce row exists for this date)",
    });
  }
  out.sort((a, b) => (String(a.business_date) < String(b.business_date) ? -1 : String(a.business_date) > String(b.business_date) ? 1 : String(a.platform).localeCompare(String(b.platform))));

  // P8.5 — period-level AOV/ASP. Formula is SUM(numerator)/SUM(denominator)
  // over the requested range, NEVER an average of daily ratios. Computed
  // per platform from the rows already fetched above (no extra query).
  // A NULL component (e.g. TikTok net_sales withheld by the settlement-
  // completeness guard) is skipped by summation like SQL SUM() would,
  // and the period is flagged PARTIAL_PERIOD_COVERAGE so a NULL-skip is
  // never silently indistinguishable from full coverage.
  type PeriodAcc = {
    netSales: number; netSalesAny: boolean; netSalesNullDates: number;
    platformGmv: number; platformGmvAny: boolean;
    orders: number; ordersAny: boolean;
    soldUnits: number; soldUnitsAny: boolean; soldUnitsNullDates: number;
  };
  const mkAcc = (): PeriodAcc => ({
    netSales: 0, netSalesAny: false, netSalesNullDates: 0,
    platformGmv: 0, platformGmvAny: false,
    orders: 0, ordersAny: false,
    soldUnits: 0, soldUnitsAny: false, soldUnitsNullDates: 0,
  });
  const acc: Record<string, PeriodAcc> = {};
  for (const r of rows) {
    const ch = String(r.channel);
    if (!acc[ch]) acc[ch] = mkAcc();
    const a = acc[ch];
    if (r.net_sales !== null && r.net_sales !== undefined) { a.netSales += Number(r.net_sales); a.netSalesAny = true; }
    else a.netSalesNullDates += 1;
    if (r.platform_gmv !== null && r.platform_gmv !== undefined) { a.platformGmv += Number(r.platform_gmv); a.platformGmvAny = true; }
    if (r.orders !== null && r.orders !== undefined) { a.orders += Number(r.orders); a.ordersAny = true; }
    if (r.sold_units !== null && r.sold_units !== undefined) { a.soldUnits += Number(r.sold_units); a.soldUnitsAny = true; }
    else a.soldUnitsNullDates += 1;
  }
  const div = (num: number, den: number): number | null => (den !== 0 ? num / den : null);
  const period_kpis: Row = {};
  for (const [ch, a] of Object.entries(acc)) {
    const netSalesComplete = a.netSalesNullDates === 0;
    const soldUnitsComplete = a.soldUnitsNullDates === 0;
    period_kpis[ch] = {
      aov_net_sales: {
        value: a.ordersAny ? div(a.netSales, a.orders) : null,
        status: !a.netSalesAny || !a.ordersAny ? "MISSING_SOURCE" : (netSalesComplete ? "READY" : "PARTIAL_PERIOD_COVERAGE"),
      },
      aov_platform_gmv: {
        value: a.ordersAny ? div(a.platformGmv, a.orders) : null,
        status: !a.platformGmvAny || !a.ordersAny ? "MISSING_SOURCE" : "READY",
      },
      asp_net_sales: {
        value: a.soldUnitsAny ? div(a.netSales, a.soldUnits) : null,
        status: !a.netSalesAny || !a.soldUnitsAny ? "MISSING_SOURCE" : (netSalesComplete && soldUnitsComplete ? "READY" : "PARTIAL_PERIOD_COVERAGE"),
      },
      asp_platform_gmv: {
        value: a.soldUnitsAny ? div(a.platformGmv, a.soldUnits) : null,
        status: !a.platformGmvAny || !a.soldUnitsAny ? "MISSING_SOURCE" : (soldUnitsComplete ? "READY" : "PARTIAL_PERIOD_COVERAGE"),
      },
      formula_basis: "SUM(numerator)/SUM(denominator) over the requested date range — never an average of daily ratios",
    };
  }

  // P8.5 — TikTok product-funnel exposure (approved 2026-09-15).
  // Real source: core.fact_product_analytics_daily via
  // mart.v_ai_product_traffic_daily (sql/052). SHOPEE has no equivalent
  // public API — never substituted, explicitly NOT_EXPOSED_PUBLIC_API.
  let product_funnel: Row;
  if (platform === "SHOPEE") {
    product_funnel = { status: "NOT_EXPOSED_PUBLIC_API", note: "Shopee's public API exposes no organic/paid product-page funnel outside Ads (see P8_5_API_ENDPOINT_FIELD_MATRIX.csv)." };
  } else {
    const pfRows = await runQuery(
      env, "get_ecom_overview_product_funnel",
      "SELECT product_id, hh_sku, ean, " +
        "sum(impressions) AS impressions, sum(clicks) AS clicks, sum(attributed_orders) AS attributed_orders, " +
        "sum(items_sold) AS items_sold, sum(gmv_amount) AS gmv_amount " +
        "FROM mart.v_ai_product_traffic_daily WHERE channel = 'TIKTOK' AND business_date BETWEEN $1 AND $2 " +
        "GROUP BY product_id, hh_sku, ean",
      [fromDate, toDate], { from_date: fromDate, to_date: toDate }, log
    );
    type ProductAgg = { product_id: unknown; hh_sku: unknown; ean: unknown; impressions: number; clicks: number; attributed_orders: number; items_sold: unknown; gmv_amount: unknown; ctr: number | null; click_to_order: number | null };
    const products: ProductAgg[] = pfRows.map((r) => {
      const impressions = Number(r.impressions ?? 0);
      const clicks = Number(r.clicks ?? 0);
      const attributed_orders = Number(r.attributed_orders ?? 0);
      return {
        product_id: r.product_id, hh_sku: r.hh_sku, ean: r.ean,
        impressions, clicks, attributed_orders, items_sold: r.items_sold, gmv_amount: r.gmv_amount,
        ctr: impressions > 0 ? Math.round((clicks / impressions) * 10000) / 10000 : null,
        click_to_order: clicks > 0 ? Math.round((attributed_orders / clicks) * 10000) / 10000 : null,
      };
    });
    // materiality = median SUM(impressions)/SUM(clicks) among products
    // with a nonzero value, so "material-impression"/"material-click"
    // is grounded in this period's own distribution, not a guessed
    // constant. No AVG of row-level ratios anywhere in this block —
    // every ctr/click_to_order above is SUM/SUM.
    const median = (nums: number[]): number => {
      if (!nums.length) return 0;
      const s = [...nums].sort((a, b) => a - b);
      const mid = Math.floor(s.length / 2);
      return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
    };
    const impressionMateriality = median(products.filter((p) => p.impressions > 0).map((p) => p.impressions));
    const clickMateriality = median(products.filter((p) => p.clicks > 0).map((p) => p.clicks));
    const top = (arr: ProductAgg[], key: "impressions" | "clicks", n = 10) =>
      [...arr].sort((a, b) => b[key] - a[key]).slice(0, n);
    const lowest = (arr: ProductAgg[], key: "ctr" | "click_to_order", n = 10) =>
      [...arr].filter((p) => p[key] !== null).sort((a, b) => (a[key] as number) - (b[key] as number)).slice(0, n);
    product_funnel = {
      status: products.length ? "API_ACTUAL" : "NO_DATA",
      dates_with_data: "2026-09-04, 2026-09-08, 2026-09-10, 2026-09-11, 2026-09-14 only (real, sparse, non-contiguous — never fabricated as continuous)",
      materiality_basis: "median SUM(impressions)/SUM(clicks) across products with a nonzero value in the requested range",
      impression_materiality_floor: impressionMateriality,
      click_materiality_floor: clickMateriality,
      top_impressions: top(products, "impressions"),
      top_clicks: top(products, "clicks"),
      low_ctr_material_impressions: lowest(products.filter((p) => p.impressions >= impressionMateriality), "ctr"),
      low_click_to_order_material_clicks: lowest(products.filter((p) => p.clicks >= clickMateriality), "click_to_order"),
      raw_primitives: products,
      source: "mart.v_ai_product_traffic_daily",
    };
  }

  return { rows: out, row_count: out.length, date_range: [fromDate, toDate], platform_filter: platform, period_kpis, product_funnel };
}

// ---------------------------------------------------------------------------
// TOOL 2 - get_cost_breakdown
// ---------------------------------------------------------------------------

export async function getCostBreakdown(
  env: Env,
  log: Logger,
  args: { from_date: string; to_date: string; platform?: string | null }
) {
  const { fromDate, toDate } = validateRange(args.from_date, args.to_date);
  const platform = validatePlatform(args.platform ?? null);

  const cols = COST_LINE_LABELS.map(([c]) => c).join(", ");
  // P8.5 ads-attribution fix — Shopee ads_spend is now single-counted
  // (see mart.gold_metric_ownership / _p6a_gold_build.py). These extra
  // columns are attached only to the ads_spend row below, never treated
  // as their own cost lines (impressions/clicks/orders/gmv are not costs).
  const adsDetailCols =
    "ads_spend_status, ads_impressions, ads_clicks, ads_ctr, ads_cpc, ads_cpm, " +
    "ads_orders_broad, ads_gmv_broad, ads_cvr_broad, ads_roas_broad, " +
    "ads_orders_direct, ads_gmv_direct, ads_cvr_direct, ads_roas_direct";
  // P8.5 — Shopee Affiliate financial wiring (approved 2026-09-15).
  // Attached only to the shopee_affiliate_commission_cost row below.
  // cm2_known is attached to every row (not order/attribution detail,
  // but the CM2-bridge context a cost breakdown consumer needs).
  const affiliateDetailCols =
    "affiliate_commission_basis, affiliate_commission_settled, affiliate_commission_order_level, " +
    "cm2_known, cm2_known_status, cm2_status";
  let sql = `SELECT business_date, channel, ${cols}, ${adsDetailCols}, ${affiliateDetailCols} FROM mart.v_ceo_ecom_daily WHERE business_date BETWEEN $1 AND $2`;
  const params: unknown[] = [fromDate, toDate];
  if (platform) {
    sql += " AND channel = $3";
    params.push(platform);
  }
  sql += " ORDER BY business_date, channel";

  const rows = await runQuery(env, "get_cost_breakdown", sql, params,
    { from_date: fromDate, to_date: toDate, platform }, log);

  const out: Row[] = [];
  for (const r of rows) {
    for (const [column, costName] of COST_LINE_LABELS) {
      const value = r[column];
      const entry: Row = {
        cost_name: costName,
        amount: value,
        amount_basis: value !== null && value !== undefined ? "VND, per-day per-channel" : null,
        platform: r.channel,
        business_date: r.business_date,
        source: "mart.v_ceo_ecom_daily",
        status: costStatus(column, value),
        value_basis: value !== null && value !== undefined ? "DERIVED" : null,
      };
      // Shopee ads_spend: attach delivery + attribution-split detail.
      // Common delivery metrics (impressions/clicks) counted ONCE;
      // orders/gmv kept split by attribution basis, never summed —
      // see P8_5_CANONICAL_BUSINESS_RULES.md.
      if (column === "ads_spend" && r.channel === "SHOPEE") {
        entry.ads_detail = {
          impressions: r.ads_impressions,
          clicks: r.ads_clicks,
          ctr: r.ads_ctr,
          cpc: r.ads_cpc,
          cpm: r.ads_cpm,
          broad: { orders: r.ads_orders_broad, gmv: r.ads_gmv_broad, cvr: r.ads_cvr_broad, roas: r.ads_roas_broad },
          direct: { orders: r.ads_orders_direct, gmv: r.ads_gmv_direct, cvr: r.ads_cvr_direct, roas: r.ads_roas_direct },
          note: "broad and direct never summed — distinct attribution bases sharing the same delivery metrics above",
        };
      }
      // P8.5 — Shopee Affiliate financial wiring (approved 2026-09-15):
      // order-level precedence (settlement > AMS conversion, never
      // summed). status here IS the basis (SETTLEMENT_ACTUAL /
      // ORDER_LEVEL_ACTUAL / MIXED_SETTLEMENT_AND_ORDER_LEVEL_ACTUAL /
      // MISSING_SOURCE) rather than a plain READY/MISSING_SOURCE, so it
      // carries provenance directly on the cost line.
      if (column === "affiliate_commission" && r.channel === "SHOPEE") {
        entry.status = r.affiliate_commission_basis ?? "MISSING_SOURCE";
        entry.affiliate_detail = {
          settled_component: r.affiliate_commission_settled,
          order_level_component: r.affiliate_commission_order_level,
          note: "settled_component uses core.fact_settlement.order_ams_commission_fee (non-zero only); order_level_component is the AMS-conversion fallback for orders not yet settled — the cost_name amount above is the order-level-precedence CANONICAL total, never their sum.",
        };
        // CM2-bridge context lives here, next to the cost line it feeds —
        // see get_ecom_overview.cm2_known for the canonical per-date value.
        entry.cm2_known = { value: r.cm2_known, status: r.cm2_known_status };
        entry.cm2_status = r.cm2_status;
      }
      out.push(entry);
    }
  }
  return {
    rows: out, row_count: out.length, date_range: [fromDate, toDate], platform_filter: platform,
    note: "amount=NULL always means the real status column explains why — never a fabricated zero.",
  };
}

// ---------------------------------------------------------------------------
// TOOL 3 - get_operations (Shopee Account Health; snapshot/current-state only)
// ---------------------------------------------------------------------------

const METRIC_ID_LABELS: Record<number, string> = {
  1: "Late Shipment Rate", 3: "Non-Fulfilment Rate", 4: "Preparation Time",
  11: "Chat Response Rate", 12: "Pre-order Listing Rate", 15: "Pre-order Listing Count",
  21: "Response Time", 22: "Shop Rating", 23: "Non-Responded Chats", 25: "Fast Handover Rate",
  29: "Average Response Time", 42: "Cancellation Rate", 43: "Return-refund Rate",
  52: "Severe Listing Violations", 53: "Other Listing Violations", 54: "Prohibited Listings",
  55: "Counterfeit/IP Infringement", 56: "Spam Listings", 2011: "PQR Products",
};
// Metric IDs the task lists as wanted but confirmed ABSENT from this shop's
// real metric_list in P7.1 - reported honestly as MISSING_SOURCE, never
// silently dropped or set to zero.
const EXPECTED_BUT_ABSENT_IDS = new Set([21, 23, 25, 29]);

// P9.1 — inventory exposure (additive, get_operations extension).
// mart.v_ai_inventory sources core.fact_inventory_snapshot for BOTH
// channels but carries no channel/platform column — a SKU listed on
// both Shopee and TikTok appears as 2 separate rows at the same
// snapshot_date, undifferentiated by channel. Aggregates below sum
// across whatever rows exist (real platform-reported available
// quantities), never deduplicated or channel-split, since the source
// has no basis to do either. lot/expiry_date are always NULL in the
// current source (0 of 3,886 rows populated) — never fabricated.
// data_status passes through the view's own real caveat string
// verbatim ('SINGLE_SNAPSHOT_ONLY_NOT_DAILY_PRODUCTION_READY').
async function buildInventoryBlock(env: Env, log: Logger): Promise<Row> {
  const latestRows = await runQuery(
    env, "get_operations_inventory_latest",
    "SELECT snapshot_date, warehouse, hh_sku, ean, product_name, lot, expiry_date, stock_qty, data_status " +
      "FROM mart.v_ai_inventory WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM mart.v_ai_inventory) " +
      "ORDER BY hh_sku",
    [], {}, log
  );
  if (!latestRows.length) {
    return { status: "MISSING_SOURCE", note: "mart.v_ai_inventory returned no rows." };
  }
  const snapshot_date = latestRows[0].snapshot_date;
  const data_status = latestRows[0].data_status;
  const skuSet = new Set(latestRows.map((r) => r.hh_sku));
  const totalStock = latestRows.reduce((sum, r) => sum + (r.stock_qty !== null ? Number(r.stock_qty) : 0), 0);
  const zeroStock = latestRows.filter((r) => Number(r.stock_qty) === 0);
  const lowStock = [...latestRows]
    .filter((r) => r.stock_qty !== null && Number(r.stock_qty) > 0)
    .sort((a, b) => Number(a.stock_qty) - Number(b.stock_qty))
    .slice(0, 15)
    .map((r) => ({ hh_sku: r.hh_sku, product_name: r.product_name, warehouse: r.warehouse, stock_qty: r.stock_qty }));
  const withExpiry = latestRows.filter((r) => r.expiry_date !== null);

  return {
    snapshot_date,
    data_status,
    total_sku_count: skuSet.size,
    total_row_count: latestRows.length,
    total_stock_quantity: totalStock,
    zero_stock_sku_count: zeroStock.length,
    zero_stock_skus: zeroStock.slice(0, 30).map((r) => ({ hh_sku: r.hh_sku, product_name: r.product_name, warehouse: r.warehouse })),
    low_stock_ranking: lowStock,
    earliest_expiry_items: withExpiry.length
      ? withExpiry.sort((a, b) => String(a.expiry_date).localeCompare(String(b.expiry_date))).slice(0, 15)
          .map((r) => ({ hh_sku: r.hh_sku, product_name: r.product_name, expiry_date: r.expiry_date, stock_qty: r.stock_qty }))
      : [],
    earliest_expiry_status: withExpiry.length ? "READY" : "MISSING_SOURCE",
    note: "warehouse column is always 'ALL' in the current source (no real per-warehouse split exists). " +
      "This view carries no channel/platform column — a SKU sold on both Shopee and TikTok appears as " +
      "2 separate rows at the same snapshot_date; total_stock_quantity sums all rows as-is, not " +
      "deduplicated by SKU. reserved/available-split, days-of-supply, and reorder quantity are NOT " +
      "computed here — no approved source/formula exists for them. lot/expiry_date are real columns " +
      "but always NULL in the current source (0 of 3,886 rows populated as of this release) — " +
      "earliest_expiry_items is correctly empty, not fabricated.",
    source: "mart.v_ai_inventory",
  };
}

export async function getOperations(
  env: Env,
  log: Logger,
  args: { platform: string; date?: string | null }
) {
  const platform = validatePlatform(args.platform);
  const inventory = await buildInventoryBlock(env, log);
  if (platform !== "SHOPEE") {
    return {
      rows: [], row_count: 0, platform, inventory,
      note: "Account Health / operational KPIs are only proven for SHOPEE this phase. " +
        "TikTok has no equivalent violation/operations API (see get_data_coverage). " +
        "The inventory block above is channel-independent and always populated regardless of platform.",
    };
  }
  const date = args.date ? validateDate("date", args.date) : null;

  let sql: string;
  let params: unknown[];
  if (date) {
    sql =
      "SELECT shop_id, snapshot_date, metric_id, metric_name, current_period, last_period, " +
      "unit, target_value, target_comparator, overall_shop_rating, value_basis, last_updated_at " +
      "FROM mart.v_ai_operations_daily WHERE snapshot_date = $1 ORDER BY metric_id";
    params = [date];
  } else {
    // latest snapshot: every metric row for the single most recent
    // snapshot_date, not an arbitrary single row (no LIMIT 1 trap).
    sql =
      "SELECT shop_id, snapshot_date, metric_id, metric_name, current_period, last_period, " +
      "unit, target_value, target_comparator, overall_shop_rating, value_basis, last_updated_at " +
      "FROM mart.v_ai_operations_daily WHERE snapshot_date = (" +
      "SELECT MAX(snapshot_date) FROM mart.v_ai_operations_daily) ORDER BY metric_id";
    params = [];
  }

  const rows = await runQuery(env, "get_operations", sql, params, { platform, date }, log);

  const out: Row[] = [];
  const seenIds = new Set<number>();
  for (const r of rows) {
    const metricId = r.metric_id as number;
    seenIds.add(metricId);
    // Every row here was actually returned by Shopee's account-health API
    // for this snapshot - that is API_ACTUAL regardless of whether the
    // value itself is null. A null current_period on a metric Shopee DID
    // return (e.g. listing-violation counts) is a real "nothing to
    // report" value proven in P7.1, not a missing source.
    out.push({
      metric: METRIC_ID_LABELS[metricId] ?? r.metric_name,
      value: r.current_period,
      unit: r.unit,
      snapshot_date: r.snapshot_date,
      source: "mart.v_ai_operations_daily (Shopee get_shop_performance)",
      status: "API_ACTUAL",
      blocking_reason: r.current_period !== null
        ? null
        : "Shopee returned this metric with a null value this period (a real reported " +
          "state, e.g. zero violations - not fabricated, not a missing source).",
    });
  }
  for (const mid of EXPECTED_BUT_ABSENT_IDS) {
    if (seenIds.has(mid)) continue;
    out.push({
      metric: METRIC_ID_LABELS[mid], value: null, unit: null,
      snapshot_date: rows.length ? rows[0].snapshot_date : date,
      source: "mart.v_ai_operations_daily (Shopee get_shop_performance)",
      status: "MISSING_SOURCE",
      blocking_reason: "Not returned by Shopee's account-health API for this shop as of the last snapshot (confirmed absent in P7.1's live probe).",
    });
  }
  return {
    rows: out, row_count: out.length, platform, inventory,
    note: "Account Health is snapshot/current-state data only - Shopee's API has no historical " +
      "backfill (proven in P7.1: identical response with/without a date filter). Do not " +
      "treat this as a time series before the date this pilot's snapshots began.",
  };
}

// ---------------------------------------------------------------------------
// TOOL 4 - get_video_performance
// ---------------------------------------------------------------------------

export async function getVideoPerformance(
  env: Env,
  log: Logger,
  args: { from_date: string; to_date: string; platform: string; account_type?: string | null; limit?: number | null }
) {
  const { fromDate, toDate } = validateRange(args.from_date, args.to_date);
  const platform = validatePlatform(args.platform);
  const accountType = validateAccountType(args.account_type ?? null);
  const limit = validateLimit(args.limit ?? null);

  if (platform !== "TIKTOK") {
    return {
      rows: [], row_count: 0, platform,
      note: "Video-grain performance is only proven for TIKTOK this phase.",
    };
  }

  let sql =
    "SELECT channel, video_id, business_date, account_type, " +
    "creator_username, creator_nick_name, creator_author_type, title, " +
    "views, sku_orders, items_sold, click_through_rate, gmv_amount, gmv_currency, " +
    "value_basis, last_updated_at " +
    "FROM mart.v_ai_video_daily WHERE business_date BETWEEN $1 AND $2 AND channel = $3";
  const params: unknown[] = [fromDate, toDate, platform];
  if (accountType) {
    sql += ` AND account_type = $${params.length + 1}`;
    params.push(accountType);
  }
  sql += ` ORDER BY gmv_amount DESC NULLS LAST LIMIT $${params.length + 1}`;
  params.push(limit);

  const rows = await runQuery(env, "get_video_performance", sql, params,
    { from_date: fromDate, to_date: toDate, platform, account_type: accountType, limit }, log);

  const out = rows.map((r) => ({
    video_id: r.video_id, business_date: r.business_date, account_type: r.account_type,
    creator: r.creator_username || r.creator_nick_name, creator_type: r.creator_author_type,
    title: r.title, views: r.views, orders: r.sku_orders, items_sold: r.items_sold,
    ctr: r.click_through_rate, gmv: r.gmv_amount, gmv_currency: r.gmv_currency,
    status: "API_ACTUAL", value_basis: r.value_basis, source: "mart.v_ai_video_daily",
  }));
  return {
    rows: out, row_count: out.length, date_range: [fromDate, toDate], platform,
    account_type_filter: accountType,
    note: "product_impressions/product_clicks are proven NOT available at this per-video grain " +
      "(confirmed absent in P7.1's real payload) and are deliberately not attached here. " +
      "account_type='ALL' vs 'AFFILIATE_ACCOUNTS' are separate slices — never sum across them.",
  };
}

// ---------------------------------------------------------------------------
// TOOL 5 - get_live_performance
// ---------------------------------------------------------------------------

export async function getLivePerformance(
  env: Env,
  log: Logger,
  args: { from_date: string; to_date: string; account_type?: string | null; limit?: number | null }
) {
  const { fromDate, toDate } = validateRange(args.from_date, args.to_date);
  const accountType = validateAccountType(args.account_type ?? null) ?? "ALL"; // default total slice
  const limit = validateLimit(args.limit ?? null);

  const sql =
    "SELECT channel, live_id, business_date, account_type, username, title, " +
    "duration_seconds, viewers, views, sku_orders, items_sold, customers, " +
    "click_through_rate, click_to_order_rate, avg_viewing_duration_secs, " +
    "likes, comments, shares, gmv_amount, gmv_currency, value_basis, last_updated_at, " +
    // P8.5 — these columns already exist in mart.v_ai_live_daily but
    // were never selected here; real data, not a source gap.
    "product_impressions, product_clicks " +
    "FROM mart.v_ai_live_daily " +
    "WHERE business_date BETWEEN $1 AND $2 AND channel = 'TIKTOK' AND account_type = $3 " +
    "ORDER BY gmv_amount DESC NULLS LAST LIMIT $4";
  const params = [fromDate, toDate, accountType, limit];

  const rows = await runQuery(env, "get_live_performance", sql, params,
    { from_date: fromDate, to_date: toDate, account_type: accountType, limit }, log);

  const out = rows.map((r) => ({
    live_id: r.live_id, business_date: r.business_date, account_type: r.account_type,
    host_username: r.username, title: r.title, duration_seconds: r.duration_seconds,
    viewers: r.viewers, views: r.views, orders: r.sku_orders, items_sold: r.items_sold,
    customers: r.customers, ctr: r.click_through_rate, ctor: r.click_to_order_rate,
    product_impressions: r.product_impressions, product_clicks: r.product_clicks,
    avg_viewing_duration_secs: r.avg_viewing_duration_secs,
    likes: r.likes, comments: r.comments, shares: r.shares,
    gmv: r.gmv_amount, gmv_currency: r.gmv_currency,
    status: "API_ACTUAL", value_basis: r.value_basis, source: "mart.v_ai_live_daily",
  }));
  return {
    rows: out, row_count: out.length, date_range: [fromDate, toDate], account_type: accountType,
    shopee_live_status: "NO_PERMISSION — Shopee LIVE has no data here; do not substitute AMS content metrics for it.",
    double_count_rule: "account_type='ALL' is the authoritative total slice. 'AFFILIATE_ACCOUNTS' is a " +
      "SUBSET of the same sessions, never an addition. Never sum ALL + AFFILIATE_ACCOUNTS.",
  };
}

// ---------------------------------------------------------------------------
// TOOL 6 - get_affiliate_performance
// ---------------------------------------------------------------------------

export async function getAffiliatePerformance(
  env: Env,
  log: Logger,
  args: { from_date: string; to_date: string; platform: string; limit?: number | null }
) {
  const { fromDate, toDate } = validateRange(args.from_date, args.to_date);
  const platform = validatePlatform(args.platform);
  const limit = validateLimit(args.limit ?? null);

  if (platform !== "SHOPEE") {
    return {
      rows: [], row_count: 0, platform,
      note: "No dedicated TikTok affiliate creator/channel view exists in the approved mart " +
        "layer. TikTok's affiliate performance is exposed at the video/LIVE content grain " +
        "instead - call get_video_performance or get_live_performance with " +
        "account_type='AFFILIATE_ACCOUNTS' for TikTok affiliate results. This is a real " +
        "data-topology fact, not an omission.",
    };
  }

  const sql =
    "SELECT business_date, channel, affiliate_id, affiliate_name, affiliate_username, " +
    "sales, items_sold, orders, clicks, est_commission, roi, total_buyers, new_buyers, " +
    "value_basis, source_updated_at " +
    "FROM mart.v_ai_affiliate_creator_daily " +
    "WHERE business_date BETWEEN $1 AND $2 AND channel = $3 " +
    "ORDER BY sales DESC NULLS LAST LIMIT $4";
  const params = [fromDate, toDate, platform, limit];

  const rows = await runQuery(env, "get_affiliate_performance", sql, params,
    { from_date: fromDate, to_date: toDate, platform, limit }, log);

  const out = rows.map((r) => ({
    affiliate: r.affiliate_name || r.affiliate_username, business_date: r.business_date,
    sales_affiliate_gmv: r.sales, orders: r.orders, items_sold: r.items_sold,
    clicks: r.clicks, commission: r.est_commission, roi: r.roi,
    total_buyers: r.total_buyers, new_buyers: r.new_buyers, views: null,
    status: "ESTIMATED_PERFORMANCE", value_basis: r.value_basis,
    source: "mart.v_ai_affiliate_creator_daily",
  }));
  return {
    rows: out, row_count: out.length, date_range: [fromDate, toDate], platform,
    note: "Affiliate GMV (sales_affiliate_gmv) is kept separate from Net Sales — never add it " +
      "into a Net Sales total. 'views' is not available at this grain (Shopee AMS creator " +
      "performance has no view-count field); left NULL, not fabricated.",
  };
}

// ---------------------------------------------------------------------------
// TOOL 7 - get_data_coverage
// ---------------------------------------------------------------------------

export async function getDataCoverage(
  env: Env,
  log: Logger,
  args: { from_date?: string | null; to_date?: string | null; platform?: string | null }
) {
  let fromDate: string | null = null;
  let toDate: string | null = null;
  if (args.from_date || args.to_date) {
    if (!(args.from_date && args.to_date)) {
      throw new ValidationError("from_date and to_date must be supplied together");
    }
    ({ fromDate, toDate } = validateRange(args.from_date, args.to_date));
  }
  const platform = validatePlatform(args.platform ?? null);

  let kpiRows = CROSSWALK.filter((r) => r.STATUS !== "EMPTY_IN_SOURCE" && r.STATUS !== "TARGET_THRESHOLD_NOT_A_KPI");
  if (platform) {
    kpiRows = kpiRows.filter(
      (r) => r.PLATFORM === platform || r.PLATFORM === "SHOPEE, TIKTOK" || r.PLATFORM === "N/A" || r.PLATFORM.includes(platform)
    );
  }

  const tally: Record<string, number> = {};
  for (const r of kpiRows) tally[r.STATUS] = (tally[r.STATUS] ?? 0) + 1;

  let liveMetricRows: Row[] = [];
  if (fromDate && toDate) {
    let sql = "SELECT metric_name, channel, business_date, availability_status, value_basis, note FROM mart.v_ai_metric_status WHERE business_date BETWEEN $1 AND $2";
    const params: unknown[] = [fromDate, toDate];
    if (platform) {
      sql += " AND channel = $3";
      params.push(platform);
    }
    sql += " LIMIT 500";
    liveMetricRows = await runQuery(env, "get_data_coverage", sql, params,
      { from_date: fromDate, to_date: toDate, platform }, log);
  }

  return {
    kpi_universe_total: kpiRows.length,
    kpi_status_tally: tally,
    kpi_rows: kpiRows.map((r) => ({
      hh_kpi: r.HH_KPI, platform: r.PLATFORM, section: r.SECTION,
      status: r.STATUS, core_location: r.CORE_LOCATION, mart_location: r.MART_LOCATION,
      gap_action: r.GAP_ACTION,
    })),
    live_metric_status_sample: liveMetricRows,
    source: "artifacts/v0/P7_1_PLATFORM_KPI_CROSSWALK.csv (locked baseline) " +
      "+ mart.v_ai_metric_status (live, date-scoped detail when a range is given)",
    warning_template: "CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH — see kpi_status_tally and " +
      "any NO_PERMISSION/SEPARATE_API_REQUIRED/NOT_EXPOSED_PUBLIC_API/MISSING_SOURCE rows above.",
  };
}
