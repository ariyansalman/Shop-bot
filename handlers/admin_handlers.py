"""Admin panel command and callback handlers."""

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
from database import (
    get_db_session, User, Category, Subcategory, Product, ProductKey,
    Order, OrderItem, Transaction, Settings, Broadcast, AdminActionLog,
    ProductType, OrderStatus, DisputeStatus, TransactionStatus
)
from services import referral as referral_service
from services import order_completion
from handlers.admin_referrals import notify_referrer
from utils import (
    is_admin, is_owner, admin_only, format_price,
    create_admin_main_menu_keyboard, create_admin_product_menu_keyboard,
    create_admin_category_menu_keyboard, create_admin_user_menu_keyboard,
    create_admin_order_menu_keyboard, create_admin_settings_menu_keyboard,
    create_admin_broadcast_menu_keyboard, create_admin_discount_menu_keyboard,
    parse_keys_from_text, clear_ban_cache, log_admin_action
)
from config.settings import settings as app_settings
from telegram.ext import ConversationHandler

logger = logging.getLogger(__name__)

# Conversation states for restock keys
WAITING_FOR_KEYS = 1


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command - show admin panel."""
    await update.message.reply_text(
        "🔐 Admin Panel\n\nSelect an option:",
        reply_markup=create_admin_main_menu_keyboard(is_owner(update.effective_user.id))
    )


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin menu callback - return to admin main menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.edit_message_text(
        "🔐 Admin Panel\n\nSelect an option:",
        reply_markup=create_admin_main_menu_keyboard(is_owner(update.effective_user.id))
    )


async def admin_restock_keys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin restock keys button - show product selection."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        # Get all KEY type products
        products = session.query(Product).filter_by(product_type=ProductType.KEY).all()

        if not products:
            await query.edit_message_text(
                "❌ No KEY products found. Please create a product first.",
                reply_markup=create_admin_product_menu_keyboard()
            )
            return

        # Build product selection keyboard
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = []
        for product in products[:10]:  # Show first 10
            keyboard.append([
                InlineKeyboardButton(
                    f"📦 {product.name} (Stock: {product.stock_count})",
                    callback_data=f"select_product_{product.id}"
                )
            ])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])

        await query.edit_message_text(
            "🔄 Select a product to restock:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_select_product_restock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection for restocking."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract product ID from callback data
    product_id = int(query.data.split("_")[2])

    # Store product ID in context for later use
    context.user_data['restock_product_id'] = product_id

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await query.edit_message_text(
                "❌ Product not found.",
                reply_markup=create_admin_product_menu_keyboard()
            )
            return

        message = f"""🔄 Restocking: {product.name}
Current Stock: {product.stock_count}

📤 Upload a .txt file with keys (one per line)
OR
✍️ Paste keys directly (one per line)

