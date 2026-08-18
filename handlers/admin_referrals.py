"""Admin panel: Referral Management.

Every state-changing action here writes an AdminActionLog row through the
existing log_admin_action() helper, so referral configuration changes and
manual referral credits are fully auditable next to every other admin action
in 📋 Audit Log.

Authorization mirrors the rest of the panel:
  * viewing / configuring  -> is_admin()
  * anything that moves money (manual referral credit) -> is_owner(), the same
    rule the existing "Adjust Balance" flow uses.
"""

import logging
from datetime import datetime
from decimal import Decimal

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session
from database.models import (
    PaymentMethod,
    ReferralRewardStatus,
    ReferralRewardType,
    ReferralSettings,
    Transaction,
    TransactionStatus,
    User,
)
from services import referral as referral_service
from utils import (
    format_price,
    format_datetime,
    is_admin,
    is_owner,
    log_admin_action,
    validate_amount,
)

logger = logging.getLogger(__name__)

# Conversation states for the referral reward configuration flow
REFERRAL_VALUE_INPUT, REFERRAL_SEARCH_INPUT, REFERRAL_CREDIT_INPUT = range(3)

HISTORY_PAGE_SIZE = 5


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def _menu_keyboard(config: dict):
    toggle = "🔴 Disable Program" if config["is_enabled"] else "🟢 Enable Program"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data="admin_ref_toggle")],
        [InlineKeyboardButton("🎁 Reward Type", callback_data="admin_ref_type"),
         InlineKeyboardButton("💵 Reward Value", callback_data="admin_ref_set_value")],
        [InlineKeyboardButton("🧾 Min Purchase", callback_data="admin_ref_set_min"),
         InlineKeyboardButton("🔒 Max Reward", callback_data="admin_ref_set_max")],
        [InlineKeyboardButton("🥇 First Purchase Only", callback_data="admin_ref_toggle_first")],
        [InlineKeyboardButton("📊 Referral Stats", callback_data="admin_ref_stats"),
         InlineKeyboardButton("📜 Reward History", callback_data="admin_ref_history")],
        [InlineKeyboardButton("🔍 Search Referral", callback_data="admin_ref_search")],
        [InlineKeyboardButton("💳 Manual Referral Credit", callback_data="admin_ref_credit")],
        [InlineKeyboardButton("📋 Referral Audit Log",
                              callback_data="admin_audit_log_filter_referral_settings")],
        [InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="admin_menu")],
    ])


def _back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Referrals", callback_data="admin_referrals")],
    ])


def _config_text(config: dict) -> str:
    status = "🟢 Enabled" if config["is_enabled"] else "🔴 Disabled"
    if config["reward_type"] == ReferralRewardType.PERCENTAGE:
        reward = f"{config['reward_value'].normalize():f}% of the order total"
    else:
        reward = format_price(config["reward_value"])
    max_reward = (format_price(config["max_reward_amount"])
                  if config["max_reward_amount"] else "no cap")
    return (
        "🤝 Referral Management\n\n"
        f"Status: {status}\n"
        f"🎁 Reward: {reward}\n"
        f"🧾 Minimum purchase: {format_price(config['min_purchase_amount'])}\n"
        f"🔒 Maximum reward: {max_reward}\n"
        f"🥇 First qualifying purchase only: "
        f"{'Yes' if config['first_purchase_only'] else 'No'}\n\n"
        "Rewards are credited to the referrer's wallet only after an order is "
        "successfully completed, once per qualifying order."
    )


def _load_config() -> dict:
    with get_db_session() as session:
        return referral_service.config_snapshot(session)


async def _render_menu(query, extra: str = ""):
    config = _load_config()
    text = _config_text(config)
    if extra:
        text = f"{extra}\n\n{text}"
    try:
        await query.edit_message_text(text, reply_markup=_menu_keyboard(config))
    except Exception:
        # Same content already rendered — Telegram rejects a no-op edit.
        pass


async def admin_referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🤝 Referral Management main screen."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await _render_menu(query)


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------

