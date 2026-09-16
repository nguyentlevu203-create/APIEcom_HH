# HH ADP Agent Capability Matrix

## Capability × Integration Matrix (Phase 9)

| CAPABILITY | AVAILABLE | CONFIGURED | CURRENT CONNECTION | CAN USE FOR HH DATA PLATFORM |
|---|---|---|---|---|
| REST API (HTTP SSE) | YES | YES (AppKey enabled on ≥2 apps) | Live, AppKey-auth only | YES — apps can already be called externally today |
| WebSocket | YES | YES (same AppKey mechanism) | Live, needs AppKey + API key | YES |
| MCP (as consumer, via marketplace connectors) | YES | NO (0 connectors attached to any app) | None live | YES, once a connector/custom MCP server is attached |
| MCP (custom server) | YES (Custom Connector mechanism) | NO (0 created) | None | YES — this is the path to expose HH's own PostgreSQL/reporting API to ADP |
| Custom HTTP API (Custom Tool) | YES | NO (0 created) | None | YES — alternative/simpler path than a full MCP server for read-only reporting endpoints |
| Database connector (generic) | PARTIAL | NO | None | Only via **Neon** (managed Postgres, Pending configuration, not Tencent/self-hosted-Postgres-generic) — not a drop-in for HH's own Postgres unless Neon-hosted |
| Tencent Cloud connector (CVM/VPC/CAM/COS) | **NO** | NO | None | NO — confirmed absent from the 52-item marketplace ("Tencent" search → No data) |
| GitHub | Not found in the sampled/searched marketplace views | NO | None | Unconfirmed — not specifically searched |
| Google Drive | Not directly observed (Notion, PandaDoc, etc. seen instead) | NO | None | Unconfirmed — not specifically searched |
| CRM | Apollo.io observed (sales contacts/accounts) | NO | None | Possible, generic CRM not HH's own system |
| ERP | Not found in the sampled/searched marketplace views | NO | None | Unconfirmed — not specifically searched |
| Messaging channels (WeChat/WeCom/Telegram/LINE/DingTalk) | YES | NO (0 configured) | None | Only relevant if HH wants chat-bot-style delivery, not for the data platform itself |

## Claw Mode Sandbox Capability Read

| Capability | Answer | Basis |
|---|---|---|
| Available? | YES, in active use on 2/4 apps | App list shows "Claw Mode" |
| Existing Claw apps? | YES — `HH_LPM_MARKETING_COMMAND_CENTER`, `HH AI Operating System` | Application list |
| Sandbox environment? | YES | Claw Mode label + per-app isolated tool/skill/KB binding |
| Can bind tools? | YES | 6 tools attached on the app inspected |
| Can call MCP? | Mechanism present, not exercised (0 connectors attached anywhere) | Connector marketplace is 100% MCP-based |
| Can execute Python/code? | Plausible, not directly confirmed | "Skills Security Check" tool (Tencent YunDing Lab) implies code-capable skills exist and are scanned; not run in this audit |
| Can generate files? | YES | Excel/XLSX, Word/DOCX, PowerPoint/PPTX, PDF, SVG Visualizer skills all attached |
| Can access internet? | YES on Claw apps (Google/Parallel/Brave Search, Agent Browser); explicitly **NO** on the Standard-mode Competitor Radar app by its own prompt design | Skill lists + verbatim prompt text |
| Can persist files? | Not tested | — |
| Permission model? | Enterprise/Space role model (Super Admin / Space Admin / Standard User) + per-app tool/skill grants | Phase 6 findings |

```
CLAW_MODE: AVAILABLE
```

---

## HH Central Data Platform Fit (Phase 16)

