-- =====================================================================
-- 110_roles.sql
-- HH_ECOM_AI_PILOT / hh_ecom — least-privilege roles.
--
-- *** CRITICAL PLATFORM PITFALL — READ BEFORE TOUCHING ROLES ***
-- Any role created through the Neon Console "Add role" UI (or the
-- equivalent Neon API role-creation endpoint) is silently enrolled as a
-- member of Neon's own `neon_superuser` role AND gets rolbypassrls =
-- true directly on the role itself. Both of these override every GRANT/
-- REVOKE in this file: a role in that state can read and write every
-- schema regardless of what SQL-level privileges say, because privilege
-- checks and Row-Level Security are bypassed for it. This is NOT
-- documented anywhere obvious in the console UI and was discovered the
-- hard way in this project — see
-- artifacts/v0/P1_DATABASE_FOUNDATION_REPORT.md, section
-- "ROLE SECURITY INCIDENT AND REMEDIATION" for the full incident.
--
-- Worse: once a role is created this way, the database owner (even the
-- project's own `neondb_owner`) CANNOT fix it in place —
-- `REVOKE neon_superuser FROM <role>`, `ALTER ROLE <role> NOINHERIT`,
-- and `DROP OWNED BY <role>` / `DROP ROLE <role>` (executed as SQL by
-- the owner) all fail with "permission denied" (SQLSTATE 42501),
-- because Neon's control plane — not the connecting Postgres owner —
-- administers console-created roles. The only way to remove such a role
-- is the Console's own "Delete role" button (Roles page → row menu →
-- Delete role), which goes through Neon's control plane rather than
-- raw SQL.
--
-- THE FIX / THE RULE FOR THIS PROJECT:
--   hh_ai_reader and hh_etl_writer must ALWAYS be created with plain
--   SQL CREATE ROLE (as the database owner, e.g. via psycopg2 or the
--   SQL Editor), never via the Neon Console "Add role" button. A
--   plain-SQL-created role does NOT get neon_superuser membership and
--   does NOT get rolbypassrls — it behaves like an ordinary Postgres
--   role and is fully controllable (ALTER/DROP/REVOKE all work
--   normally) by whoever created it.
--
--   hh_admin is the one exception: it is intentionally an
--   owner-equivalent role for this project, so console creation (and
--   whatever elevated defaults come with it) is acceptable there and
--   was not revisited.
--
-- Explicit attributes required on every application role other than
-- hh_admin, verified after creation with:
--   SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
--          rolbypassrls, pg_has_role(rolname,'neon_superuser','member')
--   FROM pg_roles WHERE rolname IN ('hh_ai_reader','hh_etl_writer');
--   -- every column must be false for both rows.
--
-- Passwords are never embedded in this file, never printed, and never
-- committed anywhere. Create/rotate them with a script that reads the
-- password from a local secret manager (this project uses the macOS
-- Keychain via the `keyring` package) and passes it to psycopg2 as a
-- bound parameter — never interpolated into SQL text, so it can never
-- appear in a query-echoed error message either. Reference pattern
-- (not executable as-is — %s is a psycopg2 bind parameter):
--
--   cur.execute(
--       "CREATE ROLE hh_etl_writer NOSUPERUSER NOCREATEDB NOCREATEROLE "
--       "NOREPLICATION NOBYPASSRLS LOGIN PASSWORD %s;",
--       (password_from_keychain,),
--   )
--
-- Rerun-safety: the DO block below only checks/reports; the GRANT/
-- REVOKE statements are idempotent by nature in Postgres. This file
-- intentionally does NOT contain CREATE ROLE statements (to avoid ever
-- inviting a literal password into a checked-in file) — run role
-- creation via the psycopg2 pattern above once per environment, then
-- apply the GRANTs below.
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_admin') THEN
        RAISE NOTICE 'hh_admin does not exist yet — create it via Neon Console > Roles (owner-equivalent role; console defaults are acceptable here).';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_etl_writer') THEN
        RAISE NOTICE 'hh_etl_writer does not exist yet — create it with plain SQL CREATE ROLE (see header comment), never via Neon Console > Roles.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_ai_reader') THEN
        RAISE NOTICE 'hh_ai_reader does not exist yet — create it with plain SQL CREATE ROLE (see header comment), never via Neon Console > Roles.';
    END IF;
    -- Defensive check: fail loudly (as a NOTICE — DO blocks can't easily
    -- abort a whole psql session) if either role somehow ended up a
    -- neon_superuser member, so this is never silently re-applied on
    -- top of a broken role.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_ai_reader')
       AND pg_has_role('hh_ai_reader', 'neon_superuser', 'member') THEN
        RAISE WARNING 'hh_ai_reader is a member of neon_superuser — it was created via the Neon Console, not plain SQL. Delete it via Console > Roles > Delete role and recreate with plain SQL CREATE ROLE before trusting any GRANT below.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_etl_writer')
       AND pg_has_role('hh_etl_writer', 'neon_superuser', 'member') THEN
        RAISE WARNING 'hh_etl_writer is a member of neon_superuser — it was created via the Neon Console, not plain SQL. Delete it via Console > Roles > Delete role and recreate with plain SQL CREATE ROLE before trusting any GRANT below.';
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- hh_admin — owner-equivalent
-- ---------------------------------------------------------------------
GRANT ALL PRIVILEGES ON SCHEMA control, core, mart, audit TO hh_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA control TO hh_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA core TO hh_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA mart TO hh_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit TO hh_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA control TO hh_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA core TO hh_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA mart TO hh_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA audit TO hh_admin;

ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA control GRANT ALL ON TABLES TO hh_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA core    GRANT ALL ON TABLES TO hh_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA mart    GRANT ALL ON TABLES TO hh_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA audit   GRANT ALL ON TABLES TO hh_admin;