Example:
KEY1-XXXX-XXXX-XXXX
KEY2-XXXX-XXXX-XXXX
KEY3-XXXX-XXXX-XXXX"""

        # Create keyboard with cancel button
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_restock")]]

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        # Return state to wait for keys
        return WAITING_FOR_KEYS


async def admin_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin products menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "📦 Product Management\n\nSelect an option:",
            reply_markup=create_admin_product_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_low_stock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List active products at or below their low stock threshold."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        products = session.query(Product).filter(
            Product.is_active == True,  # noqa: E712
            Product.stock_count <= Product.low_stock_threshold
        ).order_by(Product.stock_count.asc()).all()

        if not products:
            message = "✅ No low stock products.\n\nAll active products are above their thresholds."
        else:
            message = f"⚠️ Low Stock Products ({len(products)}):\n\n"
            for p in products:
                status = "❌ OUT OF STOCK" if p.stock_count == 0 else f"📉 {p.stock_count} left"
                message += (
                    f"📦 {p.name} (ID: #{p.id})\n"
                    f"   {status} • Threshold: {p.low_stock_threshold}\n\n"
                )

    keyboard = [
        [InlineKeyboardButton("🔄 Restock Keys", callback_data="admin_restock_keys")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_products")]
    ]

    try:
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass


async def admin_manage_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin category management menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "📁 Category Management\n\nSelect an option:",
            reply_markup=create_admin_category_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_view_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all categories and subcategories."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        categories = session.query(Category).all()

        if not categories:
            await query.edit_message_text("📁 No categories found.")
            return

        message = "📁 Categories & Subcategories:\n\n"

        for cat in categories:
            message += f"📦 {cat.name} (ID: #{cat.id})\n"
            if cat.description:
                message += f"   {cat.description}\n"

            subcategories = session.query(Subcategory).filter_by(category_id=cat.id).all()
            if subcategories:
                for subcat in subcategories:
                    message += f"   └─ {subcat.name} (ID: #{subcat.id})\n"

            message += "\n"

        await query.edit_message_text(message, reply_markup=create_admin_category_menu_keyboard())


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin users menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "👥 User Management\n\nSelect an option:",
            reply_markup=create_admin_user_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin orders menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "🛍 Order Management\n\nSelect an option:",
            reply_markup=create_admin_order_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_discounts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin discount codes menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "🎟️ Discount Codes\n\nSelect an option:",
            reply_markup=create_admin_discount_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin settings menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "⚙️ Store Settings\n\nSelect an option:",
            reply_markup=create_admin_settings_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin broadcast menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        await query.edit_message_text(
            "📢 Broadcast Messages\n\nSelect an option:",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


def _local_day_start_utc(days_ago: int = 0) -> datetime:
    """Naive-UTC datetime for local midnight, `days_ago` days before today.

    Transaction/Order timestamps are stored as naive UTC (datetime.utcnow()),
    so "today" has to be computed in the store's configured timezone
    (settings.TIMEZONE) and then converted back to naive UTC to be usable
    in a query filter against those columns.
    """
    tz = ZoneInfo(app_settings.TIMEZONE)
    local_midnight_today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    local_start = local_midnight_today - timedelta(days=days_ago)
    return local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _local_month_start_utc() -> datetime:
    """Naive-UTC datetime for the start of the current local calendar month."""
    tz = ZoneInfo(app_settings.TIMEZONE)
    local_month_start = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_month_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin stats dashboard - revenue, orders, top products, users."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    today_start = _local_day_start_utc(0)
    week_start = _local_day_start_utc(6)     # today + 6 previous days = 7 days
    month_window_start = _local_day_start_utc(29)  # today + 29 previous days = 30 days
    month_start = _local_month_start_utc()

    with get_db_session() as session:
        def revenue_since(start_utc):
            return session.query(func.sum(Transaction.amount)).filter(
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.completed_at >= start_utc
            ).scalar() or 0

        revenue_today = revenue_since(today_start)
        revenue_7d = revenue_since(week_start)
        revenue_30d = revenue_since(month_window_start)
        revenue_all_time = session.query(func.sum(Transaction.amount)).filter(
            Transaction.status == TransactionStatus.COMPLETED
        ).scalar() or 0

        orders_today = session.query(func.count(Order.id)).filter(
            Order.status == OrderStatus.COMPLETED,
            Order.completed_at >= today_start
        ).scalar() or 0

        orders_month = session.query(func.count(Order.id)).filter(
            Order.status == OrderStatus.COMPLETED,
            Order.completed_at >= month_start
        ).scalar() or 0

        top_products = session.query(
            Product.name,
            func.sum(OrderItem.quantity).label("units_sold")
        ).join(
            OrderItem, OrderItem.product_id == Product.id
        ).join(
            Order, Order.id == OrderItem.order_id
        ).filter(
            Order.status == OrderStatus.COMPLETED
        ).group_by(
            Product.id, Product.name
        ).order_by(
            func.sum(OrderItem.quantity).desc()
        ).limit(5).all()

        active_users = session.query(func.count(User.id)).filter(User.is_banned.is_(False)).scalar() or 0
        banned_users = session.query(func.count(User.id)).filter(User.is_banned.is_(True)).scalar() or 0

    message = "📊 Store Stats\n\n"
    message += "💰 Revenue\n"
    message += f"Today: {format_price(revenue_today)}\n"
    message += f"Last 7 days: {format_price(revenue_7d)}\n"
    message += f"Last 30 days: {format_price(revenue_30d)}\n"
    message += f"All-time: {format_price(revenue_all_time)}\n\n"
    message += "🛍 Completed Orders\n"
    message += f"Today: {orders_today}\n"
    message += f"This month: {orders_month}\n\n"
    message += "🏆 Top 5 Best-Sellers\n"
    if top_products:
        for i, (name, units_sold) in enumerate(top_products, start=1):
            message += f"{i}. {name} — {int(units_sold)} sold\n"
    else:
        message += "No completed sales yet.\n"
    message += "\n👥 Users\n"
    message += f"Active: {active_users}\n"
    message += f"Banned: {banned_users}\n"

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]

    try:
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        # Refresh tapped with no new data since the last render - Telegram
        # rejects an edit that doesn't change the message, ignore it.
        pass


async def admin_view_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of users."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    with get_db_session() as session:
        # Get all users
        all_users = session.query(User).order_by(User.created_at.desc()).all()

        if not all_users:
            await query.edit_message_text(
                "👥 No users found.",
                reply_markup=create_admin_user_menu_keyboard()
            )
            return

        # Pagination settings
        items_per_page = 5
        total_pages = (len(all_users) + items_per_page - 1) // items_per_page
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        users = all_users[start_idx:end_idx]

        # Build user selection keyboard
        keyboard = []
        for user in users:
            status_icon = "🚫" if user.is_banned else "✅"
            username_display = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_icon} {username_display} - {format_price(user.wallet_balance)}",
                    callback_data=f"view_user_{user.id}"
                )
            ])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_users_page_{page-1}"))
            pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_users_page_{page+1}"))
            keyboard.append(pagination_row)

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_users")])

        await query.edit_message_text(
            "👥 User List\n\nSelect a user to view details:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )



# Action types offered as filters on the audit log screen. Kept as an explicit
# list (rather than a DISTINCT query) so the filter row stays stable and the
# callback_data indexes below never shift between renders.
AUDIT_LOG_FILTERS = [
    ("ban_user", "🚫 Bans"),
    ("unban_user", "✅ Unbans"),
    ("cancel_order", "❌ Cancelled orders"),
    ("complete_order", "📦 Completed orders"),
    ("confirm_payment", "💰 Payments"),
    ("adjust_balance", "⚖️ Balance changes"),
    ("create_product", "➕ Products created"),
    ("edit_product", "✏️ Products edited"),
    ("delete_product", "🗑 Products deleted"),
    ("broadcast_sent", "📢 Broadcasts"),
    ("referral_settings", "🤝 Referral settings"),
    ("referral_manual_credit", "💳 Referral credits"),
]

# Actions grouped under the "settings" filter chip.
AUDIT_LOG_SETTINGS_ACTIONS = [
    "update_welcome_message",
    "update_support_username",
    "update_channel_username",
    "update_store_logo",
    "update_faq_text",
]


def _format_audit_entry(entry):
    """One-line-ish rendering of a single AdminActionLog row."""
    when = entry.created_at.strftime("%Y-%m-%d %H:%M") if entry.created_at else "unknown time"
    target = ""
    if entry.target_type:
        target = f" → {entry.target_type}"
        if entry.target_id is not None:
            target += f" #{entry.target_id}"

    line = f"🕒 {when}\n👮 Admin {entry.admin_telegram_id}\n🔸 {entry.action}{target}"

    if entry.details:
        details = entry.details
        if len(details) > 200:
            details = details[:200] + "…"
        line += f"\n📝 {details}"

    return line


async def admin_audit_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the paginated admin audit trail, newest first.

    Callback data shapes (mirrors admin_view_users_callback pagination):
        admin_audit_log                        -> page 0, no filter
        admin_audit_log_page_<n>               -> page n, current filter kept
        admin_audit_log_filter_<action|all>    -> switch filter, back to page 0
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    data = query.data

    # Filter selection resets pagination; otherwise keep the active filter.
    if "_filter_" in data:
        action_filter = data.split("_filter_")[1]
        context.user_data['audit_log_filter'] = action_filter
        page = 0
    else:
        action_filter = context.user_data.get('audit_log_filter', 'all')
        page = 0
        if "_page_" in data:
            page = int(data.split("_page_")[1])

    with get_db_session() as session:
        entries_query = session.query(AdminActionLog)

        if action_filter == 'settings':
            entries_query = entries_query.filter(
                AdminActionLog.action.in_(AUDIT_LOG_SETTINGS_ACTIONS)
            )
        elif action_filter != 'all':
            entries_query = entries_query.filter(AdminActionLog.action == action_filter)

        all_entries = entries_query.order_by(
            AdminActionLog.created_at.desc(), AdminActionLog.id.desc()
        ).all()

        # Pagination settings (same shape as admin_view_users_callback)
        items_per_page = 5
        total_pages = max(1, (len(all_entries) + items_per_page - 1) // items_per_page)
        page = min(page, total_pages - 1)
        start_idx = page * items_per_page
        entries = all_entries[start_idx:start_idx + items_per_page]

        filter_label = "All actions"
        if action_filter == 'settings':
            filter_label = "⚙️ Settings changes"
        else:
            for value, label in AUDIT_LOG_FILTERS:
                if value == action_filter:
                    filter_label = label
                    break

        if not all_entries:
            message = (
                f"📋 Audit Log\n\n"
                f"Filter: {filter_label}\n\n"
                f"No admin actions recorded yet."
            )
        else:
            body = "\n\n".join(_format_audit_entry(entry) for entry in entries)
            message = (
                f"📋 Audit Log\n\n"
                f"Filter: {filter_label}\n"
                f"Showing {start_idx + 1}-{start_idx + len(entries)} of {len(all_entries)}\n\n"
                f"{body}"
            )

        keyboard = []

        # Pagination row
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton(
                    "◀️ Previous", callback_data=f"admin_audit_log_page_{page-1}"))
            pagination_row.append(InlineKeyboardButton(
                f"Page {page+1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton(
                    "Next ▶️", callback_data=f"admin_audit_log_page_{page+1}"))
            keyboard.append(pagination_row)

        # Filter chips, two per row
        keyboard.append([InlineKeyboardButton(
            f"{'•' if action_filter == 'all' else ''} 🔎 All actions",
            callback_data="admin_audit_log_filter_all"
        )])

        chips = list(AUDIT_LOG_FILTERS) + [("settings", "⚙️ Settings changes")]
        for i in range(0, len(chips), 2):
            row = []
            for value, label in chips[i:i + 2]:
                prefix = "• " if value == action_filter else ""
                row.append(InlineKeyboardButton(
                    f"{prefix}{label}",
                    callback_data=f"admin_audit_log_filter_{value}"
                ))
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_user_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual user details with Ban/Unban button."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Handle pagination - redirect back to user list
    if "admin_view_users_page_" in query.data:
        return await admin_view_users_callback(update, context)

    # Extract user ID from callback data
    user_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            await query.edit_message_text(
                "❌ User not found.",
                reply_markup=create_admin_user_menu_keyboard()
            )
            return

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status='completed').with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Manual wallet credit/debit (see admin_conversations.adjust_balance_start)
        # Wallet adjustments are OWNER-only (also enforced in the handler).
        if is_owner(update.effective_user.id):
            keyboard.append([InlineKeyboardButton("💰 Adjust Balance", callback_data=f"adjust_balance_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show an 'are you sure?' confirmation before banning a user."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract user ID from callback data
    user_id = int(query.data.rsplit("_", 1)[1])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            await query.edit_message_text(
                "❌ User not found.",
                reply_markup=create_admin_user_menu_keyboard()
            )
            return

        username_display = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"

        keyboard = [
            [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_ban_user_{user.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"view_user_{user.id}")]
        ]

        await query.edit_message_text(
            f"⚠️ Ban user {username_display}? This will prevent them from using the store.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_confirm_ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle banning a user (after confirmation)."""
    query = update.callback_query
    await query.answer("✅ User banned successfully!", show_alert=True)

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract user ID from callback data
    user_id = int(query.data.rsplit("_", 1)[1])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            await query.edit_message_text(
                "❌ User not found.",
                reply_markup=create_admin_user_menu_keyboard()
            )
            return

        # Store telegram_id before committing
        telegram_id = user.telegram_id

        user.is_banned = True
        session.commit()

        log_admin_action(
            update.effective_user.id, "ban_user", "user", user_id,
            {"telegram_id": telegram_id, "username": user.username},
            session=session
        )

        # Clear ban cache for this user
        clear_ban_cache(telegram_id)

        # Refresh user details page - get updated data
        user = session.query(User).filter_by(id=user_id).first()

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status='completed').with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_unban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unbanning a user."""
    query = update.callback_query
    await query.answer("✅ User unbanned successfully!", show_alert=True)

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract user ID from callback data
    user_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()

        if not user:
            await query.edit_message_text(
                "❌ User not found.",
                reply_markup=create_admin_user_menu_keyboard()
            )
            return

        # Store telegram_id before committing
        telegram_id = user.telegram_id

        user.is_banned = False
        session.commit()

        log_admin_action(
            update.effective_user.id, "unban_user", "user", user_id,
            {"telegram_id": telegram_id, "username": user.username},
            session=session
        )

        # Clear ban cache for this user
        clear_ban_cache(telegram_id)

        # Refresh user details page - get updated data
        user = session.query(User).filter_by(id=user_id).first()

        # Get user statistics
        orders_count = session.query(Order).filter_by(user_id=user.id).count()
        total_spent = session.query(Order).filter_by(user_id=user.id, status='completed').with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0

        # Format user details
        status = "🚫 Banned" if user.is_banned else "✅ Active"
        username_display = f"@{user.username}" if user.username else "N/A"

        message = f"👤 User Details\n\n"
        message += f"Telegram ID: {user.telegram_id}\n"
        message += f"Username: {username_display}\n"
        message += f"Balance: {format_price(user.wallet_balance)}\n"
        message += f"Status: {status}\n"
        message += f"Total Orders: {orders_count}\n"
        message += f"Total Spent: {format_price(total_spent)}\n"
        message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

        # Build action keyboard
        keyboard = []

        # Ban/Unban button
        if user.is_banned:
            keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])

        # Back button
        keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_view_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of recent orders with management buttons."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    with get_db_session() as session:
        # Get all orders
        all_orders = session.query(Order).order_by(Order.created_at.desc()).all()

        if not all_orders:
            await query.edit_message_text(
                "🛍 No orders found.",
                reply_markup=create_admin_order_menu_keyboard()
            )
            return

        # Pagination settings
        orders_per_page = 5
        total_pages = (len(all_orders) + orders_per_page - 1) // orders_per_page
        start_idx = page * orders_per_page
        end_idx = start_idx + orders_per_page
        orders = all_orders[start_idx:end_idx]

        # Build message
        message = f"🛍 Recent Orders (Page {page + 1}/{total_pages}):\n\n"

        # Build keyboard with order buttons
        keyboard = []

        for order in orders:
            user = session.query(User).filter_by(id=order.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            # Format status emoji
            status_emoji = {
                OrderStatus.PROCESSING: "⏳",
                OrderStatus.COMPLETED: "✅",
                OrderStatus.CANCELLED: "❌"
            }.get(order.status, "❓")

            # Button text: Order #ID | User | Status | Amount
            button_text = f"{status_emoji} Order #{order.id} | @{username} | {format_price(order.total_amount)}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_order_{order.id}")])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_orders_page_{page-1}"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_orders_page_{page+1}"))
            if pagination_row:
                keyboard.append(pagination_row)

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_orders")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def handle_restock_keys_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file upload for restocking keys."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    # Get the uploaded file
    document = update.message.document

    if not document:
        await update.message.reply_text("❌ Please upload a text file with keys. Try again or /cancel")
        return WAITING_FOR_KEYS

    # Download file
    file = await context.bot.get_file(document.file_id)
    file_content = await file.download_as_bytearray()

    # Parse keys
    text = file_content.decode('utf-8')
    keys = parse_keys_from_text(text)

    if not keys:
        await update.message.reply_text("❌ No keys found in file. Try again or /cancel")
        return WAITING_FOR_KEYS

    # Get product ID from context (should be set earlier)
    product_id = context.user_data.get('restock_product_id')

    if not product_id:
        await update.message.reply_text("❌ Error: Product not selected. Please start over.")
        return ConversationHandler.END

    # Add keys to product_keys table
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await update.message.reply_text("❌ Product not found.")
            return

        # Insert keys into product_keys table
        added_count = 0
        for key_value in keys:
            product_key = ProductKey(
                product_id=product.id,
                key_value=key_value,
                is_sold=False
            )
            session.add(product_key)
            added_count += 1

        # Update product stock count
        product.stock_count += added_count
        session.commit()

        log_admin_action(
            update.effective_user.id, "restock_keys", "product", product.id,
            {"product_name": product.name, "keys_added": added_count,
             "new_stock": product.stock_count},
            session=session
        )

        # Create keyboard with options
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [InlineKeyboardButton("🔄 Restock More Keys", callback_data="admin_restock_keys")],
            [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ Successfully added {added_count} keys to {product.name}!\n"
            f"New stock count: {product.stock_count}",
            reply_markup=reply_markup
        )

        # Clear restock_product_id from context
        context.user_data.pop('restock_product_id', None)

        return ConversationHandler.END


async def handle_restock_keys_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pasted keys for restocking."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    # Parse keys from message text
    keys = parse_keys_from_text(update.message.text)

    if not keys:
        await update.message.reply_text("❌ No keys found. Please paste keys (one per line). Try again or /cancel")
        return WAITING_FOR_KEYS

    # Get product ID from context (should be set earlier)
    product_id = context.user_data.get('restock_product_id')

    if not product_id:
        await update.message.reply_text("❌ Error: Product not selected. Please start over.")
        return ConversationHandler.END

    # Add keys to product_keys table
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await update.message.reply_text("❌ Product not found.")
            return

        # Insert keys into product_keys table
        added_count = 0
        for key_value in keys:
            product_key = ProductKey(
                product_id=product.id,
                key_value=key_value,
                is_sold=False
            )
            session.add(product_key)
            added_count += 1

        # Update product stock count
        product.stock_count += added_count
        session.commit()

        log_admin_action(
            update.effective_user.id, "restock_keys", "product", product.id,
            {"product_name": product.name, "keys_added": added_count,
             "new_stock": product.stock_count},
            session=session
        )

        # Create keyboard with options
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [InlineKeyboardButton("🔄 Restock More Keys", callback_data="admin_restock_keys")],
            [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ Successfully added {added_count} keys to {product.name}!\n"
            f"New stock count: {product.stock_count}",
            reply_markup=reply_markup
        )

        # Clear restock_product_id from context
        context.user_data.pop('restock_product_id', None)

        return ConversationHandler.END


async def handle_welcome_message_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome message update from admin."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    new_welcome_message = update.message.text

    with get_db_session() as session:
        settings = session.query(Settings).first()

        if not settings:
            settings = Settings()
            session.add(settings)

        settings.welcome_message = new_welcome_message
        settings.updated_at = datetime.utcnow()
        session.commit()

        log_admin_action(
            update.effective_user.id, "update_welcome_message", "settings", settings.id,
            {"welcome_message": new_welcome_message},
            session=session
        )

        await update.message.reply_text("✅ Welcome message updated successfully!")


async def handle_logo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle store logo upload from admin."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Get the uploaded photo
    photo = update.message.photo[-1]  # Get highest resolution

    # Download photo
    file = await context.bot.get_file(photo.file_id)
    logo_path = os.path.join(app_settings.LOGOS_DIR, f"store_logo_{int(datetime.utcnow().timestamp())}.jpg")

    # Ensure directory exists
    os.makedirs(app_settings.LOGOS_DIR, exist_ok=True)

    await file.download_to_drive(logo_path)

    # Update settings
    with get_db_session() as session:
        settings = session.query(Settings).first()

        if not settings:
            settings = Settings()
            session.add(settings)

        settings.store_logo_path = logo_path
        settings.updated_at = datetime.utcnow()
        session.commit()

        log_admin_action(
            update.effective_user.id, "update_store_logo", "settings", settings.id,
            {"store_logo_path": logo_path},
            session=session
        )

        await update.message.reply_text("✅ Store logo updated successfully!")


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text-only broadcast to all users."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    broadcast_text = update.message.text

    with get_db_session() as session:
        # Get all users
        users = session.query(User).filter_by(is_banned=False).all()

        sent_count = 0

        for user in users:
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=broadcast_text
                )
                sent_count += 1
            except Exception as e:
                print(f"Failed to send to user {user.telegram_id}: {e}")

        # Save broadcast record
        broadcast = Broadcast(
            message_text=broadcast_text,
            sent_count=sent_count
        )
        session.add(broadcast)
        session.commit()

        log_admin_action(
            update.effective_user.id, "broadcast_sent", "broadcast", broadcast.id,
            {"type": "text", "sent_count": sent_count,
             "message_preview": broadcast_text[:200]},
            session=session
        )

        await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users!")


async def handle_broadcast_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image + text broadcast to all users (as separate messages)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Get image and caption
    photo = update.message.photo[-1]  # Get highest resolution
    caption_text = update.message.caption or ""

    # Download photo
    file = await context.bot.get_file(photo.file_id)
    image_path = os.path.join(app_settings.ASSETS_DIR, f"broadcast_{int(datetime.utcnow().timestamp())}.jpg")

    os.makedirs(app_settings.ASSETS_DIR, exist_ok=True)
    await file.download_to_drive(image_path)

    with get_db_session() as session:
        # Get all users
        users = session.query(User).filter_by(is_banned=False).all()

        sent_count = 0

        for user in users:
            try:
                # Send image first
                with open(image_path, 'rb') as img:
                    await context.bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=img
                    )

                # Send text as separate message
                if caption_text:
                    await context.bot.send_message(
                        chat_id=user.telegram_id,
                        text=caption_text
                    )

                sent_count += 1
            except Exception as e:
                print(f"Failed to send to user {user.telegram_id}: {e}")

        # Save broadcast record
        broadcast = Broadcast(
            message_text=caption_text,
            image_path=image_path,
            sent_count=sent_count
        )
        session.add(broadcast)
        session.commit()

        log_admin_action(
            update.effective_user.id, "broadcast_sent", "broadcast", broadcast.id,
            {"type": "image_text", "sent_count": sent_count,
             "image_path": image_path, "message_preview": caption_text[:200]},
            session=session
        )

        await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users!")


async def handle_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban/unban user command."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    # Expected format: telegram_id ban/unban
    try:
        parts = update.message.text.split()
        telegram_id = int(parts[0])
        action = parts[1].lower()

        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=telegram_id).first()

            if not user:
                await update.message.reply_text("❌ User not found.")
                return

            if action == "ban":
                user.is_banned = True
                session.commit()
                log_admin_action(
                    update.effective_user.id, "ban_user", "user", user.id,
                    {"telegram_id": telegram_id, "username": user.username,
                     "via": "text_command"},
                    session=session
                )
                await update.message.reply_text(f"✅ User {telegram_id} has been banned.")
            elif action == "unban":
                user.is_banned = False
                session.commit()
                log_admin_action(
                    update.effective_user.id, "unban_user", "user", user.id,
                    {"telegram_id": telegram_id, "username": user.username,
                     "via": "text_command"},
                    session=session
                )
                await update.message.reply_text(f"✅ User {telegram_id} has been unbanned.")
            else:
                await update.message.reply_text("❌ Invalid action. Use 'ban' or 'unban'.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: telegram_id ban/unban")


async def handle_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order cancellation with wallet refund."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    try:
        order_id = int(update.message.text)

        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                await update.message.reply_text("❌ Order not found.")
                return

            if order.status == OrderStatus.CANCELLED:
                await update.message.reply_text("❌ Order is already cancelled.")
                return

            # Refund to wallet
            user = session.query(User).filter_by(id=order.user_id).first()
            user.wallet_balance += order.total_amount

            # Update order status
            order.status = OrderStatus.CANCELLED
            session.commit()

            log_admin_action(
                update.effective_user.id, "cancel_order", "order", order.id,
                {"refunded": str(order.total_amount), "user_id": order.user_id,
                 "via": "text_command"},
                session=session
            )

            # Same referral reversal as the button-based cancellation path.
            referral_service.revoke_order_reward(
                order.id, reason=f"order #{order.id} cancelled by admin")

            await update.message.reply_text(
                f"✅ Order #{order_id} cancelled successfully!\n"
                f"💰 Refunded {format_price(order.total_amount)} to user's wallet."
            )

            # Notify user
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=f"❌ Order #{order_id} has been cancelled by admin.\n"
                     f"💰 {format_price(order.total_amount)} has been refunded to your wallet."
            )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: order_id")


async def handle_dispute_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order dispute status update."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return

    try:
        # Format: order_id status (opened/resolved)
        parts = update.message.text.split()
        order_id = int(parts[0])
        status = parts[1].lower()

        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                await update.message.reply_text("❌ Order not found.")
                return

            if status == "opened":
                order.dispute_status = DisputeStatus.OPENED
            elif status == "resolved":
                order.dispute_status = DisputeStatus.RESOLVED
            else:
                await update.message.reply_text("❌ Invalid status. Use 'opened' or 'resolved'.")
                return

            session.commit()

            log_admin_action(
                update.effective_user.id, "update_dispute_status", "order", order.id,
                {"dispute_status": status.upper()},
                session=session
            )

            await update.message.reply_text(f"✅ Order #{order_id} dispute status updated to: {status.upper()}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFormat: order_id opened/resolved")


async def admin_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual order details with management buttons."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    # Extract order ID from callback data (works for view_order_X and confirm_*_order_X)
    order_id = int(query.data.rsplit("_", 1)[1])

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            await query.edit_message_text(
                "❌ Order not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_view_orders")]])
            )
            return

        # Get user and order items
        user = session.query(User).filter_by(id=order.user_id).first()
        order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

        # Format status emoji
        status_emoji = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED: "✅",
            OrderStatus.CANCELLED: "❌"
        }.get(order.status, "❓")

        # Build message
        username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"
        message = f"📋 Order Details\n\n"
        message += f"Order ID: #{order.id}\n"
        message += f"Status: {status_emoji} {order.status.value}\n"
        message += f"User: @{username} ({user.telegram_id if user else 'Unknown'})\n"
        message += f"Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        message += f"Total: {format_price(order.total_amount)}\n\n"

        message += "📦 Items:\n"
        for item in order_items:
            product = session.query(Product).filter_by(id=item.product_id).first()
            product_name = product.name if product else "Unknown Product"
            message += f"• {product_name} x{item.quantity} = {format_price(item.price * item.quantity)}\n"

            # Add delivered assets (keys or download links)
            if item.delivered_asset:
                if product and product.product_type == ProductType.KEY:
                    message += f"  🔐 Keys:\n{item.delivered_asset}\n"
                elif product and product.product_type == ProductType.FILE:
                    message += f"  🔗 Download: {item.delivered_asset}\n"
                message += "\n"

        # Build keyboard with management buttons
        keyboard = []

        # Status-specific actions
        if order.status == OrderStatus.PROCESSING:
            keyboard.append([InlineKeyboardButton("✅ Mark as Completed", callback_data=f"complete_order_{order.id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order.id}")])
        elif order.status == OrderStatus.CANCELLED:
            keyboard.append([InlineKeyboardButton("🔄 Reactivate Order", callback_data=f"reactivate_order_{order.id}")])

        # Navigation buttons
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_view_orders")])

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_complete_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark an order as completed."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    order_id = int(query.data.split("_")[2])

    # One shared implementation with the buyer's own checkout: validate state,
    # flip to COMPLETED under a row lock, stamp completed_at exactly once and
    # run the referral qualification. Pressing this button twice is a no-op —
    # no duplicate reward, transaction, timestamp change or notification.
    result = order_completion.complete_order(order_id)

    if result["reason"] == order_completion.REASON_NOT_FOUND:
        await query.edit_message_text("❌ Order not found.")
        return

    if result["reason"] == order_completion.REASON_CANCELLED:
        await query.answer(
            "❌ This order is cancelled. Reactivate it first.", show_alert=True
        )
        await admin_order_detail_callback(update, context)
        return

    if result["newly_completed"]:
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()
            if order:
                log_admin_action(
                    update.effective_user.id, "complete_order", "order", order.id,
                    {"total_amount": str(order.total_amount),
                     "user_id": order.user_id,
                     "completed_at": str(result["completed_at"])},
                    session=session
                )
        await query.answer("✅ Order marked as completed!", show_alert=True)
    else:
        await query.answer("ℹ️ Order was already completed.", show_alert=True)

    # Tell the referrer about a reward this completion produced (best effort;
    # complete_order() returns a reward dict only for the call that created it).
    if result.get("reward"):
        await notify_referrer(context, result["reward"])

    # Refresh order details
    await admin_order_detail_callback(update, context)


async def admin_confirm_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for manual confirmation."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        # Get all pending transactions
        transactions = session.query(Transaction).filter_by(status=TransactionStatus.PENDING).order_by(Transaction.created_at.desc()).all()

        if not transactions:
            keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "✅ No pending payments to confirm.",
                reply_markup=reply_markup
            )
            return

        # Build keyboard with transaction buttons
        keyboard = []
        for txn in transactions:
            user = session.query(User).filter_by(id=txn.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            payment_method = txn.payment_method.value.replace('_', ' ').title()

            button_text = f"⏳ Txn #{txn.id} | @{username} | {format_price(txn.amount)} | {payment_method}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"confirm_payment_{txn.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = f"✅ Manual Payment Confirmation ({len(transactions)} pending)\n\nSelect a transaction to confirm:"

        await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_cancel_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for cancellation."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        # Get all pending transactions
        transactions = session.query(Transaction).filter_by(status=TransactionStatus.PENDING).order_by(Transaction.created_at.desc()).all()

        if not transactions:
            keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "✅ No pending payments to cancel.",
                reply_markup=reply_markup
            )
            return

        # Build keyboard with transaction buttons
        keyboard = []
        for txn in transactions:
            user = session.query(User).filter_by(id=txn.user_id).first()
            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"

            payment_method = txn.payment_method.value.replace('_', ' ').title()
            button_text = f"⏳ Txn #{txn.id} | @{username} | {format_price(txn.amount)} | {payment_method}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"cancel_payment_{txn.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = f"❌ Cancel Payments ({len(transactions)} pending)\n\nSelect a transaction to cancel:"

        await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_confirm_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually confirm a pending payment transaction."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    txn_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        from database import Transaction, TransactionStatus
        from datetime import datetime

        txn = session.query(Transaction).filter_by(id=txn_id).first()

        if not txn:
            await query.edit_message_text("❌ Transaction not found.")
            return

        if txn.status != TransactionStatus.PENDING:
            await query.answer(f"⚠️ Transaction is already {txn.status.value}", show_alert=True)
            return

        # Atomically flip PENDING -> COMPLETED, conditioned on it still being
        # PENDING. Closes the race where two admins (or one admin double-
        # tapping) both pass the check above before either commits, which
        # would otherwise credit the wallet twice for one transaction.
        updated_rows = session.query(Transaction).filter(
            Transaction.id == txn.id,
            Transaction.status == TransactionStatus.PENDING
        ).update({
            Transaction.status: TransactionStatus.COMPLETED,
            Transaction.completed_at: datetime.utcnow()
        }, synchronize_session=False)

        if updated_rows == 0:
            session.rollback()
            await query.answer("⚠️ Transaction was already confirmed elsewhere.", show_alert=True)
            return

        # Add funds to user's wallet
        user = session.query(User).filter_by(id=txn.user_id).first()
        if user:
            user.wallet_balance += txn.amount

        session.commit()

        log_admin_action(
            update.effective_user.id, "confirm_payment", "transaction", txn.id,
            {"amount": str(txn.amount), "user_id": txn.user_id,
             "payment_method": txn.payment_method.value if txn.payment_method else None},
            session=session
        )

        # Get details before session closes
        user_telegram_id = user.telegram_id if user else None
        amount = txn.amount
        new_balance = user.wallet_balance if user else 0

        await query.answer(f"✅ Payment confirmed! {format_price(amount)} added to user's wallet.", show_alert=True)

        # Notify user
        if user_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"✅ Payment Confirmed!\n\n💰 Amount: {format_price(amount)}\n💵 New Balance: {format_price(new_balance)}"
                )
            except Exception as exc:
                logger.warning("Could not notify user: %s", exc)

        # Go back to payment confirmation menu
        await admin_confirm_order_menu(update, context)


async def admin_cancel_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending payment transaction."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    txn_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        from database import Transaction, TransactionStatus

        txn = session.query(Transaction).filter_by(id=txn_id).first()

        if not txn:
            await query.edit_message_text("❌ Transaction not found.")
            return

        if txn.status != TransactionStatus.PENDING:
            await query.answer(f"⚠️ Transaction is already {txn.status.value}", show_alert=True)
            return

        # Mark transaction as failed
        txn.status = TransactionStatus.FAILED
        session.commit()

        log_admin_action(
            update.effective_user.id, "cancel_payment", "transaction", txn.id,
            {"amount": str(txn.amount), "user_id": txn.user_id,
             "payment_method": txn.payment_method.value if txn.payment_method else None},
            session=session
        )

        # Get details before session closes
        user = session.query(User).filter_by(id=txn.user_id).first()
        user_telegram_id = user.telegram_id if user else None
        amount = txn.amount

        await query.answer(f"✅ Payment cancelled!", show_alert=True)

        # Notify user
        if user_telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=user_telegram_id,
                    text=f"❌ Payment Cancelled\n\n💰 Amount: {format_price(amount)}\n\nYour payment was not confirmed. Please contact support if you believe this is an error."
                )
            except Exception as exc:
                logger.warning("Could not notify user: %s", exc)

        # Go back to payment cancellation menu
        await admin_cancel_order_menu(update, context)


async def admin_cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show an 'are you sure?' confirmation before cancelling an order."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    order_id = int(query.data.rsplit("_", 1)[1])

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            await query.edit_message_text("❌ Order not found.")
            return

        keyboard = [
            [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_cancel_order_{order.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"view_order_{order.id}")]
        ]

        await query.edit_message_text(
            f"⚠️ Cancel order #{order.id}? "
            f"{format_price(order.total_amount)} will be refunded to the buyer and this cannot be undone.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_confirm_cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel an order and refund the user (after confirmation)."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    order_id = int(query.data.rsplit("_", 1)[1])

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            await query.edit_message_text("❌ Order not found.")
            return

        user = session.query(User).filter_by(id=order.user_id).first()
        refund_amount = order.total_amount

        # Flip the status atomically first, conditioned on the order not being
        # cancelled yet. This is what makes the refund exactly-once: a
        # double-tapped Confirm button (or two admins acting at the same time)
        # sees the second UPDATE match zero rows, so no second refund is paid.
        cancelled_rows = session.query(Order).filter(
            Order.id == order.id,
            Order.status != OrderStatus.CANCELLED
        ).update({Order.status: OrderStatus.CANCELLED}, synchronize_session=False)

        if cancelled_rows == 0:
            session.rollback()
            await query.answer("❌ Order is already cancelled.", show_alert=True)
            await admin_order_detail_callback(update, context)
            return

        # Credit the refund with a relative UPDATE (not a read-modify-write) so
        # a concurrent wallet change can't be clobbered.
        if user:
            session.query(User).filter(User.id == user.id).update(
                {User.wallet_balance: User.wallet_balance + refund_amount},
                synchronize_session=False
            )

        session.commit()
        session.refresh(order)
        if user:
            session.refresh(user)

        log_admin_action(
            update.effective_user.id, "cancel_order", "order", order.id,
            {"refunded": str(order.total_amount), "user_id": order.user_id},
            session=session
        )

        # Referral safety: a cancelled/refunded order must not keep paying a
        # referral reward. revoke_order_reward() debits the referrer's wallet
        # back, writes a reversing transaction and is exactly-once (a second
        # call matches zero CREDITED rows and does nothing).
        reversed_reward = referral_service.revoke_order_reward(
            order.id, reason=f"order #{order.id} cancelled by admin")

        await query.answer(f"✅ Order cancelled and {format_price(order.total_amount)} refunded!", show_alert=True)

        # Tell the referrer their reward was taken back, so the wallet change
        # is never silent.
        if reversed_reward and reversed_reward.get("referrer_telegram_id"):
            try:
                await context.bot.send_message(
                    chat_id=reversed_reward["referrer_telegram_id"],
                    text=(f"↩️ Referral reward reversed\n\n"
                          f"Order #{order.id} was cancelled, so "
                          f"{format_price(reversed_reward['amount'])} has been "
                          f"deducted from your wallet.")
                )
            except Exception as exc:
                logger.warning("Could not notify referrer about reversal: %s", exc)

        # Notify user
        if user:
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Order #{order.id} has been cancelled by admin.\n💰 Refund: {format_price(order.total_amount)}"
                )
            except Exception as exc:
                logger.warning("Could not notify user: %s", exc)

        # Refresh order details
        await admin_order_detail_callback(update, context)


async def admin_reactivate_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show an 'are you sure?' confirmation before reactivating a cancelled order.

    Reactivation is the exact inverse of admin_confirm_cancel_order_callback:
    cancelling only refunds the buyer's wallet (stock and assigned keys are
    deliberately left untouched), so reactivating only takes that refund back.
    Stock is never re-deducted and keys are never re-assigned here.
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        order_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid order reference.", show_alert=True)
        return

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()

        if not order:
            await query.edit_message_text(
                "❌ Order not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_view_orders")]]
                )
            )
            return

        # Only a cancelled order can be reactivated — no other transition.
        if order.status != OrderStatus.CANCELLED:
            await query.answer(
                f"❌ Order #{order.id} is {order.status.value}, not cancelled.",
                show_alert=True
            )
            await admin_order_detail_callback(update, context)
            return

        user = session.query(User).filter_by(id=order.user_id).first()
        balance_note = ""
        if user is not None and user.wallet_balance < order.total_amount:
            balance_note = (
                f"\n\n⚠️ The buyer's balance is only "
                f"{format_price(user.wallet_balance)} — reactivation will fail "
                f"until they have at least {format_price(order.total_amount)}."
            )

        keyboard = [
            [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_reactivate_order_{order.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"view_order_{order.id}")]
        ]

        await query.edit_message_text(
            f"⚠️ Reactivate order #{order.id}? "
            f"The {format_price(order.total_amount)} refund will be taken back from "
            f"the buyer's wallet. Stock and delivered keys are not changed."
            f"{balance_note}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def admin_confirm_reactivate_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactivate a cancelled order and take the refund back (after confirmation)."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        order_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid order reference.", show_alert=True)
        return

    try:
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                await query.edit_message_text(
                    "❌ Order not found.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_view_orders")]]
                    )
                )
                return

            if order.status != OrderStatus.CANCELLED:
                await query.answer(
                    f"❌ Order #{order.id} is {order.status.value} and cannot be reactivated.",
                    show_alert=True
                )
                await admin_order_detail_callback(update, context)
                return

            user = session.query(User).filter_by(id=order.user_id).first()
            if not user:
                await query.answer("❌ Buyer account not found.", show_alert=True)
                return

            amount = order.total_amount

            # Restore the status the order had before cancellation. An order
            # that already had its assets delivered goes back to COMPLETED;
            # anything else returns to PROCESSING. Nothing is re-delivered.
            delivered = session.query(OrderItem).filter(
                OrderItem.order_id == order.id,
                OrderItem.delivered_asset.isnot(None)
            ).count()
            has_keys = session.query(ProductKey).filter_by(order_id=order.id).count()
            target_status = (
                OrderStatus.COMPLETED
                if (delivered or has_keys or order.completed_at)
                else OrderStatus.PROCESSING
            )

            # Take the refund back atomically, conditioned on the order still
            # being CANCELLED *and* the wallet still covering the amount. A
            # double-tapped button can therefore only ever debit once: the
            # second UPDATE matches zero rows because the status already moved.
            reactivated_rows = session.query(Order).filter(
                Order.id == order.id,
                Order.status == OrderStatus.CANCELLED
            ).update({Order.status: target_status}, synchronize_session=False)

            if reactivated_rows == 0:
                session.rollback()
                await query.answer("❌ Order was already reactivated.", show_alert=True)
                await admin_order_detail_callback(update, context)
                return

            debited_rows = session.query(User).filter(
                User.id == user.id,
                User.wallet_balance >= amount
            ).update({
                User.wallet_balance: User.wallet_balance - amount
            }, synchronize_session=False)

            if debited_rows == 0:
                # Not enough balance to reverse the refund — abandon the whole
                # reactivation so the order stays cancelled and consistent.
                session.rollback()
                await query.answer(
                    f"❌ Buyer's balance is below {format_price(amount)}. "
                    "Reactivation cancelled.",
                    show_alert=True
                )
                await admin_order_detail_callback(update, context)
                return

            # Order history (order, items, delivered assets, assigned keys,
            # transactions) is preserved as-is — only the status and the
            # wallet reversal change.
            session.commit()

            log_admin_action(
                update.effective_user.id, "reactivate_order", "order", order.id,
                {"debited": str(amount), "user_id": order.user_id,
                 "restored_status": target_status.value},
                session=session
            )

            user_telegram_id = user.telegram_id

        await query.answer(
            f"✅ Order reactivated and {format_price(amount)} debited!",
            show_alert=True
        )

        # Notify the buyer
        try:
            await context.bot.send_message(
                chat_id=user_telegram_id,
                text=(
                    f"🔄 Order #{order_id} has been reactivated by admin.\n"
                    f"💰 {format_price(amount)} has been deducted from your wallet."
                )
            )
        except Exception:
            pass

        # Refresh the admin order detail view
        await admin_order_detail_callback(update, context)

    except Exception as e:
        try:
            await query.answer(f"❌ Error reactivating order: {e}", show_alert=True)
        except Exception:
            pass


async def cancel_restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the restock keys conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "❌ Restock cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Restock cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )

    # Clear restock_product_id from context
    context.user_data.pop('restock_product_id', None)

    return ConversationHandler.END


# --- Data export -----------------------------------------------------------

# Cap every export so a large store can never trigger an unbounded query.
EXPORT_ROW_LIMIT = 10000


def create_admin_export_menu_keyboard():
    """Keyboard for the 📤 Export Data screen."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Orders CSV", callback_data="admin_export_orders")],
        [InlineKeyboardButton("👥 Users CSV", callback_data="admin_export_users")],
        [InlineKeyboardButton("💳 Transactions CSV", callback_data="admin_export_transactions")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")],
    ])


