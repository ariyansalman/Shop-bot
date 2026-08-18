"""OWNER-only admin management screens (add / list / remove admins).

Every handler here re-checks is_owner() server-side. Hiding the "Manage
Admins" button from STAFF is only cosmetic — a STAFF admin who replays a
callback still gets rejected here, and can never grant OWNER to themselves
or anyone else.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_db_session, Admin, AdminRole
from utils import (
    is_owner, clear_admin_cache,
    create_admin_main_menu_keyboard, create_manage_admins_keyboard,
)

logger = logging.getLogger(__name__)

# Conversation states
ADMIN_ID_INPUT, ADMIN_ROLE_INPUT = range(2)


def _load_admins():
    """Return admins as plain dicts (safe to use after the session closes)."""
    with get_db_session() as session:
        rows = session.query(Admin).order_by(Admin.created_at.asc()).all()
        return [
            {
                'id': a.id,
                'telegram_id': a.telegram_id,
                'username': a.username,
                'role': a.role.name if a.role else 'STAFF',
            }
            for a in rows
        ]


def _admins_text(admins) -> str:
    text = "👥 Manage Admins\n\n"
    if not admins:
        text += "No admins found."
        return text
    for a in admins:
        label = f"@{a['username']}" if a['username'] else "no username"
        icon = "👑" if a['role'] == 'OWNER' else "🛠"
        text += f"{icon} {label} — ID {a['telegram_id']} ({a['role']})\n"
    text += (
        "\nOWNER: full access, including admins, wallet adjustments and audit data.\n"
        "STAFF: products, orders and broadcasts only."
    )
    return text


def _cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_add_admin")]])


async def _deny(update: Update) -> None:
    query = update.callback_query
    if query:
        await query.answer("⛔ Only the owner can manage admins.", show_alert=True)
    elif update.message:
        await update.message.reply_text("⛔ Only the owner can manage admins.")


async def manage_admins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the admin list (OWNER only)."""
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await _deny(update)
        return

    admins = _load_admins()
    await query.edit_message_text(
        _admins_text(admins),
        reply_markup=create_manage_admins_keyboard(admins, update.effective_user.id)
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Absorb taps on the non-interactive admin row labels."""
    await update.callback_query.answer()


async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for the new admin's Telegram ID (and optional username)."""
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    context.user_data.pop('new_admin_id', None)
    context.user_data.pop('new_admin_username', None)

    await query.edit_message_text(
        "➕ Add Admin\n\n"
        "Send the new admin's numeric Telegram ID.\n"
        "Optionally add a username after a space, e.g.:\n"
        "`123456789 @janedoe`",
        reply_markup=_cancel_keyboard(),
        parse_mode="Markdown"
    )
    return ADMIN_ID_INPUT


async def add_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate the Telegram ID, then ask which role to grant."""
    if not is_owner(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    parts = (update.message.text or "").strip().split()
    if not parts or not parts[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "❌ That doesn't look like a Telegram ID. Send the numeric ID (e.g. 123456789):",
            reply_markup=_cancel_keyboard()
        )
        return ADMIN_ID_INPUT

    telegram_id = int(parts[0])
    if telegram_id <= 0:
        await update.message.reply_text(
            "❌ Telegram IDs are positive numbers. Please try again:",
            reply_markup=_cancel_keyboard()
        )
        return ADMIN_ID_INPUT

    username = parts[1].lstrip("@") if len(parts) > 1 else None

    with get_db_session() as session:
        existing = session.query(Admin).filter_by(telegram_id=telegram_id).first()
        if existing:
            await update.message.reply_text(
                f"⚠️ {telegram_id} is already an admin ({existing.role.name}).",
                reply_markup=create_admin_main_menu_keyboard(True)
            )
            return ConversationHandler.END

    context.user_data['new_admin_id'] = telegram_id
    context.user_data['new_admin_username'] = username

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 STAFF (products / orders / broadcast)", callback_data="new_admin_role_STAFF")],
        [InlineKeyboardButton("👑 OWNER (full access)", callback_data="new_admin_role_OWNER")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_add_admin")],
    ])
    await update.message.reply_text(
        f"👤 Telegram ID: {telegram_id}\n"
        f"🏷 Username: {'@' + username if username else 'not set'}\n\n"
        "Choose a role:",
        reply_markup=keyboard
    )
    return ADMIN_ROLE_INPUT


async def add_admin_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create the admin row with the chosen role (OWNER grant is owner-gated)."""
    query = update.callback_query
    await query.answer()

    # Server-side gate: only an OWNER reaches this point, so a STAFF admin
    # can never grant OWNER (or any role) to themselves or anyone else.
    if not is_owner(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    role_name = query.data.rsplit("_", 1)[1]
    if role_name not in ("STAFF", "OWNER"):
        await query.edit_message_text("❌ Invalid role.", reply_markup=create_admin_main_menu_keyboard(True))
        return ConversationHandler.END

    telegram_id = context.user_data.get('new_admin_id')
    username = context.user_data.get('new_admin_username')
    if not telegram_id:
        await query.edit_message_text(
            "❌ This flow expired. Please start again.",
            reply_markup=create_admin_main_menu_keyboard(True)
        )
        return ConversationHandler.END

    with get_db_session() as session:
        if session.query(Admin).filter_by(telegram_id=telegram_id).first():
            await query.edit_message_text(
                f"⚠️ {telegram_id} is already an admin.",
                reply_markup=create_admin_main_menu_keyboard(True)
            )
            return ConversationHandler.END

        actor = session.query(Admin).filter_by(telegram_id=update.effective_user.id).first()
        session.add(Admin(
            telegram_id=telegram_id,
            username=username,
            role=AdminRole[role_name],
            added_by=actor.id if actor else None,
        ))

    clear_admin_cache(telegram_id)
    logger.info("Owner %s added admin %s as %s", update.effective_user.id, telegram_id, role_name)

    context.user_data.pop('new_admin_id', None)
    context.user_data.pop('new_admin_username', None)

    admins = _load_admins()
    await query.edit_message_text(
        f"✅ Added {telegram_id} as {role_name}.\n\n" + _admins_text(admins),
        reply_markup=create_manage_admins_keyboard(admins, update.effective_user.id)
    )
    return ConversationHandler.END


async def cancel_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the add-admin flow."""
    context.user_data.pop('new_admin_id', None)
    context.user_data.pop('new_admin_username', None)

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "❌ Cancelled.",
            reply_markup=create_admin_main_menu_keyboard(is_owner(update.effective_user.id))
        )
    else:
        await update.message.reply_text(
            "❌ Cancelled.",
            reply_markup=create_admin_main_menu_keyboard(is_owner(update.effective_user.id))
        )
    return ConversationHandler.END


async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before removing an admin."""
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await _deny(update)
        return

    try:
        admin_row_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid admin.", show_alert=True)
        return

    with get_db_session() as session:
        admin = session.query(Admin).filter_by(id=admin_row_id).first()
        if not admin:
            await query.answer("❌ Admin not found.", show_alert=True)
            return
        label = f"@{admin.username}" if admin.username else str(admin.telegram_id)
        role = admin.role.name if admin.role else 'STAFF'
        target_telegram_id = admin.telegram_id

    if target_telegram_id == update.effective_user.id:
        await query.answer("⛔ You can't remove yourself.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Remove", callback_data=f"admin_confirm_remove_admin_{admin_row_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_admins")],
    ])
    await query.edit_message_text(
        f"⚠️ Remove admin\n\n{label} — ID {target_telegram_id} ({role})\n\n"
        "They will immediately lose access to the admin panel.",
        reply_markup=keyboard
    )


