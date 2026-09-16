# HH Tencent ADP Infrastructure & Integration Audit V1

Audit type: **READ-ONLY**. No app/workspace created or deleted, no API key created/revealed/copied, no permission changed, nothing published, no paid service enabled, no TencentDB/CVM/COS created, no billing changed, no knowledge base or workflow edited, no model changed, no connector created, no MCP configured, no deployment mode changed.

Audited: 2026-09-08. Portal: `adp.tencentcloud.com`.

---

## PHASE 1 — Portal / Account

| Item | Value |
|---|---|
| Portal | adp.tencentcloud.com (confirmed via page title/logo "Tencent Cloud ADP") |
| Current space | Default Space (only space that exists — "Total Spaces: 1" on Enterprise Dashboard, and the space switcher lists only "Default Space") |
| Space ID | `default_space` (from URL) |
| Current page requested (`#/app/staff-assist`) | Resolves to **Smart Desk** — a *built-in default app*, not a custom app named "staff-assist". Smart Desk shows an **"Activate Now"** button — **not yet activated**. No click was made on it. |
| Login status | **PASS** |
| Enterprise name | Not displayed anywhere in the UI as a distinct field (Tencent Cloud account, not a named "Enterprise" entity in ADP's own terms) |
| Account ID | 200052301989 |
| Current user role | **Main Account** (account-menu label) = **Super Administrator** (confirmed via Enterprise Management → User management: "200052301989 | Super Administrator | All Permissions") |
| Admin/super admin status | **YES — Super Administrator, All Permissions** |
| Subscription/plan | **Starter Plan** (paid), effective 2026-09-04 to 2026-10-04. A separate exhausted **Free Version Intl** package (10,000/10,000 credits used, expires 2026-09-27) also exists — this fully-used free package is the likely source of a transient "account overdue / token exhausted" warning observed once on a Knowledge tab; the paid Starter Plan itself shows 0/40,000 credits used and is Available. |
| Billing status | **Active/Paid** (Starter Plan), not overdue |
| Region/locale | Not explicitly surfaced as a region selector in this portal (global/standalone ADP portal, see Phase 14) |
| Deployment type | Not explicitly labeled in UI — see Phase 14 |

**Important distinction preserved:** `adp.tencentcloud.com` is the Global/Standalone ADP Portal. It is **not** the Tencent Cloud CVM/VPC console and was never treated as such.

---

## PHASE 2 — "Staff Assist" / Smart Desk

There is no application literally named "staff-assist". The route is the platform's built-in **Smart Desk** landing feature, and it is **not activated** (shows "Activate Now", not clicked).

```
STAFF ASSIST APP (= Smart Desk, built-in feature)
----------------
Name:          Smart Desk
Mode:          N/A (built-in feature, not a custom app; no STANDARD/WORKFLOW/MULTI-AGENT/CLAW mode shown)
Status:        NOT ACTIVATED
Knowledge:     N/A (not activated)
Models:        N/A
Tools:         N/A
Connectors:    N/A
MCP:           N/A
Published:     N/A
API exposed:   N/A
Observability: N/A
```

The real evidence of built agent work lives in 4 custom Applications — see Phase 3/Resource Inventory.

---

## PHASE 3 — Space Inventory (summary; full tables in `HH_ADP_RESOURCE_INVENTORY.md`)

- **Applications: 4**, all `Running`. Two in **Claw Mode**, two in **Standard** mode.
- **Knowledge Bases:** at least 1 populated KB observed in detail (16 files, LPM commercial/sales/CRM/market data) on `HH_LPM_MARKETING_COMMAND_CENTER`; other apps reference their own KBs (`Default Knowledge Base`, `KB_LPM_MI_SSOT`, `KB_LPM_MI_HISTORY`).
- **Tools/Plugins (marketplace):** 52 pre-built connectors total, all MCP-based. Categories: Knowledge Management, Document Management, Project Collaboration, Business Operations, Productivity Tools.
- **Connectors actually attached to apps:** 0 (every app inspected showed "Connector — Added (0)").
- **Custom Connector / Custom Tool:** 0 created enterprise-wide; the creation mechanism exists (`+ Create Connector`, `+ New Tool`) but was not used (not clicked).
- **MCP:** every marketplace connector is MCP-based (badge "MCP" on all 52 entries). No standalone "MCP server list" page found outside the Connector marketplace.

---

## PHASE 4 — Resource Dashboard

**Accessible** (current Super Administrator role). Enterprise Management → Reports → Resource Dashboard, sub-tabs: Model Usage, Model Concurrency, Knowledge Base Capacity, Connector and Tool Usage, Platform Usage, Total Usage, Resource Consumption Details.

| Metric | Value (as of 2026-09-08, today's window) |
|---|---|
| Model Call Count | 0 |
| Model Token Consumption | 0 |
| Model Page Consumption | 0 |
| Spaces with usage | 1 (Default Space), all rows 0 for today's date filter |
| Knowledge Base Capacity | 5GB total (Starter), 2.94MB used |
| General Resource Pack | 40,000 credits/month, 0 used (Starter, current period) |
| Quota/limits | Starter Plan limits shown on Billing page; no explicit hard quota breach observed |
| Abnormal consumption | None observed (all usage metrics at 0 for the queried "today" window — historical usage was not separately queried beyond this) |

---

## PHASE 5 — Enterprise View

**Accessible.** Avatar → Enterprise Management → Reports opened the same Business Dashboard / Resource Dashboard described above. Confirms Super Administrator has full Enterprise View.

- Total Spaces: 1
- Total Applications: 4
- Active Application Users: 0 (today's window)

---

## PHASE 6 — Enterprise / Space Roles

**Current user:** `SUPER_ADMIN` (Super Administrator, All Permissions, Enterprise-level).

### ADP_PERMISSION_MODEL

| User | Account ID | Enterprise Role | Space (Default Space) Role |
|---|---|---|---|
| 200052301989 (current session) | 200052301989 | Super Administrator (All Permissions) | Not an explicit space member row, but Super Admin has implicit full access to all spaces |
| hh-ai-admin | 200052305192 | Standard User | Not a member of Default Space (no row in space User management) |
| hh-ai-aae-01 | 200052305445 | Standard User | **Space Admin** (only explicit space-level role defined; "1 items in total" in Role management) |

Space-level function-permission catalogue observed (from Permission management → Allocate by user, for `hh-ai-aae-01`): Model (Add/Delete Model), Workspace/Application Development (New Application, Import App), Favourite Apps, Smart Desk, Knowledge Base (New KB, Data Source, Recall Test), Connectors and Tools (Create), Widget, Prompt (New Template), Skills (Import), Application Template, Clear Conversation, Copy, Application Experience, Platform Management. No permission values were changed.

---

## PHASE 7 — Key Management

Enterprise Management → Key Management: **1 table, "New Key (0/2)", 0 keys, "No data"**. This is the *enterprise-level* API key table, separate from per-app AppKeys (see Phase 8).

```
Tool API Key:        NONE (0/2 enterprise keys exist)
Control Plane Key:   UNKNOWN (no separate "Control Plane Key" section found in this menu; may be the same table)
App Key:              PRESENT (per-app, via each app's Publish → Service status — see Phase 8)
```

No key value, prefix beyond the UI's own masking, or secret was ever read/copied/revealed.

---

## PHASE 8 — API / App Release

Checked on **2 of 4 apps** (`HH_LPM_MARKETING_COMMAND_CENTER`, `HH AI Operating System`) via Publish → Service status. Both show an identical, real mechanism:

- **Experience URL**: present, toggle Enabled, of the form `https://adp.tencentcloud.com/webim_exp/#/chat/claw/<id>` (not fully copied here).
- **AppKey**: **PRESENT and Enabled** on both apps (masked in UI, e.g. `u******H`, `s******D`), with a visible creation timestamp. Never revealed or copied.
- **API documentation**: a "Dialog Interface" doc link is present in-product.
- Platform's own text, verbatim: *"HTTP SSE requires only AppKey authentication, while WebSocket requires AppKey + API key authentication."*
- **Publishing Channel** mechanism exists but 0 channels configured; available channel types observed in the (unselected) dropdown: WeChat, WeCom, Telegram, LINE, DingTalk — these are messaging-bot channels, separate from the raw HTTP/WebSocket API access which is already live via AppKey regardless of any messaging channel.

```
ADP_APPLICATION_API_READY: YES
  REST API (HTTP SSE):  YES (AppKey-only auth)
  WebSocket:            YES (AppKey + API key auth)
  AppKey mechanism:     YES, present & enabled on the 2 apps checked
  API documentation:    YES ("Dialog Interface" link)
  Webhook/callback:     Not directly observed as a separate primitive; messaging-channel integrations (WeCom/DingTalk/etc.) imply webhook-style delivery for those channels specifically
  MCP capability:       Not as an app-exposure mechanism here — MCP is used the other direction (ADP apps *consuming* MCP-based connectors, see Phase 9), not as a way to expose this app as an MCP server
```

---

## PHASE 9 — MCP / Connector Capability Matrix

See `HH_ADP_AGENT_CAPABILITY_MATRIX.md` for the full table. Summary: 52 pre-built MCP connectors exist in the marketplace (Business Operations / Productivity / Project Collaboration / Document / Knowledge Management categories). **Zero Tencent Cloud-branded connector** ("Tencent" search → No data). One generic managed-Postgres connector (**Neon**, Pending configuration, not Tencent-affiliated) exists. Custom Connector / Custom Tool creation mechanisms exist and are unused (0 created).

---

## PHASE 10 — Claw Mode

**Claw Mode is available and already used** — 2 of the 4 applications (`HH_LPM_MARKETING_COMMAND_CENTER`, `HH AI Operating System`) run in Claw Mode.

```
CLAW_MODE: AVAILABLE (in active use)
```

Evidence-based capability read (from the Claw-mode app's configured Skills, not from documentation alone):

| Question | Answer | Evidence |
|---|---|---|
| Sandbox environment? | YES | "Claw Mode" label + isolated per-app skill/tool binding |
| Can bind tools? | YES | 6 Tools attached (Knowledge Base Q&A, Google Search x2, Parallel Search x2, Brave Search) |
| Can call MCP? | Indirectly YES — the Tool/Connector marketplace items are MCP-based; 0 were actually attached as *Connectors* on the apps checked, but the mechanism is present |
| Can execute Python / code? | Plausible, not directly demonstrated — skill list includes "Skills Security Check" ("A skill security review tool from Tencent YunDing Lab. Performs a comprehensive..."), which implies skills capable of code-like execution exist and are security-scanned; not confirmed by directly running code |
| Can generate files? | YES | Skills: Excel/XLSX, Word/DOCX, PowerPoint/PPTX, PDF, SVG Visualizer — all file-generation tooling |
| Can access internet? | YES on Claw-mode apps (Google Search, Parallel Search/web_search, Parallel Search/web_fetch, Brave Search, Agent Browser skill all attached) — explicitly **NO** on at least one Standard-mode app (`HH_MARKET_AND_COMPETITOR_RADAR`'s own system prompt states verbatim: *"Hiện tại Agent này KHÔNG có Web Search, không có Internet access, không có external database access"*) |
| Can persist files? | Not directly tested; file-generation skills exist, persistence location not inspected |
| Permission model? | Governed by the same Enterprise/Space role model (Phase 6) plus per-app tool/skill attachment |

---

## PHASE 11 — Workflow / Multi-Agent

```
WORKFLOW:     AVAILABLE, NONE configured on the app(s) checked ("No Data" on Workflow tab for HH_LPM_MARKETING_COMMAND_CENTER)
MULTI_AGENT:  Not directly observed as a distinct "Multi-Agent" mode selector; the 4 existing apps are each single-agent (Claw or Standard), not visibly composed as a multi-agent graph. "Mode" dropdown per app showed only Claw Mode / Standard in the instances checked.
```

No sub-agents/orchestrator graph inventory found; each of the 4 apps is a standalone agent.

---

## PHASE 12 — Knowledge / Corporate Data

**YES — HH already has substantial corporate knowledge loaded**, specifically for **LPM (Le Petit Marseillais) e-commerce/marketing**, on `HH_LPM_MARKETING_COMMAND_CENTER`'s Default Knowledge Base (16 files, numbered 00-09 governance scheme):

| # | File (name as shown, truncated by UI) | Apparent purpose |
|---|---|---|
| 00 | KB_CONTROL_SSOT_SOURCE_POLICY_CUR... | Single-source-of-truth governance policy |
| 05 | KB_LPM_MARKET_C... (not fully expanded) | Market data |
| 06 | KB_LPM_CRM_DTC_TRADE_OMNICHANNEL... | CRM / DTC / Trade / Omnichannel |
| 07 | KB_LPM_MARKETING_FINANCE_KPI_GUARD... | Marketing finance KPI guardrails |
| 08A | KB_LPM_COMMERCIAL_MASTER_CURRENT... | Commercial master data |
| 08B | KB_LPM_SALES_ACTUAL_MONTHLY_TO_2... | Sales actuals, monthly |
| 08C | KB_LPM_SALES_SKU_UNITS_2025.xlsx | SKU-level unit sales, 2025 |
| 08D | KB_LPM_SALES_SKU_UNITS_2026_TO_20... | SKU-level unit sales, 2026 |
| 08E | KB_LPM_LIVESTREAM_REFERENCE_TO_2... | Livestream reference data |
| 08F | KB_LPM_RUNTIME_CONTROL_GATES_CUR... | Runtime control gates (agent guardrails) |
| 09 | KB_LPM_KNOWLEDGE_MANIFEST_CURRENT... | Knowledge manifest index |
| — | README_UPLOAD_TO_TENCENT.txt | Upload documentation |

`HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1` references its own KBs by tag (`KB_LPM_MI_SSOT`, `KB_LPM_MI_HISTORY`) with sub-files: `product_lookup_current`, `price_lookup_current`, `mart_hero_topn_current`, `ean_current_snapshot`, `price_candidate_20260721_not_current` (superseded). Only filenames/tags and stated purposes were noted; **no file was opened/downloaded and no document body content was read**.

```
KNOWLEDGE BASE INVENTORY (summary)
- LPM Commercial/Sales/SKU/CRM/Finance-KPI/Livestream data: YES (HH_LPM_MARKETING_COMMAND_CENTER)
- LPM Market Intelligence (product/price/hero-SKU lookups): YES (HH_LPM_MARKET_INTELLIGENCE_ASSISTANT_V1)
- CEO-level / cross-functional knowledge: implied by HH AI Operating System's prompt scope, not confirmed via a distinct KB inspected
- Finance (HH-wide, beyond LPM marketing-finance KPIs): NOT separately confirmed
- TikTok / Shopee raw operational data feed into ADP KB: NOT observed (no KB file names referencing TikTok/Shopee API exports)
```

---

## PHASE 13 — ADP Observability

```
ADP_OBSERVABILITY: PASS (infrastructure present; usage currently near-zero)
```

Per-app **Analytics** tab exists with: Call Statistics (Application Call Count, Avg total/first token latency, Component Call Trends: Knowledge base / Workflow / Connector-and-Tool calls), User Statistics, Knowledge Maintenance Statistics, and a separate Conversation History tab. Enterprise-level Reports (Business + Resource Dashboard, Phase 4/5) provide the platform-wide rollup. All queried metrics were 0 for "today" (2026-09-08) — consistent with light usage, not a missing-feature gap.

---

## PHASE 14 — Deployment Model

`adp.tencentcloud.com` is the standalone/global ADP portal. No UI element in this audit surfaced an explicit "Public Cloud / Dedicated Cloud / Private Deployment / Hybrid" selector or label.

```
CURRENT_ADP_DEPLOYMENT: UNKNOWN (not confirmed from UI evidence)
PORTAL TYPE != CVM INFRA EVIDENCE — explicitly noted: this portal gives no visibility into any underlying CVM/VPC/TencentDB the ADP service itself runs on. That is Tencent's own backend, not something this tenant's UI exposes.
```

---

## PHASE 15 — Can ADP Inspect Tencent Cloud Infra?

Searched the Connector marketplace (52 items) for "Tencent" → **No data**. Searched for "database"/"PostgreSQL" → 12 generic (non-Tencent) database-adjacent SaaS connectors, none pointing at Tencent Cloud infrastructure (CVM/VPC/TencentDB/COS/CAM). No CVM connector, no CAM connector, no "Infrastructure plugin" category exists. Custom Connector/Custom Tool mechanisms exist but 0 built.

```
TENCENT_INFRA_ACCESS_FROM_ADP: NO

ADP hiện không có quyền nhìn CVM/VPC/TencentDB — cần Tencent Cloud Console/API/SSH riêng
để audit hạ tầng thật (compute/network/database/security/operations), như đã nêu ở lượt hỏi trước.
```

---

## Cross-references

Full resource tables: `HH_ADP_RESOURCE_INVENTORY.md`
Capability matrix + HH platform fit: `HH_ADP_AGENT_CAPABILITY_MATRIX.md`
Target architecture decision: `HH_ADP_TO_CENTRAL_DATA_PLATFORM_PLAN.md`
