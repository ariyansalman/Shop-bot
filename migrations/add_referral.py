"""
Migration: create the referral program tables and seed their settings row.

What it does
------------
1. Creates ``referrals``, ``referral_rewards`` and ``referral_settings`` if
   they do not exist yet (including the UNIQUE indexes that make the referral
   rules enforceable at the database level).
2. Inserts the single ``referral_settings`` row with safe defaults
   (program DISABLED until an admin turns it on).
3. Adds the ``REFERRAL_REWARD`` label to the Postgres ``paymentmethod`` enum
   type — handled additively by migrations/fix_payment_method_enum.py, which
   runs right after this step in migrations/run_all.py.

Safety
------
Purely additive and idempotent: it only ever CREATEs missing tables and
INSERTs the settings row when the table is empty. It never drops, alters or
deletes anything, so it is safe to run on every startup against a live
production database. Works on PostgreSQL and on SQLite.

Run standalone with: python migrations/add_referral.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import (  # noqa: E402
    Base,
    Referral,
    ReferralReward,
    ReferralSettings,
)

REFERRAL_TABLES = [
    Referral.__table__,
    ReferralReward.__table__,
    ReferralSettings.__table__,
]


def migrate(engine=None, verbose=True):
    """Create the referral tables and seed defaults. Idempotent."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    created = []
    for table in REFERRAL_TABLES:
        if table.name in existing:
            continue
        table.create(bind=engine, checkfirst=True)
        created.append(table.name)

    # Seed the single settings row (disabled by default — an admin has to
    # enable the program explicitly before any reward can ever be paid).
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        if session.query(ReferralSettings).first() is None:
            session.add(ReferralSettings())
            session.commit()
            seeded = True
        else:
            seeded = False

    if verbose:
        if created:
            print(f"[OK] referral tables created: {', '.join(created)}")
        else:
            print("- referral tables already present")
        print("[OK] referral settings row seeded" if seeded
              else "- referral settings row already present")
    return True


if __name__ == "__main__":
    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    sys.exit(0 if migrate() else 1)