def _write_csv(filename_prefix: str, header, rows):
    """Write rows to a temp CSV file and return its path (runs in a thread)."""
    import csv
    import tempfile

    fd, path = tempfile.mkstemp(
        prefix=f"{filename_prefix}_", suffix=".csv"
    )
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _fmt_dt(value):
    """Render a datetime for CSV output, empty string when missing."""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _export_orders_sync():
    """Build the orders CSV. Blocking DB work — call via asyncio.to_thread."""
    with get_db_session() as session:
        orders = session.query(Order).order_by(
            Order.created_at.desc(), Order.id.desc()
        ).limit(EXPORT_ROW_LIMIT).all()

        rows = []
        for order in orders:
            telegram_id = order.user.telegram_id if order.user else ""
            items = order.order_items or []
            if not items:
                rows.append([
                    order.id, telegram_id, "", "", str(order.total_amount),
                    order.status.value if order.status else "",
                    _fmt_dt(order.created_at),
                ])
                continue
            for item in items:
                rows.append([
                    order.id,
                    telegram_id,
                    item.product.name if item.product else "",
                    item.quantity,
                    str(order.total_amount),
                    order.status.value if order.status else "",
                    _fmt_dt(order.created_at),
                ])

    header = [
        "id", "telegram_id", "product_name", "quantity",
        "total_amount", "status", "created_at",
    ]
    return _write_csv("orders_export", header, rows)


