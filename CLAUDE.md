# CLAUDE.md — HH Ecom Data + AI (APIEcom_HH)

Guidance for Claude Code working in this folder. Also loaded by the Telegram
report bot (`claude -p` runs here), so the reporting rules below apply to it.

> **This repo is PUBLIC.** Never commit business numbers (Net Sales, COGS, P&L,
> SKU→cost maps), infra IDs, tokens, or anything from `~/HH_PRIVATE_RELEASES`.
> Keep this file free of real figures too.

## What this is

API-based data pipeline for Hoàng Hà (HH) marketplace sales — **Shopee + TikTok
Shop only** (no Nhanh, no MT/GT) — into a Neon Postgres warehouse, exposed to
reporting/AI through `mart.*` views and an MCP server.

```
Shopee / TikTok Shop / TikTok Business (ads) APIs
  → pipelines/incremental.py  (per-domain workers, integrations/*/pilot_reporting/)
  → core.*   (facts + dims: fact_order, fact_order_item, fact_settlement,
              fact_settlement_sku_fee, fact_ads_daily, fact_shopee_affiliate_*,
              dim_product, dim_cogs, dim_combo_bom, map_platform_product)
  → scripts/gold/*.py  → mart.gold_channel_daily (long format, metric_name + coverage_status)
  → mart.v_ceo_ecom_daily (wide daily P&L by channel) + mart.v_ai_* views
  → consumers: hh-mart MCP (read-only, role hh_ai_reader), Cloudflare MCP worker
    (workers/hh-ecom-reporting-mcp), daily Telegram report, Telegram Claude bot
```

Schemas: `control` (sync state, etl runs), `core`, `mart`, `audit`.
Roles: `hh_admin`, `hh_etl_writer` (no DDL), `hh_ai_reader` (mart read-only).

## Production runtime — who owns what

- **GitHub Actions owns production**: `.github/workflows/p3-incremental.yml`
  (cron 06:00 + 18:00 ICT, plus `workflow_dispatch`) runs
  `scripts/run_production_cycle.py`: **CORE ingest → GOLD build → reconcile
  (D-1/D-3/D-7) → healthcheck**. Verdict RED = exit 1.
  - Reconcile runs *after* GOLD, so data reconciled in a run only shows in mart
    after the **next** run's GOLD build.
  - Targeted backfill: `pipelines/reconcile.py --dates YYYY-MM-DD,... --only SHOPEE:affiliate_ams`
    (must run where the tokens live — i.e. on GitHub, not locally).
- **Shopee/AMS/TikTok token rotation lives in GitHub Secrets**
  (`*_TOKEN_STATE_JSON`, written back via `GH_SECRETS_WRITER_TOKEN`). **Never run
  `integrations/shopee/auth/refresh_token*.py` locally** — it forks the refresh
  chain and breaks production. Local Keychain tokens being expired is expected.
- Shopee AMS (affiliate) catch-up has a 360 s per-call budget and advances
  sync_state partially; AMS publishes with ~1–2 days lag and rejects "today".
  `SOURCE_NOT_READY` for D-1 is normal, not a failure.

## Bộ lọc chuẩn — canonical business rules (do not change without HH approval)

Authoritative docs: `artifacts/v0/P8_5_CANONICAL_BUSINESS_RULES.md`,
`artifacts/v0/P6B3_FINAL_PNL_CONTRACT.csv`, `artifacts/v0/P6B2_RULE_CONFLICTS.csv`.

**Order / status**
- Cancelled = `core.fact_order.order_status = 'CANCELLED'` exactly (API enum,
  both channels). Not "Đã hủy"/"Canceled" (those are legacy export labels).
- `business_date` = order date (Asia/Ho_Chi_Minh). Costs from settlement or
  affiliate are attributed to the **order's** business_date, never the
  settlement/posting date.
- Platform GMV = `SUM(total_amount)`, **all statuses** — GMV ≠ Net Sales.

**Net Sales**
- Shopee (escrow, `settlement_type='escrow_estimate'`):
  `order_original_price − (same, cancelled orders) − seller_discount − voucher_from_seller
  + (seller_return_refund + drc_adjustable_refund + seller_lost_compensation)`;
  deductions/refunds only on non-cancelled orders.
- TikTok: `SUM(revenue_amount) WHERE settlement_type='MATCHED'`, joined back to
  order date. Days without full settlement → `PARTIAL_SETTLEMENT_COVERAGE` /
  `SOURCE_LAGGING` (CM1/CM2 withheld) — expect ~1 week lag.

