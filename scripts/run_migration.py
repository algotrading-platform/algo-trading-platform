# ============================================================
# scripts/run_migration.py
#
# One-off runner for migrations/002_gross_net_pnl_and_trailing_stop.sql
# — reuses the exact same connection method as core/database/db.py
# (DATABASE_URL from .env, psycopg2), so if the app connects fine,
# this will too.
#
# Usage:
#   python scripts/run_migration.py
#
# Safe to run more than once — every statement in the migration uses
# "ADD COLUMN IF NOT EXISTS", so re-running it is a no-op if the
# columns already exist.
# ============================================================

import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "migrations", "002_gross_net_pnl_and_trailing_stop.sql",
)


def get_conn_params() -> dict:
    """Same logic as core/database/db.py's _get_conn_params()."""
    url = os.getenv("DATABASE_URL", "")
    if url:
        return {"dsn": url}
    return {
        "host":     os.getenv("AZURE_DB_HOST", "ariqt-algo-trading-db-001.postgres.database.azure.com"),
        "port":     int(os.getenv("AZURE_DB_PORT", "5432")),
        "user":     os.getenv("AZURE_DB_USER", "algoadmin"),
        "password": os.getenv("AZURE_DB_PASSWORD", ""),
        "dbname":   os.getenv("AZURE_DB_NAME", "postgres"),
        "sslmode":  "require",
    }


def main():
    if not os.path.exists(MIGRATION_PATH):
        print(f"Migration file not found at: {MIGRATION_PATH}")
        print("Make sure migrations/002_gross_net_pnl_and_trailing_stop.sql "
              "is in the project root's migrations/ folder.")
        sys.exit(1)

    with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
        sql = f.read()

    print(f"Read migration file ({len(sql)} chars). Connecting...")

    params = get_conn_params()
    try:
        if "dsn" in params:
            conn = psycopg2.connect(params["dsn"])
        else:
            conn = psycopg2.connect(**params)
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Check DATABASE_URL (or AZURE_DB_* vars) in your .env file.")
        sys.exit(1)

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        print("Migration applied successfully.")
        print("New columns added to paper_positions: charges, net_pnl, "
              "peak_price, initial_stop_distance.")
    except Exception as e:
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()