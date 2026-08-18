"""Referral program core service.

All business rules for the referral program live here so the handlers stay
thin and every entry point (user /start, purchase completion, admin panel,
order cancellation) shares exactly the same logic.

Money handling
--------------
Every monetary value is a ``decimal.Decimal`` (the Money() columns are
Numeric(12, 2, asdecimal=True)), quantized to 2 decimal places with
ROUND_HALF_UP — the same convention the rest of the bot uses. No float
arithmetic is performed anywhere in this module.

Exactly-once guarantees
-----------------------
1. ``referrals.referred_user_id`` is UNIQUE  -> a user can only ever have one
   referrer, and a second referral link can never overwrite the first.
2. ``referral_rewards.order_id`` is UNIQUE    -> one qualifying event can only
   ever produce one reward row, even under concurrent/duplicate callbacks or
   webhook retries; the second insert fails on the constraint and is skipped.
3. The wallet credit, the Transaction row, the reward row and the referral
   status flip all happen inside ONE database transaction, so a partial
   credit is impossible.
4. Reversals use a conditional UPDATE (CREDITED -> REVOKED matching zero rows
   the second time), the same pattern the order-cancel refund already uses.
"""

import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.exc import IntegrityError

from database import get_db_session
from database.models import (
    Order,
    OrderStatus,
    PaymentMethod,
    Referral,
    ReferralReward,
    ReferralRewardStatus,
    ReferralRewardType,
    ReferralSettings,
    ReferralStatus,
    Transaction,
    TransactionStatus,
    User,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Result codes returned by attach_referral(); handlers turn these into text.
ATTACH_OK = "attached"
ATTACH_DISABLED = "disabled"
ATTACH_SELF = "self"
ATTACH_EXISTING_USER = "existing_user"
ATTACH_ALREADY_REFERRED = "already_referred"
ATTACH_UNKNOWN_REFERRER = "unknown_referrer"
ATTACH_INVALID = "invalid"


def q2(value) -> Decimal:
    """Quantize any numeric input to 2 decimal places as a Decimal."""
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Configuration (single-row settings table, editable from the admin panel)
# ---------------------------------------------------------------------------

def get_config(session) -> ReferralSettings:
    """Return the referral settings row, creating it with defaults if absent."""
    config = session.query(ReferralSettings).first()
    if config is None:
        config = ReferralSettings()
        session.add(config)
        session.commit()
        session.refresh(config)
    return config


def config_snapshot(session) -> dict:
    """Plain-dict copy of the config, safe to use after the session closes."""
    config = get_config(session)
    return {
        "is_enabled": bool(config.is_enabled),
        "reward_type": config.reward_type,
        "reward_value": q2(config.reward_value),
        "min_purchase_amount": q2(config.min_purchase_amount),
        "first_purchase_only": bool(config.first_purchase_only),
        "max_reward_amount": (
            q2(config.max_reward_amount)
            if config.max_reward_amount is not None else None
        ),
    }


def is_enabled() -> bool:
    with get_db_session() as session:
        return bool(get_config(session).is_enabled)


# ---------------------------------------------------------------------------
# Referral links
# ---------------------------------------------------------------------------

REFERRAL_PAYLOAD_PREFIX = "ref_"


def build_referral_link(bot_username: str, telegram_id: int) -> str:
    """Build the Telegram deep link for a user.

    ``bot_username`` always comes from the running bot (``context.bot.username``)
    — it is never hardcoded anywhere in the project.
    """
    username = (bot_username or "").lstrip("@")
    return f"https://t.me/{username}?start={REFERRAL_PAYLOAD_PREFIX}{telegram_id}"


def parse_referral_payload(args) -> int:
    """Extract the referrer Telegram ID from /start arguments, or return None."""
    if not args:
        return None
    payload = str(args[0]).strip()
    if not payload.startswith(REFERRAL_PAYLOAD_PREFIX):
        return None
    raw = payload[len(REFERRAL_PAYLOAD_PREFIX):]
    if not raw.isdigit():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Attaching a referrer
# ---------------------------------------------------------------------------

def attach_referral(referred_telegram_id: int, referrer_telegram_id: int,
                    is_new_user: bool) -> str:
    """Record ``referrer -> referred`` if every rule allows it.

    Rules enforced (see module docstring for the DB-level guarantees):
      * program must be enabled
      * no self-referral
      * only a brand-new user (created by this very /start) can be attached,
        so an existing customer never becomes someone's referral later
      * a user that already has a referrer keeps it forever
    Returns one of the ATTACH_* result codes. Never raises.
    """
    if not referrer_telegram_id:
        return ATTACH_INVALID
    if referrer_telegram_id == referred_telegram_id:
        return ATTACH_SELF
    if not is_new_user:
        return ATTACH_EXISTING_USER

    try:
        with get_db_session() as session:
            if not get_config(session).is_enabled:
                return ATTACH_DISABLED

            referred = session.query(User).filter_by(
                telegram_id=referred_telegram_id).first()
            referrer = session.query(User).filter_by(
                telegram_id=referrer_telegram_id).first()

            if referred is None:
                return ATTACH_INVALID
            if referrer is None:
                return ATTACH_UNKNOWN_REFERRER
            if referrer.id == referred.id:
                return ATTACH_SELF

            existing = session.query(Referral).filter_by(
                referred_user_id=referred.id).first()
            if existing is not None:
                return ATTACH_ALREADY_REFERRED

            session.add(Referral(
                referrer_user_id=referrer.id,
                referred_user_id=referred.id,
                status=ReferralStatus.PENDING,
            ))
            try:
                session.commit()
            except IntegrityError:
                # Concurrent /start with the same payload: the UNIQUE index on
                # referred_user_id already stored the other one. Keep it.
                session.rollback()
                return ATTACH_ALREADY_REFERRED
            return ATTACH_OK
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"attach_referral failed: {exc}")
        return ATTACH_INVALID


