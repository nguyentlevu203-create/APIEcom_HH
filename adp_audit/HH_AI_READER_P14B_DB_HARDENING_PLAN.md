# hh_ai_reader — defense-in-depth hardening (PLAN ONLY, NOT APPLIED)

P14-B, 2026-10-06. Nothing in this file has been executed against Neon. Applying it is a
separate checkpoint that needs explicit approval.

## Current state (read-only catalog check, 2026-10-06)

- `hh_ai_reader`: SELECT only, on exactly 29 `mart` objects. No INSERT/UPDATE/DELETE/TRUNCATE,
  no USAGE on `core`/`control`/`audit`, no CREATE on the database or any schema, not a
  superuser, not a member of any role, owns nothing.
- `rolconfig` is empty for every application role, so `default_transaction_read_only` is not set.
- TEMP is not granted to `hh_ai_reader` directly. It comes from the default PUBLIC grant:
  `datacl = {=Tc/neondb_owner, …}`. Every role has TEMP that way: `hh_ai_reader`,
  `hh_etl_writer`, `hh_admin`, `cloud_admin`.
- `git grep` finds no `CREATE TEMP`/`TEMPORARY`/`ON COMMIT DROP` in any tracked `.py`/`.sql`,
  so no pipeline depends on temp tables today.

## A. `ALTER ROLE hh_ai_reader SET default_transaction_read_only = on`

Evidence gathered without changing the role. The same setting was applied per session through
the connection startup option `-c default_transaction_read_only=on`, connected as `hh_ai_reader`:

- All 7 Worker tools (12 calls: overview ×3, cost ×2, video ×2, live ×2, affiliate, operations,
  coverage) ran with **0 errors**. Their output was **identical** (12/12) to a normal session.
- The Worker's own `SET statement_timeout` still works, because SET is allowed in a read-only
  transaction.
- A `CREATE TEMP TABLE` in that session was rejected: `ReadOnlySqlTransaction`.

Assessment: **safe, recommended.** It is defense in depth, not a boundary. A session could still
run `SET default_transaction_read_only = off` itself. The Worker never does that and exposes no
SQL input, but the GRANT model remains the real control.

Hyperdrive note: a role-level setting applies to every new backend session for that user, and
does not depend on Hyperdrive forwarding startup options. That is why the ALTER ROLE form is
preferred over a connection-string option.

## B. TEMP

Removing TEMP from `hh_ai_reader` alone is impossible while PUBLIC holds it. The smallest
change that keeps every other role's behavior is:

```sql
REVOKE TEMPORARY ON DATABASE hh_ecom FROM PUBLIC;
GRANT  TEMPORARY ON DATABASE hh_ecom TO hh_etl_writer, hh_admin;  -- unchanged for them
```

Assessment: **optional, low value once A is applied**, because a read-only transaction already
blocks temp-table creation. Only do it together with A, and only after confirming that no
out-of-repo tooling (Neon console, ad-hoc analysis) running as another role relies on TEMP via
PUBLIC. `cloud_admin`/`neondb_owner` keep their rights as superuser or owner.

## C. Proof plan when applying (separate approval)

1. Apply A (and B, if approved) in one transaction as `neondb_owner`. Save `pg_roles.rolconfig`
   and `pg_database.datacl` before and after.
2. As `hh_ai_reader`: `SHOW default_transaction_read_only` → `on`. A write probe must fail.
   `has_database_privilege('hh_ai_reader','hh_ecom','TEMP')` → false (if B).
3. Run the 7-tool read-only harness (the same 12 calls) through the deployed Worker →
   0 errors, output unchanged apart from fields expected to move with the data.
4. Run the ETL cycle once as normal (`hh_etl_writer`), and the healthcheck. Neither must change.

Rollback:

```sql
ALTER ROLE hh_ai_reader RESET default_transaction_read_only;
GRANT TEMPORARY ON DATABASE hh_ecom TO PUBLIC;  -- only if B was applied
```
