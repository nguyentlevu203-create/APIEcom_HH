# HH ADP → Central Data Platform — Integration Plan (audit-derived, not yet built)

This is a **plan document only**, derived from the read-only audit. Nothing described here has been created. It exists to connect this audit's findings to the separate, ongoing `HH_CENTRAL_DATA_FOUNDATION` workstream (PostgreSQL schema: raw/staging/master/core/mart/audit) once that foundation exists.

## What already exists and can be reused as-is

1. **4 production agents** (`HH_LPM_MARKETING_COMMAND_CENTER`, `HH AI Operating System`, `HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1`, `HH_MARKET_AND_COMPETITOR_RADAR`) — do not need to be rebuilt.
2. **AppKey-based API access** on the Claw Mode apps — already enabled, no action needed to expose them.
3. **Skills matching this exact problem space** — `daily-ecom-pulse`, `weekly-cmo-review`, `campaign-postmortem`, `deal-pnl-gate` on `HH_LPM_MARKETING_COMMAND_CENTER` conceptually overlap with the TikTok Daily Production Pipeline's CEO Daily Report and finance-reconciliation work already built in `pilot_reporting/`. Worth reviewing for overlap/consolidation before building a second, parallel reporting surface.
4. **Multi-model flexibility** (DeepSeek live; OpenAI-compatible, Bedrock, Azure OpenAI, Anthropic, Gemini available/pending) — no lock-in risk.

## What is missing before "TikTok/Shopee/Nhanh → PostgreSQL → ADP" can work

| Gap | Owner (this project) | Status |
|---|---|---|
| PostgreSQL Central DB itself (raw/staging/master/core/mart/audit schemas) | `HH_CENTRAL_DATA_FOUNDATION` workstream | Not yet built (separate, ongoing) |
| HH Reporting API (a thin read-only API in front of the mart layer) | Not started | Not built |
| MCP server (or Custom Connector) exposing that Reporting API to ADP | Not started | Not built — ADP's Custom Connector mechanism exists and is unused (0/0) |
| Confirmation that Neon-style "PostgreSQL connector" pattern is even the right approach vs. a bespoke MCP server | Needs a decision | Open question — Neon connector is a managed-Postgres SaaS wrapper, not a generic client; a bespoke MCP server (thin HTTP wrapper around the Reporting API, MCP-speaking) is the more direct, evidenced-fit path given ADP's Custom Connector/Custom Tool primitives |

## Recommended sequencing (once PostgreSQL Central DB exists)

1. Finish `HH_CENTRAL_DATA_FOUNDATION` (PostgreSQL schemas, TikTok migration, mart layer) — tracked separately.
2. Build a minimal **HH Reporting API**: a small, read-only HTTP service exposing `mart_channel_daily`, `mart_channel_pnl_daily`, `mart_hero_sku_daily` (and CEO Daily Report equivalents) as JSON endpoints. Reuse the existing `pilot_reporting/` Python code's query logic where possible rather than rewriting it.
3. Wrap that Reporting API as an **MCP server** (or, if faster, as ADP's **Custom Tool** — a set of "HTTP Request"-style tool definitions pointing at the Reporting API's endpoints) using ADP's existing Custom Connector/Custom Tool primitives. Read-only endpoints only, matching this whole project's standing "no write access" discipline.
4. Attach that connector/tool to `HH AI Operating System` (the already-existing CEO/cross-functional agent) first — it is the natural home for "ask a question, get a decision-ready answer grounded in HH's real data" per its own system prompt, rather than building a fifth agent from scratch.
5. Only after step 4 is validated: consider whether `HH_LPM_MARKETING_COMMAND_CENTER` (marketing-specific) and a not-yet-existing Finance agent should get the same connector, scoped to their relevant mart tables.
6. Re-run this same read-only audit process after step 3 to confirm the new Custom Connector/Custom Tool shows up correctly and that `TENCENT_INFRA_ACCESS_FROM_ADP`-style questions (now "HH_DATA_ACCESS_FROM_ADP") resolve to YES with the connector as the documented data source.

## Explicit non-goals for this plan

- Not proposing ADP as the database.
- Not proposing to abandon or replace the 4 existing agents.
- Not proposing to enable any paid Tencent Cloud infrastructure service as part of this plan — that remains a separate decision requiring its own review (compute/network/database/security/operations), independent of ADP.
- Not proposing to change any of the already-PASS TikTok reconciliation logic — the Reporting API step above is additive (a read surface on top of the existing PostgreSQL/mart output), not a rewrite.