# ---------------------------------------------------------------------------
# Reward calculation
# ---------------------------------------------------------------------------

def calculate_reward(config: dict, order_total: Decimal) -> Decimal:
    """Reward for an order total, or ZERO when it does not qualify."""
    total = q2(order_total)
    if total <= ZERO:
        return ZERO
    if total < q2(config["min_purchase_amount"]):
        return ZERO

    if config["reward_type"] == ReferralRewardType.PERCENTAGE:
        reward = q2(total * q2(config["reward_value"]) / Decimal("100"))
    else:
        reward = q2(config["reward_value"])

    max_reward = config["max_reward_amount"]
    if max_reward is not None and max_reward > ZERO and reward > max_reward:
        reward = q2(max_reward)

    return reward if reward > ZERO else ZERO


# ---------------------------------------------------------------------------
# Qualification (called after the shop confirms a successful order)
# ---------------------------------------------------------------------------

def process_order_qualification(order_id: int) -> dict:
    """Credit the referrer for a successfully completed order.

    Returns a dict describing the credited reward (for the notification), or
    None when nothing was credited (disabled, no referrer, below minimum,
    already rewarded, order not completed, ...). Safe to call repeatedly and
    concurrently for the same order — at most one reward is ever created.
    """
    try:
        with get_db_session() as session:
            config = config_snapshot(session)
            if not config["is_enabled"]:
                return None

            order = session.query(Order).filter_by(id=order_id).first()
            if order is None or order.status != OrderStatus.COMPLETED:
                return None

            # Row lock on the referral: two concurrent completions of two
            # orders from the SAME referred user serialize here, so the
            # first-purchase claim below can never be evaluated by both at
            # once. Postgres takes a real FOR UPDATE lock; SQLite ignores it
            # and the conditional UPDATE still protects the invariant.
            referral = (
                session.query(Referral)
                .filter(Referral.referred_user_id == order.user_id)
                .with_for_update()
                .first()
            )
            if referral is None or referral.status == ReferralStatus.REVOKED:
                return None

            # Already rewarded for this exact order?
            if session.query(ReferralReward).filter_by(order_id=order.id).first():
                return None

            amount = calculate_reward(config, order.total_amount)
            if amount <= ZERO:
                return None

            if config["first_purchase_only"]:
                # "First purchase only" means the FIRST QUALIFYING purchase:
                # once this referral has produced a live (credited) reward, no
                # further order pays out. Orders that never qualified — below
                # the minimum, or made while the program was off — must not
                # burn the one payout the referrer is entitled to.
                #
                # The rule is enforced by the database, not by a Python count:
                # this conditional UPDATE can only match for ONE order, even if
                # two workers run it at the same instant, and the UNIQUE index
                # on first_reward_order_id is the second safety layer. Re-runs
                # for the SAME order still pass (the claim already points at
                # it), which keeps retries idempotent rather than fatal.
                claimed = session.query(Referral).filter(
                    Referral.id == referral.id,
                    Referral.first_reward_order_id.is_(None),
                ).update(
                    {Referral.first_reward_order_id: order.id},
                    synchronize_session=False,
                )
                if not claimed:
                    session.refresh(referral)
                    if referral.first_reward_order_id != order.id:
                        logger.info(
                            "referral_reward_skipped order_id=%s referral_id=%s "
                            "reason=first_purchase_already_claimed claim_order_id=%s",
                            order.id, referral.id, referral.first_reward_order_id,
                        )
                        session.rollback()
                        return None

            referrer = session.query(User).filter_by(
                id=referral.referrer_user_id).first()
            if referrer is None or referrer.is_banned:
                return None

            reward = ReferralReward(
                referral_id=referral.id,
                referrer_user_id=referral.referrer_user_id,
                referred_user_id=referral.referred_user_id,
                order_id=order.id,
                amount=amount,
                status=ReferralRewardStatus.CREDITED,
            )
            session.add(reward)
            try:
                # Flush first: if a concurrent worker already inserted a reward
                # for this order, the UNIQUE constraint rejects it here, before
                # any wallet money moves.
                session.flush()
            except IntegrityError:
                session.rollback()
                return None

            session.query(User).filter(User.id == referrer.id).update(
                {User.wallet_balance: User.wallet_balance + amount},
                synchronize_session=False,
            )

            transaction = Transaction(
                user_id=referrer.id,
                amount=amount,
                payment_method=PaymentMethod.REFERRAL_REWARD,
                status=TransactionStatus.COMPLETED,
                admin_note=f"Referral reward for order #{order.id}",
                completed_at=datetime.utcnow(),
            )
            session.add(transaction)
            session.flush()
            reward.transaction_id = transaction.id

            referral.status = ReferralStatus.QUALIFIED
            referral.qualified_at = referral.qualified_at or datetime.utcnow()
            referral.qualifying_order_id = referral.qualifying_order_id or order.id
            referral.total_rewarded = q2(referral.total_rewarded) + amount

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None

            session.refresh(referrer)
            return {
                "reward_id": reward.id,
                "amount": amount,
                "order_id": order.id,
                "referrer_telegram_id": referrer.telegram_id,
                "referrer_balance": q2(referrer.wallet_balance),
                "referred_user_id": referral.referred_user_id,
            }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"process_order_qualification({order_id}) failed: {exc}")
        return None


