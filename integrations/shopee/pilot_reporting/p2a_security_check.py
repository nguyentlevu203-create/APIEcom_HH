#!/usr/bin/env python3
"""P2A security check — reconfirm P1 role security state is unchanged."""
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
    conn.close()


if __name__ == "__main__":
    main()