def _export_users_sync():
    """Build the users CSV. Blocking DB work — call via asyncio.to_thread."""
    with get_db_session() as session:
        users = session.query(User).order_by(
            User.created_at.desc(), User.id.desc()
        ).limit(EXPORT_ROW_LIMIT).all()

        rows = [
            [
                user.telegram_id,
                user.username or "",
                str(user.wallet_balance if user.wallet_balance is not None else 0),
                bool(user.is_banned),
                _fmt_dt(user.created_at),
            ]
            for user in users
        ]

    header = ["telegram_id", "username", "wallet_balance", "is_banned", "created_at"]
    return _write_csv("users_export", header, rows)


def _export_transactions_sync():
    """Build the transactions CSV. Blocking DB work — call via asyncio.to_thread."""
    with get_db_session() as session:
        transactions = session.query(Transaction).order_by(
            Transaction.created_at.desc(), Transaction.id.desc()
        ).limit(EXPORT_ROW_LIMIT).all()

        rows = [
            [
                tx.id,
                tx.user.telegram_id if tx.user else "",
                str(tx.amount),
                tx.payment_method.value if tx.payment_method else "",
                tx.status.value if tx.status else "",
                tx.external_reference or "",
                _fmt_dt(tx.created_at),
                _fmt_dt(tx.completed_at),
            ]
            for tx in transactions
        ]

    header = [
        "id", "telegram_id", "amount", "payment_method", "status",
        "external_reference", "created_at", "completed_at",
    ]
    return _write_csv("transactions_export", header, rows)