**COGS (GM1 = Net Sales − sellable_cogs − promo_gift_cost)**
- Path: order item seller SKU → `core.map_platform_product` → `dim_product`
  (`default_transaction_role`: SALE / PROMO_GIFT / PACKAGING) → active
  `dim_cogs` (`cost_basis = HH_OFFICIAL_COGS`, time-independent). Combos via
  `dim_combo_bom` (sum of components).
- Gifts are identified by the **product master role**, never by name keywords
  or fuzzy matching.
- Unmapped line → `COGS_INCOMPLETE` (never 0). New listings need new
  `map_platform_product` rows — the main source of unresolved lines.

**CM1** = GM1 − platform fees − packaging − shipping/return adj
- Shopee fees: commission_fee, service_fee, seller_transaction_fee (actuals).
- TikTok fees: fixed / payment / VXP / infrastructure — **API actuals from
  `fact_settlement_sku_fee`**, never the legacy flat % rates.
- Packaging = `packaging_item_cogs` (PACKAGING-role SKUs) +
  `hh_operational_packaging_cost` (2,000 VND × non-cancelled units). Two
  separate concepts; exposed separately and as `total_packaging_cost`.

**CM2** (`cm2_known` = known part; `cm2` = same value, status PARTIAL)
- Shopee: `cm1 − ads_spend − affiliate_commission`.
  - Ads: Shopee `fact_ads_daily` has BROAD and DIRECT rows with identical
    spend → use `MAX`, **never sum BROAD+DIRECT**.
  - Affiliate, per order: settlement `order_ams_commission_fee` if non-zero
    (SETTLEMENT_ACTUAL), else AMS conversion commission to affiliate + MCN
    (ORDER_LEVEL_ACTUAL). Never both.
- TikTok: `cm1 − affiliate_fee − ads_spend`, ads subtracted **only when
  `ads_spend_status='READY'`**. Ads = GMV Max for the shop only; AUCTION ads are
  stored but excluded pending an HH allocation decision.
- **Scope decision (user, 2026-11-26): Booking/KOL/KOC and Live in-house/teaser
  costs are OUT OF SCOPE for CM2 for now** — do not build, propose, or report
  them as a CM2 gap. CM2 for reporting = `cm2_known` (= `cm2`). The pipeline
  still labels it `PARTIAL_MISSING_INTERNAL_MARKETING_SOURCE`; that label only
  refers to these excluded items, so it is not by itself a reason to call CM2
  incomplete. CM2 is still provisional when its inputs are: `COGS_INCOMPLETE`,
  `SOURCE_LAGGING`, `MISSING_SOURCE` (e.g. AMS lag), ads not READY.
- Profit stays NULL (`MISSING_BACKOFFICE_SOURCE`) — no backoffice rule approved.

**Payout / tiền về** (P16, `sql/067_STAGED_*`, `scripts/gold/_p16_payout_*.py`)
- Per order, by the **order's** business_date (never `fact_settlement.business_date`,
  which for Shopee is the ingestion date — `settlement_amount_estimated` is
  therefore NOT a per-day payout).
- Shopee escrow `escrow_amount_after_adjustment`: ACTUAL if order COMPLETED/
  CANCELLED, else ESTIMATE. TikTok: `MATCHED.settlement_amount` = ACTUAL, else
  TikTok's own `est_settlement_amount` (/finance/orders/unsettled) = ESTIMATE.
  Neither → MISSING (never 0, never a % formula). Estimates are replaced by the
  real value automatically on the next run after the platform settles.
- Mart: `payout`, `payout_actual`, `payout_estimated`, `payout_status`
  (ACTUAL / MIXED_ACTUAL_AND_ESTIMATE / PLATFORM_ESTIMATE / PARTIAL_MISSING_SOURCE),
  `payout_orders_{actual,estimated,missing}`. Payout ≠ Net Sales ≠ CM1 (it is
  cash from the platform after its own fees/affiliate/shipping).

**Missing vs zero (applies everywhere)**
- NULL + a coverage status, never `COALESCE(x, 0)`. A real 0 is only a real
  observed zero. Status vocabulary: `READY`, `DERIVABLE`, `COGS_INCOMPLETE`,
  `MISSING_SOURCE`, `SOURCE_LAGGING`, `PARTIAL_*`, `NO_PERMISSION`,
  `ESTIMATED_PERFORMANCE`, `RULE_NOT_APPROVED`, `NOT_APPLICABLE`.
- Raw API values go through `pipelines/canonical_normalizer.py`
  (absent/null/"" → None; "0" → Decimal 0; no truthiness on money).