-- ---------------------------------------------------------------------
-- hh_etl_writer — least-privilege ETL writer.
--
-- control: full CRUD (SELECT/INSERT/UPDATE/DELETE). DELETE is genuinely
--   needed here — the P1 connectivity test itself inserts a row into
--   control.etl_run_log, reads it back, and deletes it as cleanup, and
--   real ETL runs need to be able to expire/replace stale sync-state
--   and file-registry rows. TRUNCATE is explicitly revoked — no ETL
--   operation needs to wipe a whole control table.
-- core: SELECT/INSERT/UPDATE only (upsert pattern for dimensions and
--   facts). No DELETE — corrections happen via UPDATE or a new
--   ingested_at-versioned row, not by deleting history. No TRUNCATE.
-- audit: SELECT/INSERT only — an append-only trail. No UPDATE/DELETE/
--   TRUNCATE — once written, an audit/reconciliation record must not
--   be silently mutated by the same writer that produced it.
-- mart: no access at all. The Gold-build process is a separate, future
--   phase (P1 explicitly excludes populating mart) and will get its
--   own least-privilege grant when it exists — not folded into
--   hh_etl_writer by default.
-- ---------------------------------------------------------------------
GRANT CONNECT ON DATABASE hh_ecom TO hh_etl_writer;
GRANT USAGE ON SCHEMA control, core, audit TO hh_etl_writer;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA control TO hh_etl_writer;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO hh_etl_writer;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA audit TO hh_etl_writer;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA control TO hh_etl_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO hh_etl_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO hh_etl_writer;

ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA control GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hh_etl_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA core    GRANT SELECT, INSERT, UPDATE ON TABLES TO hh_etl_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA audit   GRANT SELECT, INSERT ON TABLES TO hh_etl_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA control GRANT USAGE, SELECT ON SEQUENCES TO hh_etl_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA core    GRANT USAGE, SELECT ON SEQUENCES TO hh_etl_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA audit   GRANT USAGE, SELECT ON SEQUENCES TO hh_etl_writer;

-- Explicit, defense-in-depth revokes:
REVOKE ALL ON SCHEMA mart FROM hh_etl_writer;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA control FROM hh_etl_writer;
REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA core FROM hh_etl_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA audit FROM hh_etl_writer;

-- ---------------------------------------------------------------------
-- hh_ai_reader — SELECT-only on mart. No INSERT/UPDATE/DELETE/CREATE/
-- ALTER/DROP privilege is ever granted to this role on anything, and it
-- has no access to control/core/audit at all.
-- ---------------------------------------------------------------------
GRANT CONNECT ON DATABASE hh_ecom TO hh_ai_reader;
GRANT USAGE ON SCHEMA mart TO hh_ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO hh_ai_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA mart GRANT SELECT ON TABLES TO hh_ai_reader;

-- Explicit, defense-in-depth revokes:
REVOKE ALL ON SCHEMA control, core, audit FROM hh_ai_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA mart FROM hh_ai_reader;