EXPORT_BUILDERS = {
    "orders": (_export_orders_sync, "orders.csv", "Orders"),
    "users": (_export_users_sync, "users.csv", "Users"),
    "transactions": (_export_transactions_sync, "transactions.csv", "Transactions"),
}


async def admin_export_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the 📤 Export Data screen."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.edit_message_text(
        "📤 Export Data\n\n"
        f"Download store data as CSV (most recent {EXPORT_ROW_LIMIT:,} rows).\n\n"
        "Choose a dataset:",
        reply_markup=create_admin_export_menu_keyboard()
    )


async def admin_export_csv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Build the requested CSV in a thread and send it as a document.

    Callback data: admin_export_<orders|users|transactions>
    """
    import asyncio

    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    dataset = query.data.replace("admin_export_", "", 1)
    builder = EXPORT_BUILDERS.get(dataset)
    if not builder:
        await query.answer("❌ Unknown export.", show_alert=True)
        return

    build_sync, filename, label = builder

    await query.edit_message_text(f"⏳ Generating {label} CSV...")

    file_path = None
    try:
        # Same offloading pattern as payment_handlers.check_pending_payments:
        # the blocking DB read + file write run off the event loop.
        file_path = await asyncio.to_thread(build_sync)

        with open(file_path, "rb") as fh:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=fh,
                filename=filename,
                caption=f"📤 {label} export"
            )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Export failed: {e}"
        )
    finally:
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📤 Export Data\n\nChoose a dataset:",
        reply_markup=create_admin_export_menu_keyboard()
    )
