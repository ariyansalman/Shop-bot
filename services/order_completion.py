"""Centralized order-completion service.

Every path that can move an order into COMPLETED — the buyer's own wallet
purchase and the admin panel's "Mark as Completed" button — goes through
:func:`complete_order` so the business logic exists exactly once:

    validate current state
    -> mark COMPLETED (row-locked, conditional UPDATE)
    -> set completed_at (only when it is still empty)
    -> process referral qualification (exactly-once, see services.referral)
    -> return the data the caller needs for notifications

Idempotency
-----------
The status flip is a conditional UPDATE (``WHERE status != COMPLETED``) run
under a ``SELECT ... FOR UPDATE`` row lock, so pressing the admin button twice
(or a duplicate callback/webhook) can only ever complete the order once:

  * ``completed_at`` is never overwritten for an already-completed order;
  * the referral reward is created at most once (UNIQUE ``order_id`` plus the
    atomic first-purchase claim in ``services.referral``);
  * the caller can tell a real completion from a repeat via
    ``result["newly_completed"]`` and only notify on the first one.

Delivery/inventory is intentionally NOT re-run here: assets are assigned at
checkout inside the same transaction that creates the order, and an admin
completing an order must never re-deliver keys that were already handed out.
"""

import logging
from datetime import datetime

from database import get_db_session
from database.models import Order, OrderStatus
from services import referral as referral_service

logger = logging.getLogger(__name__)

# Result reasons
REASON_NOT_FOUND = "not_found"
REASON_CANCELLED = "cancelled"
REASON_ALREADY_COMPLETED = "already_completed"
REASON_COMPLETED = "completed"


def _result(ok, reason, order_id, newly_completed=False, reward=None,
            completed_at=None):
    return {
        "ok": ok,
        "reason": reason,
        "order_id": order_id,
        "newly_completed": newly_completed,
        "reward": reward,
        "completed_at": completed_at,
    }


def complete_order(order_id: int, allow_from_cancelled: bool = False) -> dict:
    """Move an order to COMPLETED and run every downstream side effect once.

    Args:
        order_id: the order to complete.
        allow_from_cancelled: when False (default) a CANCELLED order is left
            untouched — reactivation has its own refund-reversal flow.

    Returns a result dict; never raises.
    """
    completed_at = None
    newly_completed = False

    try:
        with get_db_session() as session:
            # Row lock: serializes two concurrent completions of the same
            # order. Postgres takes a real FOR UPDATE lock; SQLite (local dev)
            # ignores it, and the conditional UPDATE below still protects us.
            order = (
                session.query(Order)
                .filter(Order.id == order_id)
                .with_for_update()
                .first()
            )

            if order is None:
                logger.warning("complete_order: order_id=%s not found", order_id)
                return _result(False, REASON_NOT_FOUND, order_id)

            if order.status == OrderStatus.CANCELLED and not allow_from_cancelled:
                logger.info("complete_order: order_id=%s is cancelled, skipped",
                            order_id)
                return _result(False, REASON_CANCELLED, order_id)

            if order.status == OrderStatus.COMPLETED:
                # Already completed: never touch completed_at if it is set.
                # Backfill it only when an older row has it empty, so the
                # timestamp is never corrupted and never lost.
                if order.completed_at is None:
                    order.completed_at = datetime.utcnow()
                    session.commit()
                completed_at = order.completed_at
            else:
                now = datetime.utcnow()
                updated = session.query(Order).filter(
                    Order.id == order.id,
                    Order.status != OrderStatus.COMPLETED,
                ).update(
                    {Order.status: OrderStatus.COMPLETED,
                     Order.completed_at: now},
                    synchronize_session=False,
                )
                session.commit()
                if updated:
                    newly_completed = True
                    completed_at = now
                    logger.info("order_completed order_id=%s completed_at=%s",
                                order_id, now.isoformat())
                else:
                    session.refresh(order)
                    completed_at = order.completed_at
    except Exception as exc:
        logger.exception("complete_order(%s) failed: %s", order_id, exc)
        return _result(False, REASON_NOT_FOUND, order_id)

    # Referral qualification runs in its own transaction and is exactly-once
    # for the order, so calling it on a repeat press is harmless: it returns
    # None when a reward already exists.
    reward = referral_service.process_order_qualification(order_id)
    if reward:
        logger.info("referral_reward_credited order_id=%s reward_id=%s amount=%s",
                    order_id, reward.get("reward_id"), reward.get("amount"))

    return _result(
        True,
        REASON_COMPLETED if newly_completed else REASON_ALREADY_COMPLETED,
        order_id,
        newly_completed=newly_completed,
        reward=reward,
        completed_at=completed_at,
    )
