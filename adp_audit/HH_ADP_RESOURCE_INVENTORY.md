# HH ADP Resource Inventory — Default Space

Read-only inventory, captured 2026-09-08. No AppKey/API key/token values are included.

## APPLICATIONS

| Name | Mode | Status | Published | Purpose (from system prompt) |
|---|---|---|---|---|
| HH_LPM_MARKETING_COMMAND_CENTER | Claw Mode | Running | Yes (live, AppKey enabled, no pending publish) | "CMO_ORCHESTRATOR của Hoàng Hà cho Le Petit Marseillais (LPM) tại Việt Nam" — Net Sales/EBITDA/CM2 goals, brand ecosystem build, cross-functional connection (Brand/E-commerce/Content/Media/Creator/CRM/Trade/Sales/Finance) |
| HH AI Operating System | Claw Mode | Running | Yes (live, AppKey enabled) | "Vận hành như lớp điều hành doanh nghiệp dùng chung cho CEO, C-level, Trưởng phòng, Lead và Operator của HH" — converts questions/reports/files/emails/meetings/market evidence/operational data into decisions & consistent execution |
| HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1 | Standard | Running | Not confirmed (Publish tab not opened for this app) | "HH x LPM Market Intelligence Assistant" — Market Intelligence Lead role combining CMO/CGO/CSO/CFO/Head E-commerce/Trade Marketing/CRM/Data perspectives, scoped to LPM only |
| HH_MARKET_AND_COMPETITOR_RADAR | Standard | Running | Not confirmed | Competitor/market radar; explicitly self-declares no web search, no internet access, no external DB access, no approved KB yet — strict FACT vs INFERENCE vs hypothesis labeling in outputs |

Creator/editor: all 4 apps show `200052301989` (current Super Admin) as creator/last editor, except `HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1` whose creator is `hh-ai-aae-01` (Space Admin).

## KNOWLEDGE BASES

| Name / Tag | Attached App | Status | Approx size / doc count |
|---|---|---|---|
| Default Knowledge Base (LPM commercial/sales/CRM/finance-KPI set) | HH_LPM_MARKETING_COMMAND_CENTER | All 16 files "Import Completed" | 16 files, ~600KB+ total (largest single file 270KB) |
| KB_LPM_MI_SSOT (product/price/hero-SKU/EAN lookups) | HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1 | Referenced live in a successful debug run | Not directly opened; ≥5 distinct sub-documents referenced (product_lookup, price_lookup, mart_hero_topn, ean_snapshot, and one more) |
| KB_LPM_MI_HISTORY (superseded price candidates) | HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1 | Referenced live, explicitly marked "not_current"/superseded | ≥5 sub-documents referenced |
| Default Knowledge Base | HH_MARKET_AND_COMPETITOR_RADAR | Listed but described by the app's own prompt as "chưa có Knowledge Base được phê duyệt" (not yet approved) | Not populated with approved content per the app's own guardrail text |
| (unnamed) | HH AI Operating System | Not opened in this audit | Not captured |

Knowledge Base Storage Usage (space-wide, Billing page): **5GB plan capacity, 2.94MB used**.

## TOOLS / PLUGINS (marketplace, Resource Center → Connector and Tool → Connector tab)

52 total pre-built connectors, all carry an **MCP** badge. Categories seen: Knowledge Management, Document Management, Project Collaboration, Business Operations, Productivity Tools. Sample of entries actually viewed:

| Name | Type/Category | Status seen | Purpose |
|---|---|---|---|
| Knock | Business Operations | Unavailable | Notification infrastructure |
| Axiom | Business Operations | Available | Observability (log/trace/metric) |
| BetterStack | Business Operations | Available (Official fee) | Monitoring/incident management |
| Meta Ads | Business Operations | Available | Meta advertising accounts, campaign mgmt |
| WorkOS | Business Operations | Available | Identity/access management |
| Todoist | Project Collaboration | Available | Task/project management |
| QuickNode | Productivity Tools | Available | Web3 RPC endpoints |
| Miro | Project Collaboration | Available | Collaborative boards |
| PandaDoc | Document Management | Available | Documents/contracts/proposals |
| monday.com | Productivity Tools | Available | Boards/items/dashboards |
| Stripe | (not opened) | Pending configuration | Payments |
| Cloudflare | Productivity Tools | Available | Workers/KV/R2/D1 databases/Hyperdrive |
| Apollo.io | Business Operations | Available | Sales contacts/accounts |
| Netlify | Productivity Tools | Available | Sites/deploys |
| InstantDB | Business Operations | Pending configuration | Real-time client-side database |
| Prisma | Business Operations | Available | Managed Postgres + ORM |
| Fibery | Project Collaboration | Available | Work management + databases |
| **Neon** | Business Operations | **Pending configuration** | **Serverless PostgreSQL** — closest thing to a generic Postgres connector found; NOT Tencent-affiliated |
| Notion | Knowledge Management | Available | Workspace/pages/database |