def revoke_order_reward(order_id: int, reason: str = "order cancelled") -> dict:
    """Reverse a referral reward when its order is cancelled / refunded.

    Returns a dict describing the reversal, or None when there was nothing to
    reverse. Exactly-once: the CREDITED -> REVOKED conditional UPDATE matches
    zero rows on a second call.
    """
    try:
        with get_db_session() as session:
            reward = session.query(ReferralReward).filter_by(
                order_id=order_id).first()
            if reward is None or reward.status != ReferralRewardStatus.CREDITED:
                return None

            amount = q2(reward.amount)

            reversed_rows = session.query(ReferralReward).filter(
                ReferralReward.id == reward.id,
                ReferralReward.status == ReferralRewardStatus.CREDITED,
            ).update(
                {
                    ReferralReward.status: ReferralRewardStatus.REVOKED,
                    ReferralReward.revoked_at: datetime.utcnow(),
                    ReferralReward.revoke_reason: (reason or "")[:200],
                },
                synchronize_session=False,
            )
            if reversed_rows == 0:
                session.rollback()
                return None

            session.query(User).filter(User.id == reward.referrer_user_id).update(
                {User.wallet_balance: User.wallet_balance - amount},
                synchronize_session=False,
            )

            session.add(Transaction(
                user_id=reward.referrer_user_id,
                amount=-amount,
                payment_method=PaymentMethod.REFERRAL_REWARD,
                status=TransactionStatus.COMPLETED,
                admin_note=f"Referral reward reversed for order #{order_id}: {reason}"[:255],
                completed_at=datetime.utcnow(),
            ))

            referral = session.query(Referral).filter_by(
                id=reward.referral_id).first()
            if referral is not None:
                referral.total_rewarded = q2(referral.total_rewarded) - amount
                remaining = session.query(ReferralReward).filter(
                    ReferralReward.referral_id == referral.id,
                    ReferralReward.status == ReferralRewardStatus.CREDITED,
                ).count()
                if remaining == 0:
                    referral.status = ReferralStatus.PENDING
                    referral.qualified_at = None
                    referral.qualifying_order_id = None
                    # Release the first-purchase claim so a later genuine
                    # purchase can qualify again (mirrors the status reset).
                    referral.first_reward_order_id = None

            session.commit()

            referrer = session.query(User).filter_by(
                id=reward.referrer_user_id).first()
            return {
                "amount": amount,
                "order_id": order_id,
                "referrer_telegram_id": referrer.telegram_id if referrer else None,
            }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"revoke_order_reward({order_id}) failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Read models for the UI
