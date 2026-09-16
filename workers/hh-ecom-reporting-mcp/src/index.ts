// P8.2 - HH Ecom read-only reporting MCP server on Cloudflare Workers.
//
// Every tool here is a thin wrapper around one function in queryService.ts.
// No tool accepts, builds, or executes arbitrary SQL - every tool takes a
// small set of validated, typed parameters and returns a JSON-safe object.
// There is no execute_sql/raw_sql/run_query tool anywhere in this file.
//
// Auth: bearer-token check via the MCP_AUTH_TOKEN Worker secret - never
// hard-coded, never logged. If unset, every request is rejected (fails
// closed). The MCP handler itself does not check auth (confirmed from
// Cloudflare's own docs) - this file supplies that check explicitly.

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";
import type { Env as DbEnv } from "./db";
import {
  getAffiliatePerformance,
  getCostBreakdown,
  getDataCoverage,
  getEcomOverview,
  getLivePerformance,
  getOperations,
  getVideoPerformance,
} from "./queryService";
import { ValidationError } from "./validation";

interface Env extends DbEnv {}

function safeLog(entry: Record<string, unknown>): void {
  // Section 8: only timestamp, tool_name, safe parameter metadata, duration,
  // row_count, success/failure. Never a secret, connection string, full
  // response payload, or token.
  console.log(`MCP_AUDIT ${JSON.stringify(entry)}`);
}

async function wrap<T>(fn: () => Promise<T>): Promise<T | { error: string; message: string }> {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ValidationError) {
      return { error: "invalid_input", message: err.message };
    }
    return { error: "query_failed", message: err instanceof Error ? err.message : "unknown error" };
  }
}

function textResult(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
}