async def admin_ref_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable / disable the whole program."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        config = referral_service.get_config(session)
        config.is_enabled = not bool(config.is_enabled)
        new_value = config.is_enabled
        session.commit()

        log_admin_action(
            update.effective_user.id, "referral_settings", "settings", config.id,
            {"is_enabled": new_value}, session=session
        )

    await _render_menu(
        query, "✅ Referral program enabled." if new_value
        else "✅ Referral program disabled.")


async def admin_ref_toggle_first_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle 'first qualifying purchase only'."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        config = referral_service.get_config(session)
        config.first_purchase_only = not bool(config.first_purchase_only)
        new_value = config.first_purchase_only
        session.commit()
        log_admin_action(
            update.effective_user.id, "referral_settings", "settings", config.id,
            {"first_purchase_only": new_value}, session=session
        )

    await _render_menu(
        query, f"✅ First purchase only: {'Yes' if new_value else 'No'}")


async def admin_ref_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch between fixed amount and percentage rewards."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        config = referral_service.get_config(session)
        config.reward_type = (
            ReferralRewardType.PERCENTAGE
            if config.reward_type == ReferralRewardType.FIXED
            else ReferralRewardType.FIXED
        )
        new_value = config.reward_type.value
        session.commit()
        log_admin_action(
            update.effective_user.id, "referral_settings", "settings", config.id,
            {"reward_type": new_value}, session=session
        )

    await _render_menu(query, f"✅ Reward type set to {new_value}.")


# ---------------------------------------------------------------------------
# Numeric settings (conversation)
# ---------------------------------------------------------------------------

_FIELD_LABELS = {
    "reward_value": "💵 Reward Value",
    "min_purchase_amount": "🧾 Minimum Purchase Amount",
    "max_reward_amount": "🔒 Maximum Reward Amount",
}


def _cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_ref_cancel")],
    ])


async def admin_ref_set_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for editing one of the numeric reward settings."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    field = {
        "admin_ref_set_value": "reward_value",
        "admin_ref_set_min": "min_purchase_amount",
        "admin_ref_set_max": "max_reward_amount",
    }.get(query.data)
    if field is None:
        return ConversationHandler.END

    context.user_data['referral_field'] = field
    config = _load_config()

    if field == "reward_value":
        hint = ("Enter the reward value.\n"
                "• Fixed type: an amount, e.g. 2.50\n"
                "• Percentage type: a percent, e.g. 10 (for 10%)")
    elif field == "min_purchase_amount":
        hint = ("Enter the minimum order total that qualifies for a reward.\n"
                "Enter 0 for no minimum.")
    else:
        hint = ("Enter the maximum reward paid per qualifying order.\n"
                "Enter 0 to remove the cap.")

    await query.edit_message_text(
        f"{_FIELD_LABELS[field]}\n\n"
        f"Current reward type: {config['reward_type'].value}\n\n{hint}",
        reply_markup=_cancel_keyboard()
    )
    return REFERRAL_VALUE_INPUT


async def admin_ref_set_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate and persist the numeric setting."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    field = context.user_data.get('referral_field')
    if field is None:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    is_valid, value, error = validate_amount(raw)
    if not is_valid:
        # validate_amount() rejects 0; allow it explicitly for the two
        # "0 disables this limit" fields.
        if raw in ("0", "0.0", "0.00") and field != "reward_value":
            value = Decimal("0")
        else:
            await update.message.reply_text(
                f"❌ {error}\n\nPlease enter the value again:",
                reply_markup=_cancel_keyboard()
            )
            return REFERRAL_VALUE_INPUT

    value = referral_service.q2(value)

    with get_db_session() as session:
        config = referral_service.get_config(session)
        if field == "reward_value":
            if config.reward_type == ReferralRewardType.PERCENTAGE and value > Decimal("100"):
                await update.message.reply_text(
                    "❌ A percentage reward cannot exceed 100%.\n\n"
                    "Please enter the value again:",
                    reply_markup=_cancel_keyboard()
                )
                return REFERRAL_VALUE_INPUT
            config.reward_value = value
        elif field == "min_purchase_amount":
            config.min_purchase_amount = value
        else:
            config.max_reward_amount = None if value <= Decimal("0") else value
        config_id = config.id
        session.commit()

        log_admin_action(
            update.effective_user.id, "referral_settings", "settings", config_id,
            {field: str(value)}, session=session
        )

    context.user_data.pop('referral_field', None)
    config = _load_config()
    await update.message.reply_text(
        f"✅ {_FIELD_LABELS[field]} updated.\n\n{_config_text(config)}",
        reply_markup=_menu_keyboard(config)
    )
    return ConversationHandler.END


