-- =====================================================================
-- 001_schema.sql
-- HH_ECOM_AI_PILOT / hh_ecom — P1 database foundation
-- Schemas + control (operational metadata) + audit tables.
--
-- Timezone convention (project-wide):
--   Technical timestamps  -> TIMESTAMPTZ, stored/compared in UTC.
--   Business date         -> DATE, computed in Asia/Ho_Chi_Minh (NOT
--                             Asia/Bangkok). Callers must derive
--                             business_date from
--                             (timestamptz AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
--                             — this file does not enforce that in SQL,
--                             it is an ETL-layer contract.
--
-- Rerun-safety: every statement is IF NOT EXISTS / idempotent. Running
-- this file twice must succeed with no changes the second time and must
-- never drop an existing object.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()

-- ---------------------------------------------------------------------
-- control.etl_sync_state
-- One row per (source_system, source_endpoint, source_shop_id): the
-- current sync watermark ETL jobs read/advance.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control.etl_sync_state (
    id                  BIGSERIAL PRIMARY KEY,
    source_system       TEXT NOT NULL,
    source_endpoint     TEXT NOT NULL,
    source_shop_id      TEXT NOT NULL DEFAULT '',
    last_synced_at      TIMESTAMPTZ,
    last_business_date  DATE,
    last_cursor         TEXT,
    status              TEXT NOT NULL DEFAULT 'idle',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_etl_sync_state UNIQUE (source_system, source_endpoint, source_shop_id)
);

-- ---------------------------------------------------------------------
-- control.etl_run_log
-- One row per ETL job execution. etl_run_id is the FK target referenced
-- from every fact table's etl_run_id traceability column.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control.etl_run_log (
    etl_run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system    TEXT NOT NULL,
    source_endpoint  TEXT,
    source_shop_id   TEXT,
    run_type         TEXT NOT NULL DEFAULT 'incremental',
    business_date    DATE,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'running',
    rows_processed   BIGINT,
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_etl_run_log_source_started
    ON control.etl_run_log (source_system, started_at DESC);

-- ---------------------------------------------------------------------
-- control.data_quality_result
-- One row per DQ check execution.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control.data_quality_result (
    id             BIGSERIAL PRIMARY KEY,
    etl_run_id     UUID REFERENCES control.etl_run_log (etl_run_id),
    check_name     TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    business_date  DATE,
    status         TEXT NOT NULL,
    details        JSONB,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dq_result_table_date
    ON control.data_quality_result (table_name, business_date);

-- ---------------------------------------------------------------------
-- control.source_file_registry
-- Tracks file-based source drops (e.g. Excel exports) distinct from
-- direct API pulls.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control.source_file_registry (
    id             BIGSERIAL PRIMARY KEY,
    source_system  TEXT NOT NULL,
    file_name      TEXT NOT NULL,
    file_hash      TEXT NOT NULL DEFAULT '',
    business_date  DATE,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'received',
    etl_run_id     UUID REFERENCES control.etl_run_log (etl_run_id),
    CONSTRAINT uq_source_file_registry UNIQUE (source_system, file_name, file_hash)
);

-- ---------------------------------------------------------------------
-- control.system_health_daily
-- Per business_date/source_system/metric rollup of ETL health. Coverage
-- status must never be invented — 'unknown' until a real check sets it.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control.system_health_daily (
    business_date    DATE NOT NULL,
    source_system    TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC(18,4),
    coverage_status  TEXT NOT NULL DEFAULT 'unknown',
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_date, source_system, metric_name)
);

-- =====================================================================
-- audit schema
-- =====================================================================

-- ---------------------------------------------------------------------
-- audit.reconciliation_result
-- Cross-source reconciliation checks (e.g. order totals vs settlement).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.reconciliation_result (
    id              BIGSERIAL PRIMARY KEY,
    business_date   DATE NOT NULL,
    recon_type      TEXT NOT NULL,
    channel         TEXT,
    shop_id         TEXT,
    expected_value  NUMERIC(18,4),
    actual_value    NUMERIC(18,4),
    variance        NUMERIC(18,4),
    status          TEXT NOT NULL DEFAULT 'pending',
    details         JSONB,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_reconciliation_result UNIQUE (business_date, recon_type, channel, shop_id)
);

-- ---------------------------------------------------------------------
-- audit.data_change_log
-- Generic change trail for manual corrections / system overwrites on
-- any table in this database.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.data_change_log (
    id            BIGSERIAL PRIMARY KEY,
    table_name    TEXT NOT NULL,
    record_pk     TEXT NOT NULL,
    change_type   TEXT NOT NULL,
    changed_by    TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    old_value     JSONB,
    new_value     JSONB,
    etl_run_id    UUID REFERENCES control.etl_run_log (etl_run_id)
);

CREATE INDEX IF NOT EXISTS ix_data_change_log_table_pk
    ON audit.data_change_log (table_name, record_pk);
