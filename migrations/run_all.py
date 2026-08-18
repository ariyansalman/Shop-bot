"""
Single entry point that applies every historical migration, in order.

``database.db.init_db()`` calls :func:`run_all` after
``Base.metadata.create_all()``, so a bot start always leaves the database in
the shape the models expect — whether the database is brand new or years old.

Ordering rationale
------------------
0. ``fk_preflight`` – reconciles every model ForeignKey with the *actual* live
   schema (missing FK target columns are added as additive aliases of the real
   primary key, INTEGER/BIGINT widths are aligned). Must run first: everything
   else assumes the FK graph is valid.
1. ``categorynullable``  – relax NOT NULL on category_id (on SQLite this
   rebuilds the table, so it must run *before* anything that depends on newly
   created indexes; it preserves every column it finds).
2. ``add_low_stock_threshold`` / ``add_faq_text`` – the explicit, historical
   single-column migrations, kept intact for auditability.
2b. ``add_referral`` – creates the referral program tables (referrals,
   referral_rewards, referral_settings) and seeds the settings row.
3. ``schema_sync`` – the model-driven catch-all: adds any remaining missing
   column / index / nullability change derived from ``Base.metadata``. This is
   what prevents the "column added to the model but never to production"
   class of bug from recurring.
4. ``fix_payment_method_enum`` – keeps Postgres native enum labels in sync.
5. ``verify_schema`` – read-only final gate. Confirms every model ForeignKey
   target (``users.id``, ``products.id`` ...) exists, is PRIMARY KEY/UNIQUE and
   is physically referenced by the child tables, and reports row counts of the
   production tables. Raises instead of letting the bot start on a broken
   schema; it never writes.

Every step is additive and idempotent. No step drops a table, a column or a
row. If one step fails, the failure is reported and the remaining steps still
run. A REQUIRED step that fails aborts startup instead (see MigrationError):
the bot must never run against a partially migrated database.

Run standalone with: python migrations/run_all.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
from migrations import (  # noqa: E402
    add_faq_text,
    fk_preflight,
    add_referral,
    add_referral_first_reward_claim,
    add_low_stock_threshold,
    categorynullable,
    fix_payment_method_enum,
    schema_sync,
    verify_schema,
)

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """Raised when a REQUIRED migration step fails.

    ``database.db.init_db()`` lets this propagate and ``bot.main()`` aborts
    startup on it, so the bot can never run against a partially migrated
    database. Nothing is dropped, reset or rolled back — the operator fixes
    the database and restarts.
    """


# (name, callable, required). ``required=True`` steps are the ones the running
# code depends on; if one of them fails the process must not start.
STEPS = [
    ("fk_preflight", fk_preflight.migrate, True),
    ("categorynullable", categorynullable.migrate, True),
    ("add_low_stock_threshold", add_low_stock_threshold.migrate, True),
    ("add_faq_text", add_faq_text.migrate, True),
    ("add_referral", add_referral.migrate, True),
    ("add_referral_first_reward_claim",
     add_referral_first_reward_claim.migrate, True),
    ("schema_sync", schema_sync.migrate, True),
    ("fix_payment_method_enum", fix_payment_method_enum.migrate, True),
    # Adds the physical FOREIGN KEY constraints for any column created by the
    # steps above (schema_sync can add columns after the pre-flight ran).
    ("fk_constraints", fk_preflight.reconcile_constraints, True),
    # Final gate: read-only verification that the live schema really matches
    # the models (users.id / products.id exist and are legal FK targets, cart
    # and every other child table point at them, referral tables intact).
    ("verify_schema", verify_schema.migrate, True),
]


def run_all(engine=None, strict=True):
    """Apply all migrations.

    Args:
        engine: SQLAlchemy engine (defaults to the app engine).
        strict: when True (the default, used at startup) a failing REQUIRED
            step raises :class:`MigrationError` instead of being logged and
            skipped, so the bot never starts on a half-migrated database.

    Returns True when every step succeeded.
    """
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    ok = True
    failures = []
    for name, step, required in STEPS:
        try:
            # A few historical steps report failure by returning False instead
            # of raising; treat both the same way.
            if step(engine) is False:
                raise RuntimeError("migration step reported failure")
        except Exception as exc:
            ok = False
            failures.append(name)
            logger.exception("migration '%s' failed: %s", name, exc)
            print(f"[ERROR] migration '{name}' failed: {exc}")
            if required and strict:
                raise MigrationError(
                    f"required migration '{name}' failed: {exc}"
                ) from exc

    if ok:
        print("[OK] All migrations applied")
    else:
        print(f"[WARN] Some migrations failed: {', '.join(failures)}")
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    try:
        sys.exit(0 if run_all(strict=False) else 1)
    except MigrationError as exc:
        print(f"[FATAL] {exc}")
        sys.exit(1)