| # | Question | Verdict | Evidence |
|---|---|---|---|
| 1 | DATA DATABASE | **NO** | ADP has no native relational database of its own exposed to tenants; explicitly not treated as a PostgreSQL substitute |
| 2 | ETL ENGINE | **PARTIAL** | Claw Mode apps can call tools/skills and generate files (Excel/data-analysis), which is ETL-adjacent, but there is no dedicated batch/scheduled ETL pipeline primitive observed (no cron/schedule feature found in this audit) |
| 3 | AI AGENT PLATFORM | **YES** | 4 working custom agents already in production ("Running" status), Claw + Standard modes, real HH/LPM business prompts and skills |
| 4 | MCP CLIENT / TOOL CONSUMER | **YES** | 52-item MCP connector marketplace; mechanism proven, just not yet wired to any app |
| 5 | KNOWLEDGE/RAG LAYER | **YES** | Working KB with 16 real LPM commercial/sales/CRM files; a second app doing live RAG lookups against product/price/hero-SKU knowledge, evidenced by a successful debug run |
| 6 | REPORTING INTERFACE | **PARTIAL** | Apps can produce Excel/Word/PPT/PDF outputs and answer natural-language questions; no dashboard/BI-style visualization surface found; `daily-ecom-pulse` and `weekly-cmo-review` skills exist and are conceptually exactly this project's CEO Daily Report pattern, but were not inspected in execution |
| 7 | CEO AGENT | **YES** | `HH AI Operating System`'s own prompt explicitly scopes it as the CEO/C-level/dept-head/lead/operator shared operating layer |
| 8 | MARKETING AGENT | **YES** | `HH_LPM_MARKETING_COMMAND_CENTER` (CMO_ORCHESTRATOR) + `HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1` + `HH_MARKET_AND_COMPETITOR_RADAR` |
| 9 | FINANCE AGENT | **NO** (not yet observed) | No app/skill dedicated to Finance was found as a standalone agent; "Marketing Finance KPI Guardrails" KB file exists but is a data source, not an agent |
| 10 | ORCHESTRATOR | **YES** | `HH_LPM_MARKETING_COMMAND_CENTER` explicitly self-describes as "CMO_ORCHESTRATOR"; `HH AI Operating System` is a broader cross-functional orchestration layer |

---

## Target Architecture Decision (Phase 17)

```
TikTok / Shopee / Nhanh
          |
PostgreSQL Central DB
          |
HH Reporting API
          |
MCP
          |
Tencent ADP
          |
CEO / Finance / Marketing Agents
```

**ARCHITECTURE FIT: STRONG** for the ADP-and-above layers (MCP → ADP → Agents); **UNPROVEN/GAP** for the PostgreSQL → Reporting API → MCP bridge, which does not exist yet in this environment.

Evidence for STRONG (top of the stack):
- ADP already runs real Marketing and cross-functional (CEO-scoped) agents in production, with 17-80 skill slots, file generation, and search tooling.
- ADP's Connector marketplace is entirely MCP-based — the platform's native extension mechanism *is* MCP, so plugging in an MCP server that fronts HH's PostgreSQL Central DB is the intended, well-trodden integration path here, not a workaround.
- AppKey-based REST(SSE)/WebSocket API is already live on 2 apps — agents can be called externally today if HH ever needs the data flow to go the other direction (e.g., a scheduler pushing a prompt into an agent).
- Super Admin (current session) has full enterprise/space visibility to wire this up.

Evidence for the GAP (bottom of the stack):
- No PostgreSQL Central DB exists yet in this project (separate, still-open workstream — see `HH_CENTRAL_DATA_FOUNDATION` work).
- No HH Reporting API exists yet.
- No custom MCP server or Custom Connector has been built (0/0 in both categories) — the mechanism is present but entirely unused.
- The only Postgres-flavored marketplace connector (**Neon**) is a third-party managed-Postgres SaaS, not a generic "point at any Postgres connection string" tool — it would not, by itself, connect to a self-hosted or TencentDB-hosted instance without separate verification.

**ADP is not, and should not be treated as, the source-of-truth database.** It is a strong candidate for the **Agent Layer** at the top of the stack, contingent on HH building the PostgreSQL → Reporting API → MCP bridge first.