# ---------------------------------------------------------------------------

def get_user_stats(telegram_id: int) -> dict:
    """Referral totals for one user, as plain values (session already closed)."""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return {
                "total_referrals": 0, "qualified_referrals": 0,
                "total_earned": ZERO, "available_earned": ZERO,
                "pending_referrals": 0,
            }

        referrals = session.query(Referral).filter_by(
            referrer_user_id=user.id).all()
        qualified = sum(
            1 for r in referrals if r.status == ReferralStatus.QUALIFIED)

        rewards = session.query(ReferralReward).filter_by(
            referrer_user_id=user.id).all()
        credited = sum(
            (q2(r.amount) for r in rewards
             if r.status == ReferralRewardStatus.CREDITED),
            ZERO,
        )
        lifetime = sum((q2(r.amount) for r in rewards), ZERO)

        return {
            "total_referrals": len(referrals),
            "qualified_referrals": qualified,
            "pending_referrals": len(referrals) - qualified,
            # Lifetime credited, before reversals.
            "total_earned": q2(lifetime),
            # Currently valid earnings — already sitting in the wallet balance.
            "available_earned": q2(credited),
        }


def get_user_history(telegram_id: int, limit: int = 20) -> list:
    """Most recent referral events for one referrer (newest first)."""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return []

        rows = (
            session.query(Referral)
            .filter_by(referrer_user_id=user.id)
            .order_by(Referral.created_at.desc(), Referral.id.desc())
            .limit(limit)
            .all()
        )

        history = []
        for referral in rows:
            referred = session.query(User).filter_by(
                id=referral.referred_user_id).first()
            credited = sum(
                (q2(r.amount) for r in session.query(ReferralReward).filter_by(
                    referral_id=referral.id,
                    status=ReferralRewardStatus.CREDITED).all()),
                ZERO,
            )
            history.append({
                "referred_username": referred.username if referred else None,
                "referred_telegram_id": referred.telegram_id if referred else None,
                "status": referral.status,
                "earned": q2(credited),
                "created_at": referral.created_at,
                "qualified_at": referral.qualified_at,
            })
        return history