## Reporting / answering business questions (incl. Telegram bot)

- Query **only `mart.*`** via the `hh-mart` MCP. Start with
  `mart.v_ceo_ecom_daily`; check `mart.v_ai_source_coverage` /
  `mart.v_gold_build_freshness` first.
- Always read the `*_status` column next to a number. Non-READY numbers must be
  labelled (e.g. "tạm tính", "chưa đủ settlement") — say "CHƯA ĐỦ DỮ LIỆU"
  instead of guessing. Report CM2 = `cm2_known` (booking/KOL/live excluded by
  scope); call it "tạm tính" only when cm1/ads/affiliate inputs are not final
  (COGS_INCOMPLETE, SOURCE_LAGGING, MISSING_SOURCE, PARTIAL_SETTLEMENT_COVERAGE).
- GMV ≠ Net Sales; PLAN ≠ ACTUAL; AMS `affiliate_gmv`/`orders` are
  ESTIMATED_PERFORMANCE, not financial actuals.
- Ratios over a period: sum numerators and denominators first, never average
  daily ratios.

## Making changes

- SQL migrations: `sql/NNN_*.sql`, numbered. `NNN_STAGED_*` = not yet applied.
  `CREATE OR REPLACE VIEW` cannot reorder/rename columns — **append new columns
  at the end** of `mart.v_ceo_ecom_daily`.
- Gold metrics are ownership-guarded (`mart.gold_metric_ownership`,
  `scripts/gold/_gold_ownership.py`: BASE_GOLD vs PNL_ENRICHMENT). CM1 is
  read-only for CM2 scripts.
- When a Gold formula changes, update the mirror check in
  `scripts/production_healthcheck.py` in the same change (it recomputes CM2 and
  goes RED on mismatch).
- **Production DB writes / DDL / backfills need explicit, narrowly scoped user
  approval**, run from a saved script file (not inline one-liners).
- Tests: always run with a blocked DB so nothing hits prod/live APIs:
  `HH_ETL_WRITER_DATABASE_URL=postgresql://blocked@127.0.0.1:1/blocked
  HH_NEONDB_OWNER_DATABASE_URL=postgresql://blocked@127.0.0.1:1/blocked python3 -m pytest -q <paths>`
  and check `integrations/tiktok_shop/tokens.json` was not rewritten.
- Branch before committing; main is deployed by the schedule.

## Legacy system (reference only)

**Full rule memory: `knowledge/LEGACY_HH_LPM_ETL_RULES.md`** — how the legacy
ETL filters each channel's export files, how the CEO P&L reads them back
(waterfall, gates), and a legacy↔API crosswalk. Read it before answering any
"how did the old report / Excel CEO compute X" question or comparing numbers.

`github.com/nguyentlevu203-create/hh-lpm-etl` — HH's older Windows/Postgres
(`DuLieu`) system built on **manual Excel exports**, covering Nhanh, Shopee,
TikTok, MT, GT, and emailing a CEO Daily P&L Excel. Use it only to understand
what HH means by a term. Do not copy its code or constants: its TikTok fee %,
backoffice 15%, keyword gift rule, and `COALESCE(...,0)` routers are known
wrong or superseded (see `P6B2_RULE_CONFLICTS.csv`). It *does* hold sources this
repo lacks (TikTok live in-house/teaser costs, KOL booking-rate master), which
are currently OUT OF CM2 SCOPE by user decision (2026-11-26). If integrating,
the legacy system should **read from this mart**, not the other way round.

## Key paths

| What | Where |
|---|---|
| Production cycle | `scripts/run_production_cycle.py`, `.github/workflows/p3-incremental.yml` |
| Ingest workers | `pipelines/incremental.py`, `integrations/{shopee,tiktok_shop,tiktok_business}/` |
| Shopee worker (orders/finance/AMS) | `integrations/shopee/pilot_reporting/incr_worker.py` |
| Gold / P&L builders | `scripts/gold/` (`_p6a_gold_build.py`, `_p6c1_shopee_pnl_extend.py`, `_p6c5_shopee_cm2.py`, `_p6b4_tiktok_cm2.py`) |
| Mart view (current) | highest-numbered `sql/*.sql` that redefines `mart.v_ceo_ecom_daily` (053 as of 2026-11) |
| Healthcheck | `scripts/production_healthcheck.py` |
| Rule docs / audits | `artifacts/v0/` (P5 COGS, P6B P&L, P8.5 canonical rules) |
| MCP worker | `workers/hh-ecom-reporting-mcp/` |
