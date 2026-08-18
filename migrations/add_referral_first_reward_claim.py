"""
Migration: add ``referrals.first_reward_order_id`` (the first-purchase claim).

Why
---
"First purchase only" used to be enforced with a Python-level COUNT of
credited rewards. Two orders from the same referred user completing at the
same instant could both read "no reward yet" and both pay out. This column
turns that rule into an atomic, database-enforced claim:

    UPDATE referrals
       SET first_reward_order_id = :order_id
     WHERE id = :referral_id
       AND first_reward_order_id IS NULL

Exactly one concurrent writer can match a row, so exactly one order can ever
become the qualifying first purchase. The UNIQUE index adds a second layer:
one order can never claim two referrals.

Safety
------
Purely additive and idempotent:
  * only ever ADD COLUMN / CREATE UNIQUE INDEX, and only when missing;
  * backfills the claim from existing credited rewards (oldest first), so
    installations that already paid rewards keep their history and cannot pay
    a second "first purchase" reward afterwards;
  * nothing is dropped, reset or deleted.

Works on PostgreSQL and SQLite. Run with:
    python migrations/add_referral_first_reward_claim.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

INDEX_NAME = "ix_referrals_first_reward_order_id"


def migrate(engine=None):
    """Add referrals.first_reward_order_id + its unique index, then backfill."""
    if engine is None:
        from database.db import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "referrals" not in tables:
        print("- referrals table does not exist yet, skipping")
        return True

    columns = {c["name"] for c in inspector.get_columns("referrals")}
    if "first_reward_order_id" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE referrals ADD COLUMN first_reward_order_id INTEGER"
            ))
        print("✅ Added referrals.first_reward_order_id")
    else:
        print("- referrals.first_reward_order_id already exists, skipping")

    existing_indexes = {i["name"] for i in inspector.get_indexes("referrals")}
    if INDEX_NAME not in existing_indexes:
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
                "ON referrals (first_reward_order_id)"
            ))
        print(f"✅ Created unique index {INDEX_NAME}")
    else:
        print(f"- index {INDEX_NAME} already exists, skipping")

    # Backfill: for every referral that already produced a credited reward,
    # claim the oldest such order. Only fills rows where the claim is still
    # NULL, so re-running changes nothing.
    if "referral_rewards" in tables:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE referrals SET first_reward_order_id = ("
                "  SELECT MIN(rr.order_id) FROM referral_rewards rr"
                "   WHERE rr.referral_id = referrals.id"
                "     AND rr.status = 'CREDITED'"
                ") "
                "WHERE first_reward_order_id IS NULL "
                "  AND EXISTS ("
                "  SELECT 1 FROM referral_rewards rr2"
                "   WHERE rr2.referral_id = referrals.id"
                "     AND rr2.status = 'CREDITED')"
            ))
        print("✅ Backfilled first_reward_order_id from existing credited rewards")

    return True


if __name__ == "__main__":
    from config.settings import settings  # noqa: E402

    print(f"Database: {settings.DATABASE_URL.split('@')[-1]}")
    sys.exit(0 if migrate() else 1)
