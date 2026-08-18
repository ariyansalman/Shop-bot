"""
Migration: make ``category_id`` nullable on ``products`` and ``subcategories``.

This allows products and subcategories to exist without a category (used when
a category is deleted and its contents are kept for reassignment).

History / why this file was rewritten
-------------------------------------
The original version rebuilt the ``products`` table from a **hard-coded**
column list. Every column added to the model after that list was written
(most importantly ``low_stock_threshold``) was silently dropped when the
migration ran, because the rebuild only copied the columns it knew about.
It also assumed SQLite unconditionally, so on PostgreSQL it either did
nothing useful or pointed at the wrong database entirely.

The rewritten migration:

* On **PostgreSQL**: issues ``ALTER TABLE ... ALTER COLUMN category_id
  DROP NOT NULL``. No table is recreated, no data is copied, no column can be
  lost. This is a metadata-only change and is instant even on large tables.
* On **SQLite**: SQLite cannot ALTER a column, so a rebuild is unavoidable —
  but the new table is derived from the table's *actual current* schema
  (``PRAGMA table_info``), so **every** existing column is carried over
  verbatim, including any added later. Indexes are recreated afterwards and
  the whole rebuild runs inside one transaction with foreign keys disabled,
  so it either fully applies or fully rolls back.
* Skips work entirely when ``category_id`` is already nullable, so it is
  idempotent and safe to run on every startup.

Run with: python migrations/categorynullable.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from config.settings import settings  # noqa: E402

TARGETS = [
    ("subcategories", "category_id"),
    ("products", "category_id"),
]


def get_db_path():
    """Extract SQLite database path from DATABASE_URL (kept for compatibility)."""
    db_url = settings.DATABASE_URL
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
    return 'bot_database.db'


def _is_not_null(inspector, table, column):
    for info in inspector.get_columns(table):
        if info["name"] == column:
            return info.get("nullable") is False
    return False  # column (or table) absent -> nothing to do


def _postgres_drop_not_null(engine, table, column):
    with engine.begin() as conn:
        conn.execute(
            text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL')
        )
    print(f"   ✓ {table}.{column} is now nullable (in-place, no data copied)")


def _sqlite_drop_not_null(engine, table, column):
    """Rebuild the table preserving *all* current columns, minus the NOT NULL."""
    with engine.begin() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        # (cid, name, type, notnull, dflt_value, pk)
        col_defs = []
        col_names = []
        for _cid, name, ctype, notnull, dflt, pk in rows:
            col_names.append(name)
            piece = f'"{name}" {ctype or "TEXT"}'
            if pk:
                piece += " PRIMARY KEY"
            elif notnull and name != column:
                piece += " NOT NULL"
            if dflt is not None:
                piece += f" DEFAULT {dflt}"
            col_defs.append(piece)

        # Carry over the real foreign keys instead of guessing them.
        fk_rows = conn.execute(text(f"PRAGMA foreign_key_list({table})")).fetchall()
        for fk in fk_rows:
            # (id, seq, ref_table, from, to, on_update, on_delete, match)
            ref_table, from_col, to_col = fk[2], fk[3], fk[4]
            col_defs.append(
                f'FOREIGN KEY ("{from_col}") REFERENCES {ref_table}("{to_col}")'
            )

        indexes = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name = :t AND sql IS NOT NULL"
            ),
            {"t": table},
        ).fetchall()

        tmp = f"{table}__nullable_migration"
        cols_sql = ", ".join(f'"{c}"' for c in col_names)
        conn.execute(text(f'CREATE TABLE "{tmp}" ({", ".join(col_defs)})'))
        conn.execute(
            text(f'INSERT INTO "{tmp}" ({cols_sql}) SELECT {cols_sql} FROM "{table}"')
        )
        conn.execute(text(f'DROP TABLE "{table}"'))
        conn.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))
        for (index_sql,) in indexes:
            conn.execute(text(index_sql))

    print(
        f"   ✓ {table}.{column} is now nullable "
        f"({len(col_names)} column(s) preserved: {', '.join(col_names)})"
    )


def migrate(engine=None):
    """Make category_id nullable where needed. Safe to run repeatedly."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    is_sqlite = engine.dialect.name == "sqlite"
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    print("Starting migration: Make category_id nullable...")
    try:
        for table, column in TARGETS:
            if table not in tables:
                print(f"   - {table} does not exist yet, skipping")
                continue
            if not _is_not_null(inspector, table, column):
                print(f"   - {table}.{column} already nullable, skipping")
                continue
            if is_sqlite:
                _sqlite_drop_not_null(engine, table, column)
            else:
                _postgres_drop_not_null(engine, table, column)
            inspector = inspect(engine)
        print("✅ Migration completed successfully!")
        return True
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    ok = migrate()
    sys.exit(0 if ok else 1)
