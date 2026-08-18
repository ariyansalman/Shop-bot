"""User-facing referral program handlers.

UI language matches the rest of the bot: emoji-prefixed section titles,
inline keyboards, a 🔙 Back button on every screen. No pagination is used —
the history screen shows the most recent 20 entries, which is plenty for a
store of this size and keeps the screen compact.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database.models import ReferralRewardType, ReferralStatus
from services import referral as referral_service
from utils import check_user_banned, format_price, format_datetime, get_user_language, t

HISTORY_LIMIT = 20


def _menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Referral History", callback_data="referral_history")],
        [InlineKeyboardButton("ℹ️ How It Works", callback_data="referral_info")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ])


def _back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="referral")],
    ])


def _reward_description(config: dict) -> str:
    if config["reward_type"] == ReferralRewardType.PERCENTAGE:
        value = f"{config['reward_value'].normalize():f}% of the purchase"
    else:
        value = format_price(config["reward_value"])

    lines = [f"🎁 Reward: {value}"]
    if config["min_purchase_amount"] > referral_service.ZERO:
        lines.append(
            f"🧾 Minimum purchase: {format_price(config['min_purchase_amount'])}")
    if config["max_reward_amount"]:
        lines.append(
            f"🔒 Maximum reward: {format_price(config['max_reward_amount'])}")
    lines.append(
        "🥇 Applies to: first qualifying purchase only"
        if config["first_purchase_only"]
        else "🔁 Applies to: every qualifying purchase")
    return "\n".join(lines)


def _load_config() -> dict:
    from database import get_db_session

    with get_db_session() as session:
        return referral_service.config_snapshot(session)


async def referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """💰 Referral Program — overview, stats and the user's referral link."""
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    if check_user_banned(telegram_id):
        lang = get_user_language(telegram_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    config = _load_config()
    if not config["is_enabled"]:
        await query.edit_message_text(
            "💰 Referral Program\n\n"
            "😴 The referral program is currently disabled.\n"
            "Please check back later.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]])
        )
        return

    stats = referral_service.get_user_stats(telegram_id)
    link = referral_service.build_referral_link(
        context.bot.username, telegram_id)

    message = (
        "💰 Referral Program\n\n"
        f"👥 Total Referrals: {stats['total_referrals']}\n"
        f"✅ Qualified Referrals: {stats['qualified_referrals']}\n"
        f"💵 Total Earned: {format_price(stats['total_earned'])}\n"
        f"💳 Available Referral Earnings: {format_price(stats['available_earned'])}\n"
        "ℹ️ Referral earnings are credited straight to your wallet balance, "
        "so you can spend them on any product right away.\n\n"
        "🔗 My Referral Link\n"
        f"{link}\n\n"
        f"{_reward_description(config)}"
    )

    await query.edit_message_text(
        message,
        reply_markup=_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def referral_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ℹ️ How It Works — the exact rules, no marketing fluff."""
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    if check_user_banned(telegram_id):
        lang = get_user_language(telegram_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    config = _load_config()
    qualifying = (
        "their first completed purchase"
        if config["first_purchase_only"]
        else "every completed purchase they make")

    message = (
        "ℹ️ How Referrals Work\n\n"
        "1️⃣ Share your referral link with a friend.\n"
        "2️⃣ They must be a brand-new user and start the bot with your link — "
        "someone who already used this bot before cannot become your referral.\n"
        f"3️⃣ You are rewarded after {qualifying} is successfully completed. "
        "Simply opening the link never pays anything.\n"
        "4️⃣ The reward is credited automatically to your wallet balance and "
        "you get a notification.\n\n"
        f"{_reward_description(config)}\n\n"
        "⚠️ Conditions\n"
        "• You cannot refer yourself.\n"
        "• Each user can have only one referrer, set permanently on their "
        "first start.\n"
        "• Cancelled or refunded orders do not pay a reward — if an order is "
        "refunded after payout, the reward is reversed.\n"
        "• One reward per qualifying order."
    )

    await query.edit_message_text(message, reply_markup=_back_keyboard())


async def referral_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📜 Referral History — most recent referrals and what they earned."""
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    if check_user_banned(telegram_id):
        lang = get_user_language(telegram_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    history = referral_service.get_user_history(telegram_id, limit=HISTORY_LIMIT)

    if not history:
        await query.edit_message_text(
            "📜 Referral History\n\n"
            "You have not referred anyone yet.\n"
            "Share your referral link to get started!",
            reply_markup=_back_keyboard()
        )
        return

    lines = ["📜 Referral History\n"]
    for entry in history:
        name = (f"@{entry['referred_username']}" if entry["referred_username"]
                else f"User {entry['referred_telegram_id']}")
        icon = "✅" if entry["status"] == ReferralStatus.QUALIFIED else "⏳"
        earned = (f" · 💵 {format_price(entry['earned'])}"
                  if entry["earned"] > referral_service.ZERO else "")
        joined = format_datetime(entry["created_at"]) if entry["created_at"] else "-"
        lines.append(f"{icon} {name} · {joined}{earned}")

    if len(history) == HISTORY_LIMIT:
        lines.append(f"\nShowing your {HISTORY_LIMIT} most recent referrals.")

    await query.edit_message_text("\n".join(lines), reply_markup=_back_keyboard())
