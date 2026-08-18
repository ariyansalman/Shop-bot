"""
Migration: reconcile a *live* database with the current SQLAlchemy models.

Why this exists
---------------
``Base.metadata.create_all()`` only ever CREATEs tables that do not exist yet.
It never ALTERs an existing table, so every column, index or constraint added
to ``database/models.py`` after a deployment went live is silently missing on
that deployment's database. That is exactly how production ended up without
``products.low_stock_threshold`` while a fresh developer database had it.

What this module does
---------------------
It reads the *authoritative* schema straight from ``Base.metadata`` (so it can
never drift from the models again) and, for every table that already exists:

1. Adds any missing column with ``ALTER TABLE ... ADD COLUMN``, preserving the
   model's type and ``server_default`` so existing rows get a sensible value.
2. Creates any missing index / unique index declared on the model.
3. Relaxes ``NOT NULL`` where the model says the column is nullable
   (currently ``products.category_id`` and ``subcategories.category_id``).

Everything is additive and idempotent:

* No table is ever dropped, recreated or renamed.
* No column is ever dropped, renamed or retyped.
* No row is ever deleted or rewritten.
* Running it twice — or on a brand new database — is a no-op.

Works on PostgreSQL and on SQLite (dev). Safe to call on every startup.

Run standalone with: python migrations/schema_sync.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.schema import CreateIndex  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import Base  # noqa: E402

# Columns that are NOT NULL in the model but carry no server_default cannot be
# added to a table that already has rows without inventing a value. They are
# added as NULLable instead and reported, so nothing ever explodes mid-startup
# on a production database.
_UNSAFE_NOTNULL_NOTE = (
    "added as NULLable (model says NOT NULL but declares no server default; "
    "backfill manually if you need the constraint)"
)


def _quote(dialect, name):
    return dialect.identifier_preparer.quote(name)


def _server_default_sql(column):
    """Render the column's server_default as SQL text, if it has one."""
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if arg is None:
        return None
    if isinstance(arg, str):
        # A plain Python string server_default (e.g. server_default='en' or
        # server_default='3') is a *literal value*, so it must be emitted as a
        # quoted SQL literal. Postgres otherwise reads a bare word as a column
        # reference and rejects the DDL.
        return "'" + arg.replace("'", "''") + "'"
    # sqlalchemy.text() / func.now() and friends are raw SQL: emit as-is.
    return str(getattr(arg, "text", arg))


def _ensure_enum_type(conn, column):
    """Make sure a Postgres native ENUM type backing this column exists."""
    type_ = column.type
    if hasattr(type_, "create") and getattr(type_, "native_enum", False):
        try:
            type_.create(conn, checkfirst=True)
        except Exception:
            # Type already exists / not applicable for this dialect.
            pass


def _add_missing_columns(engine, table, existing_columns, report):
    dialect = engine.dialect
    for column in table.columns:
        if column.name in existing_columns:
            continue

        try:
            type_sql = column.type.compile(dialect)
        except Exception as exc:  # pragma: no cover - defensive
            report.append(
                f"!! {table.name}.{column.name}: cannot render type ({exc}), skipped"
            )
            continue

        pieces = [
            f"ALTER TABLE {_quote(dialect, table.name)} "
            f"ADD COLUMN {_quote(dialect, column.name)} {type_sql}"
        ]
        default_sql = _server_default_sql(column)
        note = ""
        if default_sql is not None:
            pieces.append(f"DEFAULT {default_sql}")
        if not column.nullable:
            if default_sql is not None:
                pieces.append("NOT NULL")
            else:
                note = f" ({_UNSAFE_NOTNULL_NOTE})"

        try:
            with engine.begin() as conn:
                _ensure_enum_type(conn, column)
                conn.execute(text(" ".join(pieces)))
        except Exception as exc:
            # Never let one column abort the whole sync: report and continue.
            report.append(f"!! {table.name}.{column.name}: ADD COLUMN failed ({exc})")
            continue
        report.append(f"[OK] {table.name}.{column.name} added{note}")


def _add_missing_indexes(engine, table, inspector, report):
    existing = {ix["name"] for ix in inspector.get_indexes(table.name) if ix.get("name")}
    # Unique constraints surface separately on some dialects.
    try:
        existing |= {
            uc["name"]
            for uc in inspector.get_unique_constraints(table.name)
            if uc.get("name")
        }
    except NotImplementedError:  # pragma: no cover
        pass

    current_columns = {c["name"] for c in inspector.get_columns(table.name)}

    for index in table.indexes:
        if index.name in existing:
            continue
        if not {c.name for c in index.columns} <= current_columns:
            report.append(
                f"!! index {index.name}: target column missing, skipped"
            )
            continue
        ddl = str(CreateIndex(index).compile(engine)).strip()
        # Belt and braces: IF NOT EXISTS is supported by both PostgreSQL and
        # SQLite, and makes a concurrent/duplicate run harmless.
        ddl = ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
        ddl = ddl.replace(
            "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1
        )
        with engine.begin() as conn:
            conn.execute(text(ddl))
        report.append(f"[OK] index {index.name} created on {table.name}")

    # Column-level unique=True without an explicit Index (e.g.
    # transactions.external_reference) — mirror it as a unique index so the
    # guarantee also holds on databases created before the column existed.
    for column in table.columns:
        if not column.unique or column.primary_key:
            continue
        if column.name not in current_columns:
            continue
        index_name = f"ix_{table.name}_{column.name}"
        if index_name in existing or any(
            index_name == ix.name for ix in table.indexes
        ):
            continue
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                    f"ON {_quote(engine.dialect, table.name)} "
                    f"({_quote(engine.dialect, column.name)})"
                )
            )
        report.append(f"[OK] unique index {index_name} ensured")


def _relax_not_null(engine, table, inspector, report):
    """Drop NOT NULL where the model declares the column nullable.

    PostgreSQL can do this in place. SQLite cannot ALTER a column at all, so
    the SQLite side is handled by migrations/categorynullable.py, which
    rebuilds the table from its *actual* current schema.
    """
    if engine.dialect.name != "postgresql":
        return

    live = {c["name"]: c for c in inspector.get_columns(table.name)}
    for column in table.columns:
        info = live.get(column.name)
        if info is None or column.primary_key:
            continue
        if column.nullable and info.get("nullable") is False:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"ALTER TABLE {_quote(engine.dialect, table.name)} "
                        f"ALTER COLUMN {_quote(engine.dialect, column.name)} "
                        f"DROP NOT NULL"
                    )
                )
            report.append(f"[OK] {table.name}.{column.name} NOT NULL dropped")


def migrate(engine=None, verbose=True):
    """Bring an existing database in line with the models. Idempotent."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    report = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # Brand new table: create_all() already handled it (fresh DB path).
            continue

        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        _add_missing_columns(engine, table, existing_columns, report)

        # Re-inspect: the columns we just added must be visible to the index
        # and NOT NULL passes below.
        inspector = inspect(engine)
        _add_missing_indexes(engine, table, inspector, report)
        _relax_not_null(engine, table, inspector, report)
        inspector = inspect(engine)

    if verbose:
        for line in report:
            print(line)
        print(
            f"[OK] Schema sync complete ({len(report)} change(s) applied)"
            if report
            else "[OK] Schema already up to date"
        )
    return report


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    migrate()
