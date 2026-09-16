#!/usr/bin/env python3
"""P2B security check — reconfirm P1 role security state is unchanged
after the additive migration + TikTok load. Mirrors
../../shopee/pilot_reporting/p2a_security_check.py exactly."""
import keyring
import psycopg2


def main() -> None:
    owner_url = keyring.get_password("HH_ECOM_NEON", "neondb_owner_database_url")
    conn = psycopg2.connect(owner_url)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rolname, rolbypassrls, pg_has_role(rolname, 'neon_superuser', 'member')
        FROM pg_roles WHERE rolname IN ('hh_ai_reader','hh_etl_writer')
        ORDER BY rolname;
        """
    )
    for row in cur.fetchall():
        print(row)

    # hh_ai_reader must still be read-only: no INSERT/UPDATE/DELETE grants.
    cur.execute(
        """
        SELECT grantee, table_name, privilege_type
        FROM information_schema.role_table_grants
        WHERE grantee = 'hh_ai_reader' AND table_schema IN ('core','mart')
          AND privilege_type IN ('INSERT','UPDATE','DELETE')
        ORDER BY table_name, privilege_type;
        """
    )
    write_grants = cur.fetchall()
    print(f"hh_ai_reader write grants on core/mart (expect none): {write_grants}")

    conn.close()


if __name__ == "__main__":
    main()
