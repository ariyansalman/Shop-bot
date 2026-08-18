"""
Migration: Add low_stock_threshold column to the products table.

Adds an integer column (default 3) used to trigger low-stock admin alerts.

Previously this migration only spoke raw sqlite3, so it was a no-op against a
production PostgreSQL database (and reported "database not found"). It now
runs through the SQLAlchemy engine, so the same code path works on SQLite and
PostgreSQL. It is additive and idempotent: it only ever issues ADD COLUMN, and
only when the column is genuinely missing.

Run with: python migrations/add_low_stock_threshold.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from config.settings import settings  # noqa: E402


def get_db_path():
    """Extract SQLite database path from DATABASE_URL (kept for compatibility)."""
    db_url = settings.DATABASE_URL
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
    return 'bot_database.db'


def migrate(engine=None):
    """Add products.low_stock_threshold if it doesn't exist yet."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    if "products" not in inspector.get_table_names():
        print("- products table does not exist yet, skipping")
        return True

    columns = {c["name"] for c in inspector.get_columns("products")}
    if "low_stock_threshold" in columns:
        print("- products.low_stock_threshold already exists, skipping")
        return True

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE products ADD COLUMN low_stock_threshold "
                    "INTEGER NOT NULL DEFAULT 3"
                )
            )
        print("✅ Added products.low_stock_threshold (default 3)")
        return True
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    ok = migrate()
    sys.exit(0 if ok else 1)
