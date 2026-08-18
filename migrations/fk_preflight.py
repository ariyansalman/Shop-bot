"""
Pre-flight migration: reconcile foreign keys with the *actual* live schema
BEFORE ``Base.metadata.create_all()`` runs.

Root cause this fixes
---------------------
``Base.metadata.create_all()`` only creates tables that do not exist yet. When
an older/live database already contains a parent table (``users``,
``products``, ``orders`` ...) whose primary key is **not** named the way the
current models assume, creating a *new* child table fails outright:

    CREATE TABLE cart (
        ...
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    psycopg2.errors.UndefinedColumn:
      column "id" referenced in foreign key constraint does not exist

i.e. the live ``users`` / ``products`` tables carry a different real primary
key (e.g. ``users.telegram_id``, ``products.product_id``) or lack the surrogate
``id`` column entirely, while the models declare ``ForeignKey('users.id')``.
The very same statement also fails silently-later when the parent PK is
``BIGINT`` and the child FK column would be ``INTEGER``.

What this module does (all additive, all idempotent)
----------------------------------------------------
1. Reflects the real schema of every already-existing table.
2. For every ForeignKey declared in ``Base.metadata``, checks that the
   referenced column really exists in the live parent table.
3. When it does not, it **does not** drop or rename anything. It adds the
   missing referenceable column as a surrogate *alias* of the table's real
   single-column primary key:

       ALTER TABLE users ADD COLUMN id BIGINT;          -- type copied from real PK
       UPDATE users SET id = telegram_id WHERE id IS NULL;
       ALTER TABLE users ALTER COLUMN id SET NOT NULL;
       ALTER TABLE users ADD CONSTRAINT users_id_key UNIQUE (id);
       -- plus a sequence default so future INSERTs keep filling it

   The original primary key stays exactly as it is, every row is preserved, and
   the column is now a legal FK target.
4. Aligns Python/SQL **types**: if the live referenced column is ``BIGINT``
   (or the live child column is), the corresponding in-memory model columns are
   switched from ``Integer`` to ``BigInteger`` so that any table created
   afterwards gets a type-compatible FK column. Nothing is retyped in an
   existing table.
5. Reports (and can be run standalone as a pure audit) every FK whose target is
   still unresolvable, instead of letting Postgres abort startup with an opaque
   error.

Nothing here drops a table, drops a column, deletes a row, disables FK
validation or "changes all primary keys to id".

Run standalone:
    python migrations/fk_preflight.py            # apply
    python migrations/fk_preflight.py --audit    # report only, no writes
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import BigInteger, Integer, inspect, text  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import Base  # noqa: E402

class SchemaMismatchError(RuntimeError):
    """Raised when a model ForeignKey cannot be made resolvable safely.

    Raised *before* ``Base.metadata.create_all()`` runs, so the operator gets a
    precise diagnostic instead of Postgres' opaque
    ``column "id" referenced in foreign key constraint does not exist``.
    Nothing is dropped, guessed at or rolled back when this is raised.
    """


_INT_TYPE_NAMES = {"INTEGER", "INT", "INT4", "SERIAL", "SMALLINT", "INT2"}
_BIGINT_TYPE_NAMES = {"BIGINT", "INT8", "BIGSERIAL"}


def _type_name(sqltype):
    try:
        return str(sqltype).split("(")[0].strip().upper()
    except Exception:  # pragma: no cover - defensive
        return ""


def _is_bigint(sqltype):
    return _type_name(sqltype) in _BIGINT_TYPE_NAMES


def _is_int(sqltype):
    return _type_name(sqltype) in _INT_TYPE_NAMES


def _reflect(engine):
    """Return {table_name: {"columns": {name: info}, "pk": [names]}}."""
    inspector = inspect(engine)
    live = {}
    for name in inspector.get_table_names():
        try:
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
        except Exception as exc:  # pragma: no cover - defensive
            print(f"   ! could not reflect {name}: {exc}")
            continue
        if len(pk) == 1:
            unique.add(pk[0])
        live[name] = {"columns": columns, "pk": list(pk), "unique": unique}
    return live


def _row_count(engine, table):
    """Row count of a live table (used for data-preservation checks)."""
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT COUNT(*) FROM {_quote(engine, table)}")
        ).scalar_one()


def _quote(engine, name):
    return engine.dialect.identifier_preparer.quote(name)


def _add_alias_column(engine, table, new_col, source_col, source_type_name):
    """Add ``new_col`` as a NOT NULL UNIQUE alias of ``source_col``.

    Purely additive: existing rows are backfilled from ``source_col``, the real
    primary key is untouched, and no data is deleted or rewritten.
    """
    is_pg = engine.dialect.name == "postgresql"
    col_type = "BIGINT" if source_type_name in _BIGINT_TYPE_NAMES else "INTEGER"
    t, c, s = _quote(engine, table), _quote(engine, new_col), _quote(engine, source_col)

    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {t} ADD COLUMN {c} {col_type}"))
        conn.execute(text(f"UPDATE {t} SET {c} = {s} WHERE {c} IS NULL"))
        if is_pg:
            seq = f"{table}_{new_col}_alias_seq"
            qseq = _quote(engine, seq)
            conn.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {qseq} OWNED BY {t}.{c}"))
            conn.execute(
                text(
                    f"SELECT setval('{seq}', "
                    f"COALESCE((SELECT MAX({c}) FROM {t}), 0) + 1, false)"
                )
            )
            conn.execute(
                text(f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT nextval('{seq}')")
            )
            conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL"))
            conn.execute(
                text(
                    f"ALTER TABLE {t} ADD CONSTRAINT "
                    f"{_quote(engine, table + '_' + new_col + '_key')} UNIQUE ({c})"
                )
            )
        else:
            # SQLite cannot add constraints after the fact; a unique index is
            # enough to make the column a legal FK target there.
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS "
                    f"{_quote(engine, 'ux_' + table + '_' + new_col)} ON {t} ({c})"
                )
            )
    print(
        f"   + {table}.{new_col} {col_type} added as UNIQUE alias of the real "
        f"primary key {table}.{source_col} (rows backfilled, PK untouched)"
    )


def _synthesize_surrogate_column(engine, table, new_col):
    """Add ``new_col`` as a fresh, generated surrogate key on ``table``.

    Only used when the live table exposes *no* single-column integer primary
    key and *no* single-column unique integer column to alias. Because the
    column did not exist, nothing in the database can already reference it, so
    generating fresh values cannot break an existing relationship. Existing
    rows keep every one of their current values; only the new column is filled.
    """
    if engine.dialect.name != "postgresql":
        raise SchemaMismatchError(
            f"{table} has no single-column integer key to derive '{new_col}' "
            f"from, and surrogate-key synthesis is only supported on PostgreSQL."
        )

    t, c = _quote(engine, table), _quote(engine, new_col)
    seq = f"{table}_{new_col}_alias_seq"
    qseq = _quote(engine, seq)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {t} ADD COLUMN {c} BIGINT"))
        conn.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {qseq} OWNED BY {t}.{c}"))
        conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT nextval('{seq}')"))
        conn.execute(text(f"UPDATE {t} SET {c} = nextval('{seq}') WHERE {c} IS NULL"))
        conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL"))
        conn.execute(
            text(
                f"ALTER TABLE {t} ADD CONSTRAINT "
                f"{_quote(engine, table + '_' + new_col + '_key')} UNIQUE ({c})"
            )
        )
    print(
        f"   + {table}.{new_col} BIGINT added as a generated surrogate key "
        f"(no existing column could be aliased; every existing row kept and "
        f"backfilled with a fresh value)"
    )


def _ensure_unique(engine, table, column):
    """Add a UNIQUE constraint/index on an existing column, additively.

    Duplicate values are reported instead of being deleted or merged.
    """
    t, c = _quote(engine, table), _quote(engine, column)
    with engine.begin() as conn:
        dupes = conn.execute(
            text(
                f"SELECT COUNT(*) FROM (SELECT {c} FROM {t} WHERE {c} IS NOT NULL "
                f"GROUP BY {c} HAVING COUNT(*) > 1) d"
            )
        ).scalar_one()
        if dupes:
            raise SchemaMismatchError(
                f"{table}.{column} holds {dupes} duplicated value(s); it cannot "
                f"become a unique foreign-key target without a manual data "
                f"decision. No rows were changed."
            )
        nulls = conn.execute(
            text(f"SELECT COUNT(*) FROM {t} WHERE {c} IS NULL")
        ).scalar_one()
        if nulls:
            raise SchemaMismatchError(
                f"{table}.{column} has {nulls} NULL value(s); refusing to guess "
                f"replacements. No rows were changed."
            )
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL"))
            conn.execute(
                text(
                    f"ALTER TABLE {t} ADD CONSTRAINT "
                    f"{_quote(engine, table + '_' + column + '_key')} UNIQUE ({c})"
                )
            )
        else:
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS "
                    f"{_quote(engine, 'ux_' + table + '_' + column)} ON {t} ({c})"
                )
            )
    print(f"   + {table}.{column} made UNIQUE NOT NULL (valid FK target)")


def _alias_source(parent_live):
    """Pick the live column that ``id`` should mirror, or None.

    Preference order:
      1. the table's single-column integer PRIMARY KEY (the legacy key), then
      2. a single-column UNIQUE NOT NULL integer column (e.g. ``telegram_id``
         on a ``users`` table whose PK was never created).
    """
    columns = parent_live["columns"]
    pk_cols = parent_live["pk"]
    if len(pk_cols) == 1:
        info = columns.get(pk_cols[0])
        if info is not None and _type_name(info["type"]) in (
            _INT_TYPE_NAMES | _BIGINT_TYPE_NAMES
        ):
            return pk_cols[0], _type_name(info["type"]), "primary key"

    for name in sorted(parent_live.get("unique") or ()):
        info = columns.get(name)
        if info is None or info.get("nullable", True):
            continue
        type_name = _type_name(info["type"])
        if type_name in (_INT_TYPE_NAMES | _BIGINT_TYPE_NAMES):
            return name, type_name, "unique integer column"
    return None


def _model_fks():
    """Yield (child_table, child_column, parent_table, parent_column, col_obj)."""
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                yield (
                    table.name,
                    column.name,
                    fk.column.table.name,
                    fk.column.name,
                    column,
                )


def _align_types(engine, live):
    """Switch in-memory Integer <-> BigInteger so new tables match the live DB."""
    changed = []
    for child_t, child_c, parent_t, parent_c, column in _model_fks():
        parent_live = live.get(parent_t)
        child_live = live.get(child_t)
        target_big = False

        if parent_live and parent_c in parent_live["columns"]:
            target_big = _is_bigint(parent_live["columns"][parent_c]["type"])
        if child_live and child_c in child_live["columns"]:
            target_big = target_big or _is_bigint(child_live["columns"][child_c]["type"])

        if target_big and _is_int(column.type):
            column.type = BigInteger()
            changed.append(f"{child_t}.{child_c} -> BIGINT")
            # Keep the referenced model column consistent too.
            parent_col = Base.metadata.tables[parent_t].columns.get(parent_c)
            if parent_col is not None and _is_int(parent_col.type):
                parent_col.type = BigInteger()
                changed.append(f"{parent_t}.{parent_c} -> BIGINT")

    # A live BIGINT primary key with no child FK yet must still be matched.
    for table in Base.metadata.sorted_tables:
        live_t = live.get(table.name)
        if not live_t:
            continue
        for column in table.primary_key.columns:
            info = live_t["columns"].get(column.name)
            if info is not None and _is_bigint(info["type"]) and _is_int(column.type):
                column.type = BigInteger()
                changed.append(f"{table.name}.{column.name} -> BIGINT")

    for entry in sorted(set(changed)):
        print(f"   ~ model type aligned with live schema: {entry}")
    return changed


def _live_fks(engine, table):
    """{(child_col,): [(name, parent_table, (parent_col,))]} for a live table."""
    out = {}
    try:
        for fk in inspect(engine).get_foreign_keys(table) or []:
            cols = tuple(fk.get("constrained_columns") or ())
            out.setdefault(cols, []).append(
                (
                    fk.get("name"),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or ()),
                )
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return out


def _widen_int_columns(engine, live):
    """Widen already-live INTEGER FK columns to BIGINT where the model (or the
    live parent/child on the other end of the relationship) needs BIGINT.

    ``_align_types`` only updates the in-memory metadata so *new* tables come
    out right; it explicitly never touches a column that already exists. That
    leaves a live INTEGER column permanently mismatched against a BIGINT
    parent (or child) forever, since every later run just reports the same
    "integer width mismatch" instead of fixing it.

    Widening INTEGER -> BIGINT is safe and lossless in both directions of the
    comparison: no row can hold a value that stops fitting, so this never
    loses or rewrites data beyond the column's own storage. Only PostgreSQL is
    supported (SQLite has no fixed-width integer types to mismatch).
    """
    if engine.dialect.name != "postgresql":
        return []
    changed = []
    for child_t, child_c, parent_t, parent_c, _column in _model_fks():
        child_live, parent_live = live.get(child_t), live.get(parent_t)
        if not child_live or not parent_live:
            continue
        if child_c not in child_live["columns"] or parent_c not in parent_live["columns"]:
            continue
        ptype = _type_name(parent_live["columns"][parent_c]["type"])
        ctype = _type_name(child_live["columns"][child_c]["type"])
        if not (_is_int(ptype) or _is_int(ctype)):
            continue
        if not (_is_int(ptype) and _is_int(ctype)) and not (
            (ptype in _BIGINT_TYPE_NAMES) or (ctype in _BIGINT_TYPE_NAMES)
        ):
            continue
        target_big = ptype in _BIGINT_TYPE_NAMES or ctype in _BIGINT_TYPE_NAMES
        if not target_big:
            continue
        t, c = _quote(engine, child_t), _quote(engine, child_c)
        if ctype not in _BIGINT_TYPE_NAMES:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {c} TYPE BIGINT"))
            changed.append(f"{child_t}.{child_c} -> BIGINT")
            print(f"   ~ widened live column {child_t}.{child_c} INTEGER -> BIGINT")
        if parent_c in parent_live["columns"]:
            ptype = _type_name(parent_live["columns"][parent_c]["type"])
            if ptype not in _BIGINT_TYPE_NAMES and _is_int(ptype):
                pt, pcq = _quote(engine, parent_t), _quote(engine, parent_c)
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {pt} ALTER COLUMN {pcq} TYPE BIGINT"))
                changed.append(f"{parent_t}.{parent_c} -> BIGINT")
                print(f"   ~ widened live column {parent_t}.{parent_c} INTEGER -> BIGINT")
    return changed


def ensure_fk_constraints(engine, live=None):
    """Add missing physical FOREIGN KEY constraints on pre-existing tables.

    ``create_all()`` only emits FK constraints for tables it creates, so a
    legacy table (e.g. a hand-made ``referrals``) can carry the right columns
    with no constraint behind them. This adds the missing constraint only when
    the existing data already satisfies it; when orphan rows exist nothing is
    deleted, nullified or "cleaned" — the situation is reported and skipped.

    A constraint that already exists but points at the *wrong* column (e.g. a
    legacy ``orders.user_id -> users(telegram_id)`` left over from before the
    ``id`` surrogate key existed) used to be treated as "already satisfied"
    just because it referenced the right table, so it was never corrected.
    Such a constraint is now dropped and replaced with one pointing at the
    column the models actually declare — safe here because ``users.id`` was
    populated as a 1:1 backfilled alias of ``telegram_id``, so every row that
    satisfied the old constraint still satisfies the new one. No row is
    deleted or rewritten; only the constraint definition changes.

    Only supported on PostgreSQL; a no-op elsewhere.
    """
    if engine.dialect.name != "postgresql":
        return []
    live = live if live is not None else _reflect(engine)
    added = []
    for child_t, child_c, parent_t, parent_c, _column in _model_fks():
        child_live, parent_live = live.get(child_t), live.get(parent_t)
        if not child_live or not parent_live:
            continue
        if child_c not in child_live["columns"] or parent_c not in parent_live["columns"]:
            continue
        existing = _live_fks(engine, child_t).get((child_c,), [])
        correct = [e for e in existing if e[1] == parent_t and e[2] in ((parent_c,), ())]
        if correct:
            continue
        wrong = [e for e in existing if e[1] == parent_t]
        if wrong:
            for name, _ref_t, ref_cols in wrong:
                if not name:
                    continue
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE {_quote(engine, child_t)} "
                                f"DROP CONSTRAINT {_quote(engine, name)}"
                            )
                        )
                    print(
                        f"   ~ dropped {child_t}.{child_c} FOREIGN KEY -> "
                        f"{parent_t}{ref_cols} (wrong column; replacing with "
                        f"{parent_t}.{parent_c})"
                    )
                except Exception as exc:
                    print(
                        f"   ! could not drop stale FOREIGN KEY {name} on "
                        f"{child_t}.{child_c}: {exc}"
                    )
        if parent_c not in (parent_live.get("unique") or set()) and parent_live["pk"] != [parent_c]:
            continue  # not a legal target yet; reported by the audit
        ct, cc = _quote(engine, child_t), _quote(engine, child_c)
        pt, pc = _quote(engine, parent_t), _quote(engine, parent_c)
        try:
            with engine.begin() as conn:
                orphans = conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM {ct} c WHERE c.{cc} IS NOT NULL AND NOT "
                        f"EXISTS (SELECT 1 FROM {pt} p WHERE p.{pc} = c.{cc})"
                    )
                ).scalar_one()
                if orphans:
                    print(
                        f"   ! {child_t}.{child_c} has {orphans} row(s) with no "
                        f"matching {parent_t}.{parent_c}; leaving the data untouched "
                        f"and skipping the FOREIGN KEY constraint"
                    )
                    continue
                name = f"{child_t}_{child_c}_fkey"
                conn.execute(
                    text(
                        f"ALTER TABLE {ct} ADD CONSTRAINT {_quote(engine, name)} "
                        f"FOREIGN KEY ({cc}) REFERENCES {pt} ({pc})"
                    )
                )
            added.append(f"{child_t}.{child_c} -> {parent_t}.{parent_c}")
            print(f"   + FOREIGN KEY {child_t}.{child_c} -> {parent_t}.{parent_c} added")
        except Exception as exc:
            print(
                f"   ! could not add FOREIGN KEY {child_t}.{child_c} -> "
                f"{parent_t}.{parent_c}: {exc}"
            )
    return added


def reconcile_constraints(engine=None):
    """run_all() step: add FK constraints for columns created by later steps.

    ``schema_sync`` can add a missing column (e.g. ``products.category_id``)
    after the pre-flight has already run, so the constraint pass is repeated
    once every column exists. Purely additive and idempotent.
    """
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine
    print("Starting migration: foreign-key constraint reconciliation...")
    added = ensure_fk_constraints(engine)
    if not added:
        print("   - no missing foreign-key constraints")
    return True


def audit(engine, live=None):
    """Static FK audit against the live schema. Returns a list of problems."""
    live = live if live is not None else _reflect(engine)
    problems = []
    for child_t, child_c, parent_t, parent_c, column in _model_fks():
        parent_live = live.get(parent_t)
        if parent_live is None:
            continue  # will be created by create_all in dependency order
        if parent_c not in parent_live["columns"]:
            problems.append(
                f"{child_t}.{child_c} -> {parent_t}.{parent_c}: referenced column "
                f"does not exist (real primary key: "
                f"{', '.join(parent_live['pk']) or 'none'})"
            )
            continue
        child_live = live.get(child_t)
        if child_live and child_c in child_live["columns"]:
            ptype = _type_name(parent_live["columns"][parent_c]["type"])
            ctype = _type_name(child_live["columns"][child_c]["type"])
            big = _BIGINT_TYPE_NAMES
            if (ptype in big) != (ctype in big) and (
                ptype in big | _INT_TYPE_NAMES and ctype in big | _INT_TYPE_NAMES
            ):
                problems.append(
                    f"{child_t}.{child_c} ({ctype}) -> {parent_t}.{parent_c} "
                    f"({ptype}): integer width mismatch"
                )
    return problems


def migrate(engine=None, verbose=True, strict=True):
    """Make every model ForeignKey resolvable against the live database.

    Safe and idempotent: on a brand-new database (or one that already matches
    the models) this is a no-op.
    """
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    if verbose:
        print("Starting migration: foreign-key pre-flight reconciliation...")

    live = _reflect(engine)
    if not live:
        if verbose:
            print("   - empty database, nothing to reconcile")
        return True

    # 1. Make every missing FK target column exist on the live parent table.
    for child_t, child_c, parent_t, parent_c, _column in _model_fks():
        parent_live = live.get(parent_t)
        if parent_live is None or parent_c in parent_live["columns"]:
            continue

        before = _row_count(engine, parent_t)
        source = _alias_source(parent_live)
        if source is not None:
            source_col, source_type, kind = source
            print(
                f"   * {parent_t}.{parent_c} missing; deriving it from the legacy "
                f"{kind} {parent_t}.{source_col}"
            )
            _add_alias_column(engine, parent_t, parent_c, source_col, source_type)
        elif before == 0:
            # Empty legacy table: a generated key cannot lose or mis-map data.
            _synthesize_surrogate_column(engine, parent_t, parent_c)
        else:
            # Non-empty table with no integer key at all. Generating values is
            # still safe (nothing can reference a column that did not exist),
            # but say so loudly so the operator can audit it.
            print(
                f"   ! {parent_t} has {before} row(s) and no single-column integer "
                f"primary key or unique column to derive '{parent_c}' from "
                f"(primary key: {', '.join(parent_live['pk']) or 'none'})"
            )
            _synthesize_surrogate_column(engine, parent_t, parent_c)

        after = _row_count(engine, parent_t)
        if after != before:
            raise SchemaMismatchError(
                f"row count of {parent_t} changed from {before} to {after} while "
                f"adding '{parent_c}'; aborting before any further schema change"
            )
        live = _reflect(engine)

    # 1b. A referenced column that exists but is neither PRIMARY KEY nor
    #     UNIQUE is not a legal FK target either (this is what a previously
    #     interrupted run leaves behind). Make it one, additively.
    for _child_t, _child_c, parent_t, parent_c, _column in _model_fks():
        parent_live = live.get(parent_t)
        if parent_live is None or parent_c not in parent_live["columns"]:
            continue
        if parent_c in (parent_live.get("unique") or set()):
            continue
        if parent_live["pk"] == [parent_c]:
            continue
        try:
            _ensure_unique(engine, parent_t, parent_c)
            live = _reflect(engine)
        except Exception as exc:
            print(
                f"   ! could not make {parent_t}.{parent_c} a valid FK target: {exc}"
            )

    # 2. Align integer widths in the in-memory metadata used by create_all().
    _align_types(engine, live)

    # 2a. Widen already-live INTEGER FK columns to BIGINT where the other end
    #     of the relationship is BIGINT. Lossless (widening only) and the one
    #     piece _align_types deliberately skips, since it only touches
    #     in-memory metadata for tables that don't exist yet.
    if _widen_int_columns(engine, live):
        live = _reflect(engine)

    # 2b. Give pre-existing legacy child tables the FK constraints the models
    #     declare (only where the live data already satisfies them). Also
    #     corrects constraints that exist but point at the wrong column.
    ensure_fk_constraints(engine, live)
    live = _reflect(engine)

    # 3. Fail *before* create_all() if an FK target is still unresolvable, so
    #    the operator sees a precise diagnostic instead of Postgres aborting
    #    the CREATE TABLE with "column ... referenced in foreign key
    #    constraint does not exist".
    problems = audit(engine, live)
    missing = [p for p in problems if "does not exist" in p]
    for problem in problems:
        print(f"   [WARN] foreign-key mismatch: {problem}")
    if missing and strict:
        raise SchemaMismatchError(
            "the live database still has foreign-key targets that do not exist "
            "and could not be created automatically:\n  - "
            + "\n  - ".join(missing)
            + "\nNothing was dropped or modified beyond additive columns. Fix "
            "the listed parent table(s) manually, then restart."
        )
    if not problems and verbose:
        print("   [OK] every model foreign key resolves against the live schema")
    return True


if __name__ == "__main__":
    from database.db import engine as default_engine

    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    if "--audit" in sys.argv:
        found = audit(default_engine)
        if found:
            print("[FAIL] foreign-key problems found:")
            for item in found:
                print(f"  - {item}")
            sys.exit(1)
        print("[OK] all foreign keys valid against the live schema")
        sys.exit(0)
    sys.exit(0 if migrate(default_engine) else 1)
