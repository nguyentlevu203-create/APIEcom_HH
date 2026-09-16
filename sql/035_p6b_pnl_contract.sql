-- =====================================================================
-- 035_p6b_pnl_contract.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P6B P&L metric contract catalog.
--
-- Static reference table (not date-grained) recording, per metric x
-- channel, the recovered/proven business formula, its exact source
-- (file/function/API field), and its current availability — so future
-- work (P6C+, MCP) never has to re-derive a formula that already has a
-- proven source. No secrets/credentials stored. Additive only.
-- =====================================================================
CREATE TABLE IF NOT EXISTS mart.gold_metric_contract (
    id                    BIGSERIAL PRIMARY KEY,
    metric_name           TEXT NOT NULL,
    channel               TEXT NOT NULL,
    business_definition   TEXT NOT NULL,
    formula_expression    TEXT NOT NULL,
    source_type           TEXT NOT NULL,   -- API_ACTUAL | SETTLEMENT_ACTUAL | HH_DERIVED_RULE | HH_MANUAL_INPUT | SEPARATE_API_REQUIRED | NO_PERMISSION | MISSING_SOURCE
    source_table          TEXT,
    source_field          TEXT,
    value_basis           TEXT NOT NULL,   -- API_ACTUAL | DERIVED_HH_RULE | CANDIDATE_UNVALIDATED
    date_basis            TEXT NOT NULL,   -- order business_date | settlement business_date, etc.
    status_basis          TEXT,            -- which upstream statuses are included (e.g. finance_state list)
    availability_status   TEXT NOT NULL,
    approved_rule_source  TEXT,            -- file path of the proof/approval document
    pnl_view              TEXT NOT NULL DEFAULT 'MANAGEMENT_PNL',  -- MANAGEMENT_PNL | SETTLEMENT_ACTUAL
    version               TEXT NOT NULL DEFAULT 'V1',
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_gold_metric_contract UNIQUE (metric_name, channel, pnl_view, version)
);