function buildServer(env: Env): McpServer {
  const server = new McpServer({ name: "hh-ecom-reporting", version: "1.0.0" }, {
    instructions:
      "Read-only HH Ecom reporting tools (Shopee + TikTok). All data comes from " +
      "approved mart.v_ai_*/v_ceo_* views via the hh_ai_reader role. " +
      "ALWAYS check status/value_basis/blocking_reason fields before stating a " +
      "number as fact - a NULL value with a status field explains why, it is " +
      "never a real zero. Platform GMV is never Net Sales. TikTok LIVE " +
      "account_type='ALL' is the authoritative total; 'AFFILIATE_ACCOUNTS' is a " +
      "SUBSET of it - never sum the two. Before concluding CM2 is complete or a " +
      "channel/campaign is profitable at CM2, call get_data_coverage and " +
      "get_cost_breakdown and confirm no material cost source is missing; if any " +
      "is, say so explicitly (in Vietnamese if the user asked in Vietnamese: " +
      "'CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH') and list exactly what is missing.",
  });

  server.registerTool(
    "get_ecom_overview",
    {
      title: "Ecom overview",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "Daily Sales/Net Sales/COGS/GM1/CM1/CM2/Profit overview per platform. " +
        "from_date/to_date: YYYY-MM-DD, max 31-day range. platform: SHOPEE or TIKTOK (omit for both). " +
        "Platform GMV and Net Sales are always returned as separate fields - never conflate them.",
      inputSchema: z.object({
        from_date: z.string(),
        to_date: z.string(),
        platform: z.string().optional(),
      }),
    },
    async ({ from_date, to_date, platform }) => {
      const data = await wrap(() => getEcomOverview(env, safeLog, { from_date, to_date, platform }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_cost_breakdown",
    {
      title: "Cost breakdown",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "Every currently verified cost component (fees, packaging, affiliate commission, " +
        "Ads spend, Booking/KOL/KOC, Live in-house cost, backoffice) with amount=NULL + a real " +
        "status whenever a source is unavailable. Never returns a fabricated zero.",
      inputSchema: z.object({
        from_date: z.string(),
        to_date: z.string(),
        platform: z.string().optional(),
      }),
    },
    async ({ from_date, to_date, platform }) => {
      const data = await wrap(() => getCostBreakdown(env, safeLog, { from_date, to_date, platform }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_operations",
    {
      title: "Operations health",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "Shopee Account Health operational KPIs (Late Shipment Rate, Non-Fulfilment Rate, " +
        "Shop Rating, listing violations, etc). Snapshot/current-state data only - Shopee's " +
        "API has no historical backfill. platform must be SHOPEE or TIKTOK (TikTok returns " +
        "an explanatory empty result - no violation-points API exists for TikTok). Also " +
        "returns an 'inventory' block (latest stock snapshot, SKU/stock totals, zero-stock " +
        "and low-stock SKUs, earliest-expiry items) - channel-independent, always populated " +
        "regardless of the platform argument.",
      inputSchema: z.object({
        platform: z.string(),
        date: z.string().optional(),
      }),
    },
    async ({ platform, date }) => {
      const data = await wrap(() => getOperations(env, safeLog, { platform, date }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_video_performance",
    {
      title: "Video performance",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "TikTok video/content-grain performance (views, GMV, orders, CTR, creator). " +
        "account_type: ALL or AFFILIATE_ACCOUNTS. Only proven for TikTok; Shopee returns " +
        "an explanatory empty result. Never attaches shop-daily impressions/clicks that " +
        "don't exist at this grain.",
      inputSchema: z.object({
        from_date: z.string(),
        to_date: z.string(),
        platform: z.string(),
        account_type: z.string().optional(),
        limit: z.number().int().optional(),
      }),
    },
    async ({ from_date, to_date, platform, account_type, limit }) => {
      const data = await wrap(() => getVideoPerformance(env, safeLog, { from_date, to_date, platform, account_type, limit }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_live_performance",
    {
      title: "LIVE performance",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "TikTok LIVE session performance (GMV, orders, viewers, CTR, CTOR, engagement). " +
        "account_type='ALL' (default) is the authoritative total slice; 'AFFILIATE_ACCOUNTS' " +
        "is a SUBSET - never sum the two. Shopee LIVE has no data here (NO_PERMISSION).",
      inputSchema: z.object({
        from_date: z.string(),
        to_date: z.string(),
        account_type: z.string().optional(),
        limit: z.number().int().optional(),
      }),
    },
    async ({ from_date, to_date, account_type, limit }) => {
      const data = await wrap(() => getLivePerformance(env, safeLog, { from_date, to_date, account_type, limit }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_affiliate_performance",
    {
      title: "Affiliate performance",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "Affiliate/creator performance (sales, orders, commission, ROI). Proven for " +
        "SHOPEE only - TikTok's affiliate performance lives at the video/LIVE content " +
        "grain instead (call get_video_performance/get_live_performance with " +
        "account_type='AFFILIATE_ACCOUNTS'); requesting platform=TIKTOK here returns an " +
        "explanatory routing note, not fabricated data.",
      inputSchema: z.object({
        from_date: z.string(),
        to_date: z.string(),
        platform: z.string(),
        limit: z.number().int().optional(),
      }),
    },
    async ({ from_date, to_date, platform, limit }) => {
      const data = await wrap(() => getAffiliatePerformance(env, safeLog, { from_date, to_date, platform, limit }));
      return textResult(data);
    }
  );

  server.registerTool(
    "get_data_coverage",
    {
      title: "Data coverage",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      description:
        "The locked 52-KPI coverage state (API_ACTUAL / DERIVED_VERIFIED / NO_PERMISSION / " +
        "SEPARATE_API_REQUIRED / NOT_EXPOSED_PUBLIC_API / MISSING_SOURCE) plus live per-date " +
        "metric status when a date range is given. Call this BEFORE making any claim about " +
        "data completeness, especially before concluding CM2 is complete.",
      inputSchema: z.object({
        from_date: z.string().optional(),
        to_date: z.string().optional(),
        platform: z.string().optional(),
      }),
    },
    async ({ from_date, to_date, platform }) => {
      const data = await wrap(() => getDataCoverage(env, safeLog, { from_date, to_date, platform }));
      return textResult(data);
    }
  );

  return server;
}

function timingSafeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const bufA = enc.encode(a);
  const bufB = enc.encode(b);
  if (bufA.length !== bufB.length) return false;
  let diff = 0;
  for (let i = 0; i < bufA.length; i++) diff |= bufA[i] ^ bufB[i];
  return diff === 0;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return Response.json({ status: "ok" });
    }

    if (!env.MCP_AUTH_TOKEN) {
      // fail closed - refuse to serve an unauthenticated reporting server
      return Response.json({ error: "server_misconfigured" }, { status: 500 });
    }

    const auth = request.headers.get("Authorization") ?? "";
    const provided = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!timingSafeEqual(provided, env.MCP_AUTH_TOKEN)) {
      return Response.json({ error: "unauthorized" }, { status: 401 });
    }

    const handler = createMcpHandler((_ctx) => buildServer(env), { route: "/mcp" });
    return handler(request, env, ctx);
  },
};