Per-application **Tool** attachments actually configured (not marketplace-wide):

| App | Tools attached (6, HH_LPM_MARKETING_COMMAND_CENTER) | Status |
|---|---|---|
| — | Knowledge Base Q&A / KnowledgeRetrievalAnswer | Configured |
| — | Google Search / google_search | Pending configuration |
| — | Google Search / extract_webpage_content | Pending configuration |
| — | Parallel Search / web_search | Configured |
| — | Parallel Search / web_fetch | Configured |
| — | Brave Search / brave_web_search | Pending configuration |

**Connectors actually attached to any app: 0** (every app's "Connector" section showed "Added (0)").

## CONNECTORS

| Name | Connected Service | Status |
|---|---|---|
| (none) | — | 0 connectors are attached to any of the 4 applications. The 52-item marketplace exists but nothing from it has been wired into an app. |

## MCP

| Name | Endpoint/type | Status |
|---|---|---|
| (implicit, via marketplace) | Every one of the 52 marketplace connectors is MCP-based | Available, not attached to any app |
| Standalone/custom MCP server | — | 0 configured (Custom Connector tab: "No data", creation mechanism unused) |

## SKILLS (per-app, sample from HH_LPM_MARKETING_COMMAND_CENTER — 17/80 slots used)

Doc Co-authoring, Excel/XLSX, Pdf, PowerPoint/PPTX, Word/DOCX, Agent Browser, Skills Security Check (Tencent YunDing Lab), SVG Visualizer, Data-Analysis, **lpm-brand-context**, **market-pulse**, **daily-ecom-pulse** (D+1 Shopee/TikTok/DTC performance → root causes/actions), **deal-pnl-gate**, **campaign-planner**, **campaign-postmortem** (D+1/D+3/D+7), **content-creator-sprint**, **weekly-cmo-review**. The bold entries are HH/LPM-custom-built skills, not generic marketplace skills — direct evidence of prior investment in exactly this project's problem space (daily e-commerce performance reporting, campaign P&L gating, CMO-level review cadence).

## MODELS (Resource Center → Model)

| Provider | Status |
|---|---|
| DeepSeek | Added (in active use — DeepSeek-V4-Flash, 1000K context, on all 4 apps checked) |
| Tencent Youtu | Added |
| Platform-Hosted Third-Party Models (GPT etc.) | Added |
| ADP DocParsing Protocol, Amazon Bedrock, OpenAI Compatible, Azure OpenAI | Available (not yet added) |
| OpenAI, Anthropic (Claude), Gemini, Moonshot AI, Qwen, Byteplus, Minimax, Z.AI(GLM) | Pending addition (not configured) |

## SECURITY (Platform → Security)

| Item | Status |
|---|---|
| Security Policy | 2 default policies: "Disable Content Moderation" and "Default Strategy of Content Moderation" (both system default) |
| Keyword Library | Tab exists, not opened in detail |
| Application Security Settings | Tab exists, not opened in detail |
| Risk Identification Statistics / Details | Tabs exist, not opened in detail |

## BILLING (Platform → Billing)

| Package | Resource Pack (Used/Total) | KB Capacity (Used/Total) | Status | Effective | Expires |
|---|---|---|---|---|---|
| Starter Version Monthly Paid Intl | 0/40,000 credits | 2.94MB/5GB | Available | 2026-09-04 | 2026-10-04 |
| Free Version Intl | 10,000/10,000 credits (exhausted) | 2.94MB/1GB | Available | 2026-08-27 | 2026-09-27 |

Prepaid Resource Package: none. Dedicated Concurrency: none.