async def admin_ref_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any referral conversation and return to the referral menu."""
    context.user_data.pop('referral_field', None)
    context.user_data.pop('referral_credit_user', None)

    query = update.callback_query
    if query:
        await query.answer()
        await _render_menu(query, "❌ Cancelled.")
    else:
        config = _load_config()
        await update.message.reply_text(
            f"❌ Cancelled.\n\n{_config_text(config)}",
            reply_markup=_menu_keyboard(config)
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Stats / history / search
# ---------------------------------------------------------------------------

async def admin_ref_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 Program-wide referral totals."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    stats = referral_service.get_global_stats()
    await query.edit_message_text(
        "📊 Referral Stats\n\n"
        f"👥 Total referrals: {stats['total_referrals']}\n"
        f"✅ Qualified referrals: {stats['qualified_referrals']}\n"
        f"⏳ Pending referrals: {stats['pending_referrals']}\n"
        f"🎁 Rewards paid out: {stats['reward_count']}\n"
        f"💵 Total credited: {format_price(stats['total_credited'])}\n"
        f"↩️ Total reversed: {format_price(stats['total_revoked'])}",
        reply_markup=_back_keyboard()
    )


async def admin_ref_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📜 Paginated referral reward history.

    Callback data: admin_ref_history | admin_ref_history_page_<n>
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    page = 0
    if "_page_" in query.data:
        try:
            page = max(0, int(query.data.split("_page_")[1]))
        except ValueError:
            page = 0

    total = referral_service.count_rewards()
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(page, total_pages - 1)
    rewards = referral_service.get_recent_rewards(
        limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)

    if not rewards:
        await query.edit_message_text(
            "📜 Referral Reward History\n\nNo referral rewards yet.",
            reply_markup=_back_keyboard()
        )
        return

    lines = [f"📜 Referral Reward History (page {page + 1}/{total_pages})\n"]
    for reward in rewards:
        icon = "✅" if reward["status"] == ReferralRewardStatus.CREDITED else "↩️"
        when = format_datetime(reward["created_at"]) if reward["created_at"] else "-"
        lines.append(
            f"{icon} #{reward['id']} · {format_price(reward['amount'])}\n"
            f"   👤 {reward['referrer']} ← 🧑 {reward['referred']}\n"
            f"   🛍 Order #{reward['order_id']} · {when}"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Previous", callback_data=f"admin_ref_history_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"admin_ref_history_page_{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "🔙 Back to Referrals", callback_data="admin_referrals")])

    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_ref_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔍 Ask for a Telegram ID / @username / order id to look up."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        "🔍 Search Referral\n\n"
        "Send one of:\n"
        "• a Telegram ID (e.g. 123456789)\n"
        "• a username (e.g. @someone)\n"
        "• an order id (e.g. #42)",
        reply_markup=_cancel_keyboard()
    )
    return REFERRAL_SEARCH_INPUT


async def admin_ref_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the search result."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    result = referral_service.search(update.message.text)

    if result["kind"] == "order":
        icon = "✅" if result["status"] == ReferralRewardStatus.CREDITED else "↩️"
        text = (
            f"🔍 Order #{result['order_id']}\n\n"
            f"{icon} Reward: {format_price(result['amount'])}\n"
            f"👤 Referrer: {result['referrer']}\n"
            f"🧑 Referred: {result['referred']}\n"
            f"🕒 {format_datetime(result['created_at']) if result['created_at'] else '-'}"
        )
    elif result["kind"] == "user":
        username = f"@{result['username']}" if result["username"] else "N/A"
        invited = result["invited_by"] or "nobody"
        text = (
            f"🔍 User {result['telegram_id']} ({username})\n\n"
            f"💰 Wallet: {format_price(result['wallet_balance'])}\n"
            f"👥 Total referrals: {result['total_referrals']}\n"
            f"✅ Qualified: {result['qualified_referrals']}\n"
            f"💵 Referral earnings: {format_price(result['earned'])}\n"
            f"🔗 Invited by: {invited}"
        )
    else:
        text = "🔍 No referral data found for that search."

    await update.message.reply_text(text, reply_markup=_back_keyboard())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Manual referral credit (OWNER only)
# ---------------------------------------------------------------------------

async def admin_ref_credit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """💳 Ask for '<telegram_id> <amount> <reason>' to credit manually."""
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await query.answer("⛔ Only the owner can credit referral earnings.",
                           show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        "💳 Manual Referral Credit\n\n"
        "Credits a user's wallet and records it as a referral adjustment in "
        "the transaction history and the audit log.\n\n"
        "Send: <telegram_id> <amount> <reason>\n"
        "Example: 123456789 5 goodwill for lost referral",
        reply_markup=_cancel_keyboard()
    )
    return REFERRAL_CREDIT_INPUT


async def admin_ref_credit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply the manual credit atomically and log it."""
    if not is_owner(update.effective_user.id):
        return ConversationHandler.END

    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "❌ Invalid format. Send: <telegram_id> <amount> <reason>",
            reply_markup=_cancel_keyboard()
        )
        return REFERRAL_CREDIT_INPUT

    telegram_id = int(parts[0])
    is_valid, amount, error = validate_amount(parts[1])
    if not is_valid:
        await update.message.reply_text(
            f"❌ {error}\n\nSend: <telegram_id> <amount> <reason>",
            reply_markup=_cancel_keyboard()
        )
        return REFERRAL_CREDIT_INPUT

    reason = (parts[2].strip() if len(parts) > 2 else "manual referral credit")[:180]
    amount = referral_service.q2(amount)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            await update.message.reply_text(
                "❌ User not found.", reply_markup=_back_keyboard())
            return ConversationHandler.END

        session.query(User).filter(User.id == user.id).update(
            {User.wallet_balance: User.wallet_balance + amount},
            synchronize_session=False
        )
        session.add(Transaction(
            user_id=user.id,
            amount=amount,
            payment_method=PaymentMethod.REFERRAL_REWARD,
            status=TransactionStatus.ADMIN_ADJUSTMENT,
            admin_note=f"Manual referral credit: {reason}"[:255],
            completed_at=datetime.utcnow(),
        ))
        session.commit()
        session.refresh(user)
        new_balance = user.wallet_balance

        log_admin_action(
            update.effective_user.id, "referral_manual_credit", "user",
            telegram_id,
            {"amount": str(amount), "reason": reason,
             "new_balance": str(new_balance)},
            session=session
        )

    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(f"💰 Referral credit\n\n"
                  f"Amount: {format_price(amount)}\n"
                  f"Reason: {reason}\n"
                  f"💳 New balance: {format_price(new_balance)}")
        )
    except Exception as exc:
        logger.warning("Could not notify user %s about referral credit: %s",
                       telegram_id, exc)

    await update.message.reply_text(
        f"✅ Credited {format_price(amount)} to {telegram_id}.\n"
        f"💳 New balance: {format_price(new_balance)}",
        reply_markup=_back_keyboard()
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Notification helper used by the purchase flow
# ---------------------------------------------------------------------------

async def notify_referrer(context, reward: dict):
    """DM the referrer about a credited reward. Never raises.

    Called exactly once per credited reward — process_order_qualification()
    returns a dict only for the call that actually created the reward row, so
    duplicate callbacks/webhooks cannot produce duplicate notifications.
    """
    if not reward or not reward.get("referrer_telegram_id"):
        return
    try:
        await context.bot.send_message(
            chat_id=reward["referrer_telegram_id"],
            text=(
                "🎉 Referral Reward Earned!\n\n"
                f"💵 Reward: {format_price(reward['amount'])}\n"
                f"🛍 From order #{reward['order_id']}\n"
                f"💳 New wallet balance: {format_price(reward['referrer_balance'])}\n\n"
                "Thanks for spreading the word!"
            )
        )
    except Exception as exc:  # pragma: no cover - Telegram may block the DM
        logger.warning(f"Could not notify referrer: {exc}")
