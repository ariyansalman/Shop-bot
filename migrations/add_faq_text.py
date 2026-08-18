"""
Migration: Add faq_text column to the settings table.

Adds a TEXT column holding the admin-editable FAQ shown by the ❓ FAQ button.

Previously this migration only spoke raw sqlite3, so it was a no-op against a
production PostgreSQL database. It now runs through the SQLAlchemy engine, so
the same code path works on SQLite and PostgreSQL. Additive and idempotent.

Run with: python migrations/add_faq_text.py
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
    """Add settings.faq_text if it doesn't exist yet."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        print("- settings table does not exist yet, skipping")
        return True

    columns = {c["name"] for c in inspector.get_columns("settings")}
    if "faq_text" in columns:
        print("- settings.faq_text already exists, skipping")
        return True

    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE settings ADD COLUMN faq_text TEXT"))
        print("✅ Added settings.faq_text")
        return True
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    ok = migrate()
    sys.exit(0 if ok else 1)
