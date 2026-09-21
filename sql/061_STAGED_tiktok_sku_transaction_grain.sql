-- =====================================================================
-- 061_STAGED_tiktok_sku_transaction_grain.sql
-- STATUS: STAGED / NOT APPLIED to production or rehearsal. Phase 6C
-- Gate 5 revenue-residual fix — RECOMMENDED_DESIGN = 3,
-- TRANSACTION_IDEMPOTENCY_MODEL = H2 (revised 2026-09-21 after a
-- broader live duplicate-content probe — see below).
--
-- PROBLEM PROVEN LIVE: TikTok's finance_order_statement_transactions can
-- return MULTIPLE sku_transaction entries sharing the same
-- (order_id, sku_id) -- e.g. an original-sale line plus a separate
-- refund-adjustment line, each carrying its own statement_id (proven:
-- order 585474165265106454 returned 4 entries covering only 2 distinct
-- sku_id). core.fact_settlement_sku_fee's UPSERT key
-- (channel,shop_id,order_id,sku_id) has no statement_id component, so
-- writing each entry individually makes a later one silently overwrite
-- an earlier one and real revenue is lost.
--
-- WHY statement_id ALONE does not fix this (rejecting DESIGN 2): the
-- SAME order also proved TikTok can return two entries sharing the
-- IDENTICAL (sku_id, statement_id) pair with DIFFERENT settlement_amount
-- (one real, one an all-zero duplicate) -- (order_id, sku_id,
-- statement_id) is not a safe unique constraint either.
--
-- WHY content_hash ALONE (even over every field) is not globally unique
-- either (rejecting a naive UNIQUE(content_hash)): a 55-order / 120-entry
-- live probe (full canonical hash over statement_id, sku_id, sku_name,
-- product_name, quantity, settlement/revenue/shipping/fee_tax totals,
-- and all 3 breakdown objects) found 3 duplicate-hash groups, ALL
-- cross-order — the identical (sku_id=1735801927555253468,
-- statement_id=7672589526459598609, all-zero) entry appeared verbatim
-- on 2 different order_ids; likewise 2 more groups sharing
-- statement_id=7678517378749073170 across up to 3 different order_ids.
-- 0 duplicate-hash groups were found WITHIN a single order in this
-- sample, but per Task A2's instruction this is NOT proof it can never
-- happen — the schema below does not assume it.
--
-- DESIGN (TRANSACTION_IDEMPOTENCY_MODEL = H2): unique key is
-- (channel, shop_id, order_id, content_hash, occurrence_index).
-- order_id is a real column, NOT folded into the hash (the cross-order
-- duplicate content above must NOT collide). occurrence_index is the
-- 0-based rank of this exact content_hash among entries sharing it
-- WITHIN THE SAME order's own tx array, assigned deterministically by
-- pipelines/canonical_normalizer.py::assign_occurrence_indices() in the
-- API's own returned array order — proven live (4/4 orders, 2 separate
-- calls each) that this order is stable across re-fetch, which is what
-- makes occurrence_index reproducible/idempotent on replay rather than
-- an artifact of iteration. A true duplicate (2 byte-identical entries
-- in one order — not yet observed, but not assumed impossible) gets 2
-- rows (occurrence_index 0 and 1), never silently collapsed to 1.
--
-- TRANSACTION_TABLE_SEMANTICS = IMMUTABLE_EVENT_LEDGER (Task B1):
-- evidence — (a) re-fetch stability: 2 separate live calls per order
-- returned byte-identical tx arrays, 4/4 orders tested, no entry ever
-- observed to disappear or change value on re-fetch; (b) a "statement"
-- is, by ordinary e-commerce/accounting semantics, a discrete settlement
-- event that is generated once and stands as a permanent financial
-- record — a refund arrives as a NEW statement_id/entry, never an edit
-- to an old one. This table is therefore APPEND-ONLY: rows are only
-- ever inserted (ON CONFLICT DO NOTHING), never updated or deleted by
-- the regular write path. Residual risk this assumption is later found
-- wrong (e.g. TikTok silently retracts a previously-issued statement) is
-- not fully ruled out by this sample size/time window — a periodic
-- reconciliation job comparing "rows in this table but no longer
-- returned by a fresh API pull" is the recommended safety net if that
-- is ever observed, not built here since it hasn't been.
--
-- core.fact_settlement_sku_fee is UNCHANGED IN SHAPE (still one row per
-- (channel,shop_id,order_id,sku_id), still flat-object raw JSONB
-- columns) -- its structured/computed columns are written by SUMMING
-- every row in this table for that (order_id,sku_id) via
-- pipelines/canonical_normalizer.py::aggregate_tiktok_finance_skus().
-- Its raw JSONB columns become informational only (the entry with the
-- largest |settlement_amount| in the group) -- full lossless fidelity
-- lives here, not in that column. This deliberately avoids the
-- JSONB-array-shape change an earlier pass of this design considered
-- and rejected (Task A5): every existing `raw->'fee'->>'field'`-style
-- reader (sql/060, ad-hoc reconciliation queries) keeps working
-- unmodified against fact_settlement_sku_fee.
--
-- sql/060 IMPACT: see sql/063_STAGED_canonical_fee_components_view_v3.sql
-- (full replacement view, supersedes sql/060 v2 + incorporates the
-- sql/062 addendum) for the 2 fee_codes that must read this table
-- directly instead of fact_settlement_sku_fee's raw column.
-- =====================================================================

