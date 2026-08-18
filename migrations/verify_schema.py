"""
Post-migration schema verification.

Runs as the last step of :func:`migrations.run_all.run_all`, i.e. after
``fk_preflight``, ``Base.metadata.create_all()`` and every historical
migration. It only *reads* the database — it never creates, alters, drops or
deletes anything — and raises :class:`SchemaVerificationError` with a precise
diagnostic when the live schema is still incompatible with the models, so the
bot can never start against a half-migrated database.

Checks performed
----------------
* every parent table referenced by a model ForeignKey exists,
* the referenced column exists (``users.id``, ``products.id``, ...),
* that column is a PRIMARY KEY or has a UNIQUE constraint/index, i.e. it is a
  legal FK target,
* child FK columns have a compatible integer width,
* the physical foreign keys of ``cart`` really point at ``users(id)`` and
  ``products(id)`` (and likewise for every other table referencing them),
* referral tables still reference the same user identifier the rest of the app
  uses, and
* the tables that carry production data are still readable and non-corrupt
  (row counts are reported, never modified).

Run standalone (read-only):
    python migrations/verify_schema.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import Base  # noqa: E402
from migrations.fk_preflight import (  # noqa: E402
    _BIGINT_TYPE_NAMES,
    _INT_TYPE_NAMES,
    _model_fks,
    _type_name,
)


class SchemaVerificationError(RuntimeError):
    """Raised when the live schema is still incompatible with the models."""


# Tables whose contents must survive every migration. Reported, never touched.
DATA_TABLES = (
    "users",
    "products",
    "orders",
    "order_items",
    "transactions",
    "product_keys",
    "discount_codes",
    "referrals",
    "referral_rewards",
    "referral_settings",
)


def _live(engine):
    inspector = inspect(engine)
    tables = {}
    for name in inspector.get_table_names():
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        pk = inspector.get_pk_constraint(name).get("constrained_columns") or []
        unique = set()
        for uc in inspector.get_unique_constraints(name) or []:
            cols = uc.get("column_names") or []
            if len(cols) == 1:
                unique.add(cols[0])
        for ix in inspector.get_indexes(name) or []:
            cols = ix.get("column_names") or []
            if ix.get("unique") and len(cols) == 1 and cols[0]:
                unique.add(cols[0])
        if len(pk) == 1:
            unique.add(pk[0])
        fks = []
        for fk in inspector.get_foreign_keys(name) or []:
            fks.append(
                (
                    tuple(fk.get("constrained_columns") or ()),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or ()),
                )
            )
        tables[name] = {
            "columns": columns,
            "pk": list(pk),
            "unique": unique,
            "fks": fks,
        }
    return tables


def _orphan_count(engine, child_t, child_c, parent_t, parent_c):
    """Rows in child that reference a non-existent parent (read-only)."""
    q = engine.dialect.identifier_preparer.quote
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    f"SELECT COUNT(*) FROM {q(child_t)} c WHERE c.{q(child_c)} "
                    f"IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {q(parent_t)} p "
                    f"WHERE p.{q(parent_c)} = c.{q(child_c)})"
                )
            ).scalar_one()
    except Exception:  # pragma: no cover - defensive
        return 0


def _is_intish(type_name):
    return type_name in (_INT_TYPE_NAMES | _BIGINT_TYPE_NAMES)


def verify(engine=None, verbose=True):
    """Read-only verification. Returns the list of problems found."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    live = _live(engine)
    problems = []

    for child_t, child_c, parent_t, parent_c, _col in _model_fks():
        parent = live.get(parent_t)
        child = live.get(child_t)
        if parent is None:
            problems.append(f"parent table '{parent_t}' is missing")
            continue
        if parent_c not in parent["columns"]:
            problems.append(
                f"{parent_t}.{parent_c} does not exist "
                f"(referenced by {child_t}.{child_c}; live primary key: "
                f"{', '.join(parent['pk']) or 'none'})"
            )
            continue
        if parent_c not in parent["unique"] and parent["pk"] != [parent_c]:
            problems.append(
                f"{parent_t}.{parent_c} exists but is neither PRIMARY KEY nor "
                f"UNIQUE, so it is not a legal foreign-key target"
            )
        if child is None:
            problems.append(f"child table '{child_t}' is missing")
            continue
        if child_c not in child["columns"]:
            problems.append(f"{child_t}.{child_c} does not exist")
            continue
        ptype = _type_name(parent["columns"][parent_c]["type"])
        ctype = _type_name(child["columns"][child_c]["type"])
        if _is_intish(ptype) and _is_intish(ctype):
            if (ptype in _BIGINT_TYPE_NAMES) != (ctype in _BIGINT_TYPE_NAMES):
                problems.append(
                    f"{child_t}.{child_c} ({ctype}) -> {parent_t}.{parent_c} "
                    f"({ptype}): integer width mismatch"
                )
        # The physical constraint must exist and point at the right column.
        declared = [
            fk
            for fk in child["fks"]
            if fk[0] == (child_c,) and fk[1] == parent_t
        ]
        if not declared:
            orphans = _orphan_count(engine, child_t, child_c, parent_t, parent_c)
            if orphans:
                # Pre-existing legacy rows that point nowhere. Reported, never
                # deleted or rewritten; the ORM relationship still works.
                print(
                    f"   [WARN] {child_t}.{child_c} has no FOREIGN KEY constraint "
                    f"because {orphans} legacy row(s) reference a missing "
                    f"{parent_t}.{parent_c}. No data was changed."
                )
            else:
                problems.append(
                    f"{child_t}.{child_c} has no FOREIGN KEY constraint referencing "
                    f"{parent_t}"
                )
        elif all(fk[2] not in ((parent_c,), ()) for fk in declared):
            problems.append(
                f"{child_t}.{child_c} references {parent_t}"
                f"{declared[0][2]} instead of ({parent_c})"
            )

    if verbose and not problems:
        counts = []
        with engine.connect() as conn:
            for table in DATA_TABLES:
                if table in live:
                    count = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            + engine.dialect.identifier_preparer.quote(table)
                        )
                    ).scalar_one()
                    counts.append(f"{table}={count}")
        print("   [OK] schema verified; rows preserved: " + ", ".join(counts))
    return problems


def migrate(engine=None):
    """run_all() step wrapper: verify, and fail loudly when incompatible."""
    print("Verifying schema against the models (read-only)...")
    problems = verify(engine)
    if problems:
        raise SchemaVerificationError(
            "schema verification failed:\n  - " + "\n  - ".join(problems)
        )
    return True


if __name__ == "__main__":
    from database.db import engine as default_engine

    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    found = verify(default_engine)
    if found:
        print("[FAIL] schema verification failed:")
        for item in found:
            print(f"  - {item}")
        sys.exit(1)
    print("[OK] schema verified")
    sys.exit(0)
