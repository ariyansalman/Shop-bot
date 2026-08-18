"""Inline keyboard utilities for the Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config.settings import settings
from utils.i18n import LANGUAGES, t


def create_main_menu_keyboard(lang: str = "en"):
    """Create the main menu keyboard for users."""
    keyboard = [
        [InlineKeyboardButton(t("btn_products", lang), callback_data="products")],
        [
            InlineKeyboardButton(t("btn_topup", lang), callback_data="topup"),
            InlineKeyboardButton(t("btn_order_history", lang), callback_data="order_history")
        ],
        [
            InlineKeyboardButton(t("btn_availability", lang), callback_data="availability"),
            InlineKeyboardButton(t("btn_reviews", lang), callback_data="reviews")
        ],
        [
            InlineKeyboardButton(t("btn_support", lang), callback_data="support"),
            InlineKeyboardButton(t("btn_faq", lang), callback_data="faq")
        ],
        [InlineKeyboardButton(t("btn_referral", lang), callback_data="referral")],
        [InlineKeyboardButton(t("btn_language", lang), callback_data="language")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_language_keyboard(current_lang: str = "en"):
    """Create the language picker keyboard, checkmarking the active language."""
    keyboard = []
    for code, native_name, flag in LANGUAGES:
        label = f"{native_name} {flag}"
        if code == current_lang:
            label = f"✅ {label}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"lang_{code}")])

    keyboard.append([InlineKeyboardButton(t("btn_back", current_lang), callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_back_support_keyboard(lang: str = "en"):
    """Create standard back and support buttons."""
    keyboard = [
        [
            InlineKeyboardButton(t("btn_back", lang), callback_data="back"),
            InlineKeyboardButton(t("btn_support", lang), callback_data="support")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_pagination_keyboard(items, page, total_pages, callback_prefix, back_button=True, lang: str = "en"):
    """Create a paginated keyboard with items."""
    keyboard = []

    # Add item buttons - items should already be a list of button rows
    keyboard.extend(items)

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton(t("btn_previous", lang), callback_data=f"{callback_prefix}_page_{page-1}"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton(t("btn_next", lang), callback_data=f"{callback_prefix}_page_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    # Add back and support buttons
    if back_button:
        keyboard.append([
            InlineKeyboardButton(t("btn_back", lang), callback_data="back"),
            InlineKeyboardButton(t("btn_support", lang), callback_data="support")
        ])

    return InlineKeyboardMarkup(keyboard)


def create_product_detail_keyboard(product_id, back_callback="back", lang: str = "en"):
    """Create keyboard for product details view with Buy Now button."""
    keyboard = [
        [InlineKeyboardButton(t("btn_buy_now", lang), callback_data=f"buy_{product_id}")],
        [
            InlineKeyboardButton(t("btn_back", lang), callback_data=back_callback),
            InlineKeyboardButton(t("btn_support", lang), callback_data="support")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_quantity_keyboard(product_id):
    """Create keyboard for quantity confirmation."""
    keyboard = [
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_cancel_keyboard():
    """Create a simple cancel button keyboard."""
    keyboard = [[InlineKeyboardButton("☎️ Cancel", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)


def create_payment_method_keyboard():
    """Create payment method selection keyboard."""
    from config.settings import settings as app_settings

    keyboard = [
        [InlineKeyboardButton("🪙 CryptoBot", callback_data="pay_crypto")],
        [InlineKeyboardButton("💳 Card", callback_data="pay_card")],
    ]
    if app_settings.BINANCE_API_KEY and app_settings.BINANCE_PAY_ID:
        keyboard.append([InlineKeyboardButton("🟡 Binance Pay", callback_data="pay_binance")])
    if app_settings.BYBIT_API_KEY and app_settings.BYBIT_UID:
        keyboard.append([InlineKeyboardButton("⚫ Bybit Pay", callback_data="pay_bybit")])
    if app_settings.ZINIPAY_API_KEY and (
        app_settings.ZINIPAY_BKASH_NUMBER
        or app_settings.ZINIPAY_NAGAD_NUMBER
        or app_settings.ZINIPAY_ROCKET_NUMBER
    ):
        keyboard.append([InlineKeyboardButton("📱 bKash / Nagad / Rocket", callback_data="pay_bkash_nagad")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def create_reference_cancel_keyboard():
    """Cancel button shown while waiting for a buyer to submit a payment reference."""
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)


def create_support_keyboard(support_username, channel_username, lang: str = "en"):
    """Create support page keyboard with contact and community links."""
    keyboard = []

    if support_username:
        keyboard.append([InlineKeyboardButton(t("btn_contact_support", lang), url=f"https://t.me/{support_username}")])

    if channel_username:
        keyboard.append([InlineKeyboardButton(t("btn_join_community", lang), url=f"https://t.me/{channel_username}")])

    keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="main_menu")])

    return InlineKeyboardMarkup(keyboard)


def create_reviews_keyboard(page, total_pages, lang: str = "en"):
    """Create review pagination controls with a full-width back button."""
    keyboard = [[InlineKeyboardButton(t("btn_write_review", lang), callback_data="review_start")]]
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(
                InlineKeyboardButton(
                    t("btn_previous", lang),
                    callback_data=f"reviews_page_{page - 1}"
                )
            )
        if page < total_pages - 1:
            pagination_row.append(
                InlineKeyboardButton(
                    t("btn_next", lang),
                    callback_data=f"reviews_page_{page + 1}"
                )
            )
        if pagination_row:
            keyboard.append(pagination_row)

    keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="back")])
    return InlineKeyboardMarkup(keyboard)


def create_review_orders_keyboard(orders, lang: str = "en"):
    """Create an order picker for customers who want to submit a review."""
    keyboard = []
    for order in orders:
        date = order.created_at.strftime("%Y-%m-%d")
        keyboard.append([
            InlineKeyboardButton(
                f"⭐ Order #{order.id} · {date}",
                callback_data=f"review_order_{order.id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(t("btn_back_to_reviews", lang), callback_data="reviews")
    ])
    return InlineKeyboardMarkup(keyboard)


def create_review_rating_keyboard(order_id, lang: str = "en"):
    """Create the five-star picker shown after a completed order."""
    keyboard = [[
        InlineKeyboardButton(f"{rating} ⭐", callback_data=f"review_rating_{order_id}_{rating}")
        for rating in range(1, 6)
    ]]
    keyboard.append([
        InlineKeyboardButton(t("btn_back_to_orders", lang), callback_data="order_history")
    ])
    return InlineKeyboardMarkup(keyboard)


def create_admin_main_menu_keyboard(is_owner_user: bool = False):
    """Create admin panel main menu keyboard.

    Owner-only entries are hidden for STAFF admins. Hiding is cosmetic —
    every owner-only handler re-checks is_owner() server-side.
    """
    keyboard = [
        [InlineKeyboardButton("📦 Product Management", callback_data="admin_products")],
        [InlineKeyboardButton("👥 User Management", callback_data="admin_users")],
        [InlineKeyboardButton("🛍 Order Management", callback_data="admin_orders")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("⚙️ Store Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎟️ Discount Codes", callback_data="admin_discounts")],
        [InlineKeyboardButton("🤝 Referral Management", callback_data="admin_referrals")],
        [InlineKeyboardButton("📋 Audit Log", callback_data="admin_audit_log")],
        [InlineKeyboardButton("📤 Export Data", callback_data="admin_export_menu")],
    ]
    if is_owner_user:
        keyboard.append([InlineKeyboardButton("👥 Manage Admins", callback_data="admin_manage_admins")])
    keyboard.append([InlineKeyboardButton("🔙 Exit Admin", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_manage_admins_keyboard(admins, current_owner_telegram_id: int):
    """Keyboard listing admins with remove buttons, plus an add button."""
    keyboard = []
    for admin in admins:
        label = f"@{admin['username']}" if admin.get('username') else str(admin['telegram_id'])
        role = admin['role']
        if admin['telegram_id'] == current_owner_telegram_id:
            keyboard.append([InlineKeyboardButton(f"👑 {label} ({role}) — you", callback_data="noop_admin")])
        else:
            keyboard.append([
                InlineKeyboardButton(f"{'👑' if role == 'OWNER' else '🛠'} {label} ({role})", callback_data="noop_admin"),
                InlineKeyboardButton("🗑", callback_data=f"admin_remove_admin_{admin['id']}"),
            ])
    keyboard.append([InlineKeyboardButton("➕ Add Admin", callback_data="admin_add_admin")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_admin_product_menu_keyboard():
    """Create admin product management menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("➕ Create Product", callback_data="admin_create_product")],
        [InlineKeyboardButton("✏️ Edit Product", callback_data="admin_edit_product")],
        [InlineKeyboardButton("🔄 Restock Keys", callback_data="admin_restock_keys")],
        [InlineKeyboardButton("⚠️ Low Stock", callback_data="admin_low_stock")],
        [InlineKeyboardButton("📁 Manage Categories", callback_data="admin_manage_categories")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_category_menu_keyboard():
    """Create admin category management menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("➕ Create Category", callback_data="admin_create_category")],
        [InlineKeyboardButton("➕ Create Subcategory", callback_data="admin_create_subcategory")],
        [InlineKeyboardButton("✏️ Edit Category", callback_data="admin_edit_category")],
        [InlineKeyboardButton("✏️ Edit Subcategory", callback_data="admin_edit_subcategory")],
        [InlineKeyboardButton("📋 View Categories", callback_data="admin_view_categories")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_products")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_user_menu_keyboard():
    """Create admin user management menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("👁 View Users", callback_data="admin_view_users")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_order_menu_keyboard():
    """Create admin order management menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("📋 View All Orders", callback_data="admin_view_orders")],
        [InlineKeyboardButton("🚨 View Disputes", callback_data="admin_view_disputes")],
        [InlineKeyboardButton("✅ Manual Confirmation", callback_data="admin_confirm_order")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data="admin_cancel_order")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_settings_menu_keyboard():
    """Create admin store settings menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("💬 Welcome Message", callback_data="admin_welcome_msg")],
        [InlineKeyboardButton("🖼 Store Logo", callback_data="admin_store_logo")],
        [InlineKeyboardButton("📞 Support Username", callback_data="admin_support_username")],
        [InlineKeyboardButton("📢 Channel Username", callback_data="admin_channel_username")],
        [InlineKeyboardButton("❓ FAQ Text", callback_data="admin_faq_text")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_broadcast_menu_keyboard():
    """Create admin broadcast menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("💬 Text Only Broadcast", callback_data="admin_broadcast_text")],
        [InlineKeyboardButton("🖼 Image + Text Broadcast", callback_data="admin_broadcast_image")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_admin_discount_menu_keyboard():
    """Create admin discount code management menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("➕ Create Discount Code", callback_data="admin_create_discount")],
        [InlineKeyboardButton("📋 List Discount Codes", callback_data="admin_view_discounts")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)