def get_global_stats() -> dict:
    """Program-wide totals for the admin dashboard."""
    with get_db_session() as session:
        total = session.query(Referral).count()
        qualified = session.query(Referral).filter_by(
            status=ReferralStatus.QUALIFIED).count()
        rewards = session.query(ReferralReward).all()
        credited = sum(
            (q2(r.amount) for r in rewards
             if r.status == ReferralRewardStatus.CREDITED),
            ZERO,
        )
        revoked = sum(
            (q2(r.amount) for r in rewards
             if r.status == ReferralRewardStatus.REVOKED),
            ZERO,
        )
        return {
            "total_referrals": total,
            "qualified_referrals": qualified,
            "pending_referrals": total - qualified,
            "reward_count": len(rewards),
            "total_credited": q2(credited),
            "total_revoked": q2(revoked),
        }


def get_recent_rewards(limit: int = 10, offset: int = 0) -> list:
    """Recent reward rows for the admin referral history screen."""
    with get_db_session() as session:
        rows = (
            session.query(ReferralReward)
            .order_by(ReferralReward.created_at.desc(), ReferralReward.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        out = []
        for reward in rows:
            referrer = session.query(User).filter_by(
                id=reward.referrer_user_id).first()
            referred = session.query(User).filter_by(
                id=reward.referred_user_id).first()
            out.append({
                "id": reward.id,
                "amount": q2(reward.amount),
                "status": reward.status,
                "order_id": reward.order_id,
                "created_at": reward.created_at,
                "referrer": referrer.username if referrer and referrer.username
                else (str(referrer.telegram_id) if referrer else "?"),
                "referred": referred.username if referred and referred.username
                else (str(referred.telegram_id) if referred else "?"),
            })
        return out


def count_rewards() -> int:
    with get_db_session() as session:
        return session.query(ReferralReward).count()


def search(term: str) -> dict:
    """Look up referral data by Telegram ID, @username or order id (#12/12).

    Returns {"kind": "user"|"order"|"none", ...}. Never raises on bad input.
    """
    term = (term or "").strip()
    if not term:
        return {"kind": "none"}

    with get_db_session() as session:
        # Order lookup: "#12" or "order 12"
        cleaned = term.lstrip("#").strip()
        if cleaned.isdigit():
            reward = session.query(ReferralReward).filter_by(
                order_id=int(cleaned)).first()
            if reward is not None:
                referrer = session.query(User).filter_by(
                    id=reward.referrer_user_id).first()
                referred = session.query(User).filter_by(
                    id=reward.referred_user_id).first()
                return {
                    "kind": "order",
                    "order_id": reward.order_id,
                    "amount": q2(reward.amount),
                    "status": reward.status,
                    "created_at": reward.created_at,
                    "referrer": referrer.telegram_id if referrer else None,
                    "referred": referred.telegram_id if referred else None,
                }

        user = None
        if cleaned.isdigit():
            user = session.query(User).filter_by(
                telegram_id=int(cleaned)).first()
        if user is None:
            user = session.query(User).filter_by(
                username=term.lstrip("@")).first()
        if user is None:
            return {"kind": "none"}

        as_referrer = session.query(Referral).filter_by(
            referrer_user_id=user.id).all()
        as_referred = session.query(Referral).filter_by(
            referred_user_id=user.id).first()
        invited_by = None
        if as_referred is not None:
            inviter = session.query(User).filter_by(
                id=as_referred.referrer_user_id).first()
            invited_by = inviter.telegram_id if inviter else None

        credited = sum(
            (q2(r.amount) for r in session.query(ReferralReward).filter_by(
                referrer_user_id=user.id,
                status=ReferralRewardStatus.CREDITED).all()),
            ZERO,
        )
        return {
            "kind": "user",
            "telegram_id": user.telegram_id,
            "username": user.username,
            "wallet_balance": q2(user.wallet_balance),
            "total_referrals": len(as_referrer),
            "qualified_referrals": sum(
                1 for r in as_referrer if r.status == ReferralStatus.QUALIFIED),
            "earned": q2(credited),
            "invited_by": invited_by,
        }