async def confirm_remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the admin row (never the last OWNER, never yourself)."""
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await _deny(update)
        return

    try:
        admin_row_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid admin.", show_alert=True)
        return

    removed_id = None
    with get_db_session() as session:
        admin = session.query(Admin).filter_by(id=admin_row_id).first()
        if not admin:
            await query.answer("❌ Admin not found.", show_alert=True)
            return

        if admin.telegram_id == update.effective_user.id:
            await query.answer("⛔ You can't remove yourself.", show_alert=True)
            return

        if admin.role == AdminRole.OWNER:
            owner_count = session.query(Admin).filter_by(role=AdminRole.OWNER).count()
            if owner_count <= 1:
                await query.answer("⛔ The last owner can't be removed.", show_alert=True)
                return

        removed_id = admin.telegram_id
        # Keep self-referencing rows valid: admins added by this one become orphaned.
        session.query(Admin).filter_by(added_by=admin.id).update(
            {Admin.added_by: None}, synchronize_session=False
        )
        session.delete(admin)

    clear_admin_cache(removed_id)
    logger.info("Owner %s removed admin %s", update.effective_user.id, removed_id)

    admins = _load_admins()
    await query.edit_message_text(
        f"✅ Removed admin {removed_id}.\n\n" + _admins_text(admins),
        reply_markup=create_manage_admins_keyboard(admins, update.effective_user.id)
    )