CREATE TABLE IF NOT EXISTS core.fact_settlement_sku_transaction (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel                 TEXT NOT NULL,
    shop_id                 TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    sku_id                  TEXT NOT NULL,
    statement_id            TEXT,             -- present on every observed entry, but NOT trusted as a unique key (see header) — kept for traceability/debugging only
    business_date           DATE,

    -- tx-level totals, exactly as TikTok returns them on each sku_transaction entry
    quantity                NUMERIC,
    settlement_amount       NUMERIC(18,4),
    revenue_amount          NUMERIC(18,4),
    shipping_cost_amount    NUMERIC(18,4),
    fee_tax_amount          NUMERIC(18,4),

    -- structured fee columns, same names/semantics as fact_settlement_sku_fee,
    -- computed per-entry via normalize_tiktok_finance_sku() (not aggregated --
    -- aggregation happens when fact_settlement_sku_fee is derived from this table)
    fixed_fee                          NUMERIC(18,4),
    payment_fee                        NUMERIC(18,4),
    vxp_fee                            NUMERIC(18,4),
    infrastructure_fee                 NUMERIC(18,4),
    affiliate_fee                      NUMERIC(18,4),
    affiliate_ads_commission_amount    NUMERIC(18,4),
    affiliate_partner_commission_amount NUMERIC(18,4),
    tap_shop_ads_commission_amount     NUMERIC(18,4),

    -- lossless raw, ONE entry per row (never an array -- this table IS the
    -- per-entry grain, so no shape change is needed here either)
    sku_name                    TEXT,
    product_name                TEXT,
    fee_tax_breakdown_raw       JSONB,
    revenue_breakdown_raw       JSONB,
    shipping_cost_breakdown_raw JSONB,

    -- H2 idempotency key components — see
    -- transaction_content_hash()/assign_occurrence_indices() in
    -- canonical_normalizer.py. content_hash deliberately does NOT
    -- include order_id (proven live: identical content legitimately
    -- repeats across different orders); order_id is this table's own
    -- column and is part of the composite unique constraint below.
    content_hash             TEXT NOT NULL,
    occurrence_index         INT NOT NULL DEFAULT 0,

    source_system            TEXT NOT NULL DEFAULT 'TIKTOK',
    source_endpoint          TEXT NOT NULL DEFAULT '/finance/202501/orders/{order_id}/statement_transactions',
    source_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    etl_run_id               UUID,

    CONSTRAINT uq_fact_settlement_sku_transaction_content
        UNIQUE (channel, shop_id, order_id, content_hash, occurrence_index)
);

CREATE INDEX IF NOT EXISTS ix_fact_settlement_sku_transaction_order_sku
    ON core.fact_settlement_sku_transaction (channel, shop_id, order_id, sku_id);
CREATE INDEX IF NOT EXISTS ix_fact_settlement_sku_transaction_business_date
    ON core.fact_settlement_sku_transaction (channel, business_date);
-- Debug/traceability only, NOT a uniqueness guarantee (proven non-unique live):
CREATE INDEX IF NOT EXISTS ix_fact_settlement_sku_transaction_statement
    ON core.fact_settlement_sku_transaction (channel, shop_id, statement_id);

-- Write pattern (implemented in incr_worker.py, NOT executed against
-- production this pass):
--   for tx, content_hash, occurrence_index in assign_occurrence_indices(sku_transactions):
--       INSERT INTO core.fact_settlement_sku_transaction (..., content_hash, occurrence_index, ...)
--       VALUES (..., %s, %s, ...)
--       ON CONFLICT (channel, shop_id, order_id, content_hash, occurrence_index) DO NOTHING;
-- Append-only — see TRANSACTION_TABLE_SEMANTICS above. Never UPDATE,
-- never DELETE, from the regular incremental write path.

-- fact_settlement_sku_fee's derivation query (for the write path to use
-- after inserting new transaction rows for an order, NOT executed here):
--   SELECT sku_id,
--          sum(fixed_fee), sum(payment_fee), sum(vxp_fee), sum(infrastructure_fee),
--          sum(affiliate_fee), sum(affiliate_ads_commission_amount),
--          sum(affiliate_partner_commission_amount), sum(tap_shop_ads_commission_amount)
--   FROM core.fact_settlement_sku_transaction
--   WHERE channel='TIKTOK' AND order_id=%s AND sku_id=%s
--   GROUP BY sku_id;
-- (computed_shipping_cost / computed_revenue follow the same sum-of-
-- per-entry-computed-value pattern via aggregate_tiktok_finance_skus,
-- called directly on the in-memory tx list at write time — no extra
-- round-trip needed for the order just fetched.)
