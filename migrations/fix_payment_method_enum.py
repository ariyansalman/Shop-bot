"""
Migration: make the Postgres ``paymentmethod`` (and sibling) enum types agree
with what SQLAlchemy actually stores.

Background
----------
``Column(Enum(PaymentMethod))`` with a Python ``enum.Enum`` persists the enum
**member name** (``CRYPTO_WALLET``, ``BINANCE_PAY``, ...), *not* the member
value (``crypto_wallet``, ``binance_pay``, ...). That is SQLAlchemy's default
and it is what ``Base.metadata.create_all()`` emits in the ``CREATE TYPE``
DDL.

Earlier startup migrations in ``database/db.py`` added the lower-case *values*
to the live Postgres type instead:

    ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'binance_pay'

On a database whose type was created before those members existed, the labels
the ORM actually writes (``BINANCE_PAY``) were therefore never added, and any
insert failed with::

    invalid input value for enum paymentmethod: "BINANCE_PAY"

What this migration does (safe for a live database)
---------------------------------------------------
1. Adds every canonical label (the Python enum member *names*) to the existing
   Postgres enum types with ``ADD VALUE IF NOT EXISTS`` — additive only.
2. Re-maps any rows that were persisted with the legacy lower-case label back
   to the canonical name (``crypto_wallet`` -> ``CRYPTO_WALLET``, ...).
3. Leaves the stale lower-case labels in place. Postgres cannot remove an enum
   label without recreating the type, which would require dropping/rewriting
   the dependent columns — explicitly out of scope. Unused labels are inert.

Nothing is dropped, no type is recreated, no row is deleted. Running it twice
is a no-op. SQLite stores these columns as VARCHAR, so only step 2 applies
there and it is likewise idempotent.

Run with: python migrations/fix_payment_method_enum.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import (  # noqa: E402
    DiscountType,
    PaymentMethod,
    TransactionStatus,
)

# (python enum, postgres type name, [(table, column), ...])
ENUM_TARGETS = [
    (PaymentMethod, "paymentmethod", [("transactions", "payment_method")]),
    (TransactionStatus, "transactionstatus", [("transactions", "status")]),
    (DiscountType, "discounttype", [("discount_codes", "discount_type")]),
]


def canonical_labels(enum_cls):
    """The labels SQLAlchemy actually persists for this enum: member names."""
    return [member.name for member in enum_cls]


def legacy_label_map(enum_cls):
    """Legacy label -> canonical label, for values that differ from names."""
    return {
        member.value: member.name
        for member in enum_cls
        if member.value != member.name
    }


def _existing_tables(engine):
    return set(inspect(engine).get_table_names())


def add_missing_labels(engine, enum_cls, type_name):
    """Additively ensure every canonical label exists on the Postgres type."""
    # ALTER TYPE ... ADD VALUE cannot run inside a multi-statement transaction
    # block, so use an autocommit connection.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_type WHERE typname = :t"), {"t": type_name}
        ).scalar()
        if not exists:
            # Fresh database: create_all() will build the type with the correct
            # labels; nothing to patch.
            print(f"- {type_name}: type not present yet, skipping")
            return
        for label in canonical_labels(enum_cls):
            # Labels come from the Python enum definition only, never user input.
            conn.execute(
                text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")
            )
    print(f"[OK] {type_name}: canonical labels present "
          f"({', '.join(canonical_labels(enum_cls))})")


def remap_legacy_rows(engine, enum_cls, type_name, columns, is_sqlite):
    """Rewrite rows still holding a legacy lower-case label."""
    mapping = legacy_label_map(enum_cls)
    if not mapping:
        return

    tables = _existing_tables(engine)
    for table, column in columns:
        if table not in tables:
            continue
        with engine.begin() as conn:
            for legacy, canonical in mapping.items():
                if is_sqlite:
                    stmt = text(
                        f"UPDATE {table} SET {column} = :canonical "
                        f"WHERE {column} = :legacy"
                    )
                else:
                    # Cast through text so the comparison works even when the
                    # legacy label is still a valid member of the enum type.
                    stmt = text(
                        f"UPDATE {table} SET {column} = CAST(:canonical AS {type_name}) "
                        f"WHERE CAST({column} AS text) = :legacy"
                    )
                result = conn.execute(
                    stmt, {"canonical": canonical, "legacy": legacy}
                )
                if result.rowcount:
                    print(f"[OK] {table}.{column}: {result.rowcount} row(s) "
                          f"'{legacy}' -> '{canonical}'")


def migrate(engine=None):
    """Apply the corrective enum migration. Safe to run repeatedly."""
    if engine is None:
        from database.db import engine as default_engine
        engine = default_engine

    is_sqlite = engine.dialect.name == "sqlite"

    for enum_cls, type_name, columns in ENUM_TARGETS:
        if not is_sqlite:
            add_missing_labels(engine, enum_cls, type_name)
        remap_legacy_rows(engine, enum_cls, type_name, columns, is_sqlite)

    print("[OK] PaymentMethod enum migration complete")
    return True


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    ok = migrate()
    sys.exit(0 if ok else 1)
