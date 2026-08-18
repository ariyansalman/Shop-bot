"""User-facing command and callback handlers."""

import os
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    get_db_session, User, Category, Subcategory, Product, Order, OrderItem,
    Review, Settings, ProductType, OrderStatus, DisputeStatus
)
from services import referral as referral_service
from utils import (
    get_or_create_user, format_price, format_datetime, create_main_menu_keyboard,
    create_pagination_keyboard, create_product_detail_keyboard,
    create_support_keyboard, check_user_banned,
    paginate_items, format_product_display, build_availability_text,
    create_back_support_keyboard, create_language_keyboard,
    create_reviews_keyboard, create_review_orders_keyboard, create_review_rating_keyboard,
    get_user_language, set_user_language, t, build_receipt_text
)
from utils.i18n import LANGUAGE_NAMES, normalize_language
from database.models import DEFAULT_FAQ_TEXT
from config.settings import settings as app_settings


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show welcome message with wallet balance."""
    user = update.effective_user
    telegram_id = user.id
    username = user.username

    # Check if user is banned
    if check_user_banned(telegram_id):
        lang = get_user_language(telegram_id)
        await update.message.reply_text(t("banned_message", lang))
        return

    # Get or create user and fetch settings in same session
    with get_db_session() as session:
        # Get or create user
        db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
        is_new_user = db_user is None
        if not db_user:
            db_user = User(telegram_id=telegram_id, username=username)
            session.add(db_user)
            session.commit()
            session.refresh(db_user)

        wallet_balance = db_user.wallet_balance
        lang = normalize_language(db_user.language_code)

        # Get store settings
        store_settings = session.query(Settings).first()
        welcome_msg = store_settings.welcome_message if store_settings else "Welcome to our Digital Products Store!"
        logo_path = store_settings.store_logo_path if store_settings else None

    # Send logo if available
    if logo_path and os.path.exists(logo_path):
        with open(logo_path, 'rb') as logo:
            await update.message.reply_photo(photo=logo)

    # Referral deep link: /start ref_<referrer_telegram_id>
    # Only a brand-new user can be attached, and only once — see
    # services/referral.attach_referral() for the full rule set.
    referral_note = ""
    referrer_id = referral_service.parse_referral_payload(context.args)
    if referrer_id:
        result = referral_service.attach_referral(
            telegram_id, referrer_id, is_new_user)
        if result == referral_service.ATTACH_OK:
            referral_note = (
                "\n\n🎉 You joined through a referral link! "
                "Your friend earns a reward once you complete a qualifying purchase."
            )
        elif result == referral_service.ATTACH_SELF:
            referral_note = "\n\n⚠️ You cannot refer yourself."
        elif result in (referral_service.ATTACH_EXISTING_USER,
                        referral_service.ATTACH_ALREADY_REFERRED):
            referral_note = (
                "\n\nℹ️ Referral links only work for brand-new users, "
                "so this one was not applied."
            )

    # Send welcome message with wallet balance
    message = f"{welcome_msg}\n\n{t('welcome_balance', lang, balance=format_price(wallet_balance))}{referral_note}"

    await update.message.reply_text(
        message,
        reply_markup=create_main_menu_keyboard(lang)
    )


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu callback - return to main menu."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    with get_db_session() as session:
        # Get user
        db_user = session.query(User).filter_by(telegram_id=user_id).first()
        if not db_user:
            db_user = User(telegram_id=user_id)
            session.add(db_user)
            session.commit()
            session.refresh(db_user)

        wallet_balance = db_user.wallet_balance
        lang = normalize_language(db_user.language_code)

        # Get store settings
        store_settings = session.query(Settings).first()
        welcome_msg = store_settings.welcome_message if store_settings else "Welcome to our Digital Products Store!"

    message = f"{welcome_msg}\n\n{t('welcome_balance', lang, balance=format_price(wallet_balance))}"

    await query.edit_message_text(
        message,
        reply_markup=create_main_menu_keyboard(lang)
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle language button - show the language picker."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)
    current_name = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["en"])

    message = f"{t('select_language_title', lang)}\n\n{t('current_language_label', lang, lang=current_name)}"

    await query.edit_message_text(
        message,
        reply_markup=create_language_keyboard(lang)
    )


async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a language selection - persist it and re-render the picker."""
    query = update.callback_query
    user_id = update.effective_user.id

    if check_user_banned(user_id):
        await query.answer()
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    # callback_data is "lang_<code>", e.g. "lang_fr"
    selected_code = query.data.split("_", 1)[1]
    selected_code = normalize_language(selected_code)

    set_user_language(user_id, selected_code)

    # A callback query can only be answered once, so this single answer()
    # carries the confirmation toast instead of an earlier blank one.
    current_name = LANGUAGE_NAMES.get(selected_code, LANGUAGE_NAMES["en"])
    await query.answer(t("language_updated", selected_code, lang=current_name), show_alert=False)

    message = f"{t('select_language_title', selected_code)}\n\n{t('current_language_label', selected_code, lang=current_name)}"

    await query.edit_message_text(
        message,
        reply_markup=create_language_keyboard(selected_code)
    )


async def products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle products button - show category list."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    # If coming from a photo message, delete it and create new text message
    if query.message.photo:
        await query.message.delete()
        message = await query.message.reply_text("Loading...")

        # Create mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        query = MockQuery(message)

    # Extract page number from callback data
    callback_data = query.data if hasattr(query, 'data') else "products"
    page = 0
    if "_page_" in callback_data:
        page = int(callback_data.split("_page_")[1])

    with get_db_session() as session:
        categories = session.query(Category).all()

        if not categories:
            await query.edit_message_text(
                t("no_categories", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

        # Paginate categories
        page_info = paginate_items(categories, page, page_size=5)

        # Create category buttons
        category_buttons = [
            [InlineKeyboardButton(cat.name, callback_data=f"category_{cat.id}")]
            for cat in page_info['items']
        ]

        keyboard = create_pagination_keyboard(
            category_buttons,
            page_info['page'],
            page_info['total_pages'],
            "products",
            lang=lang
        )

        text = t("select_category", lang)
        if page_info['total_pages'] > 1:
            text += t("page_indicator", lang, current=page_info['page'] + 1, total=page_info['total_pages'])

        await query.edit_message_text(text, reply_markup=keyboard)


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product callback."""
    await product_detail_callback(update, context)


async def subcategory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subcategory selection."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)
    subcategory_id = int(query.data.split("_")[1])

    # If coming from a photo message (product detail with image), delete and send new message
    if query.message.photo:
        await query.message.delete()
        # Create a new text message for products list
        message = await query.message.reply_text("Loading products...")
        # Now we need to pass this message to show_products_list
        # We'll use a workaround by creating a mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        mock_query = MockQuery(message)
        await show_products_list(mock_query, subcategory_id=subcategory_id, context=context, lang=lang)
    else:
        await show_products_list(query, subcategory_id=subcategory_id, context=context, lang=lang)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection - show subcategories or products."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)
    callback_data = query.data
    category_id = int(callback_data.split("_")[1])

    # If coming from a photo message, delete it and create new text message
    if query.message.photo:
        await query.message.delete()
        message = await query.message.reply_text("Loading...")

        # Create mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        query = MockQuery(message)

    with get_db_session() as session:
        category = session.query(Category).filter_by(id=category_id).first()

        if not category:
            await query.edit_message_text(t("category_not_found", lang))
            return

        # Check if category has subcategories
        subcategories = session.query(Subcategory).filter_by(category_id=category_id).all()

        if subcategories:
            # Show subcategories
            subcat_buttons = [
                [InlineKeyboardButton(subcat.name, callback_data=f"subcategory_{subcat.id}")]
                for subcat in subcategories[:5]
            ]

            # Create keyboard with back to products
            from telegram import InlineKeyboardMarkup
            keyboard = subcat_buttons + [[
                InlineKeyboardButton(t("btn_back", lang), callback_data="back_to_products"),
                InlineKeyboardButton(t("btn_support", lang), callback_data="support")
            ]]

            await query.edit_message_text(
                t("select_subcategory_product", lang, category=category.name),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Show products directly
            await show_products_list(query, category_id=category_id, context=context, lang=lang)


async def show_products_list(query, category_id=None, subcategory_id=None, page=0, context=None, lang="en"):
    """Show list of products for a category or subcategory."""
    with get_db_session() as session:
        query_filter = Product.is_active == True

        if category_id:
            products = session.query(Product).filter(
                Product.category_id == category_id,
                Product.subcategory_id == None,
                query_filter
            ).all()
        elif subcategory_id:
            products = session.query(Product).filter(
                Product.subcategory_id == subcategory_id,
                query_filter
            ).all()
        else:
            products = session.query(Product).filter(query_filter).all()

        if not products:
            await query.edit_message_text(
                t("no_products_category", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

        # Paginate products
        page_info = paginate_items(products, page, page_size=5)

        # Create product buttons
        product_buttons = [
            [InlineKeyboardButton(
                f"{prod.name} | {format_price(prod.price)} | {t('available_label', lang)}: {prod.stock_count}",
                callback_data=f"product_{prod.id}"
            )]
            for prod in page_info['items']
        ]

        # Add pagination if needed
        from telegram import InlineKeyboardMarkup
        keyboard = product_buttons.copy()
        if page_info['total_pages'] > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton(t("btn_previous", lang), callback_data=f"products_page_{page-1}"))
            if page < page_info['total_pages'] - 1:
                pagination_row.append(InlineKeyboardButton(t("btn_next", lang), callback_data=f"products_page_{page+1}"))
            if pagination_row:
                keyboard.append(pagination_row)

        # Determine back button based on what we're showing
        if subcategory_id:
            # Get the subcategory to find its parent category
            subcategory = session.query(Subcategory).filter_by(id=subcategory_id).first()
            if subcategory and subcategory.category_id:
                # Back to category (which will show subcategories)
                back_data = f"category_{subcategory.category_id}"
            else:
                back_data = "back_to_products"
        elif category_id:
            # Back to products (category list)
            back_data = "back_to_products"
        else:
            back_data = "back_to_products"

        keyboard.append([
            InlineKeyboardButton(t("btn_back", lang), callback_data=back_data),
            InlineKeyboardButton(t("btn_support", lang), callback_data="support")
        ])

        text = t("select_product", lang)
        if page_info['total_pages'] > 1:
            text += t("page_indicator", lang, current=page_info['page'] + 1, total=page_info['total_pages'])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection - show product details."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)
    product_id = int(query.data.split("_")[1])

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await query.edit_message_text(t("product_not_found", lang))
            return

        # Determine back navigation based on product's category/subcategory
        if product.subcategory_id:
            # Product belongs to a subcategory - go back to subcategory list
            back_callback = f"subcategory_{product.subcategory_id}"
        elif product.category_id:
            # Product belongs to a category - go back to category
            back_callback = f"category_{product.category_id}"
        else:
            # Fallback to products list
            back_callback = "back_to_products"

        # Format product details
        details = format_product_display(product, include_description=True, lang=lang)

        # Send product image if available
        if product.image_path and os.path.exists(product.image_path):
            with open(product.image_path, 'rb') as image:
                await query.message.reply_photo(
                    photo=image,
                    caption=details,
                    reply_markup=create_product_detail_keyboard(product_id, back_callback, lang=lang)
                )
            await query.message.delete()
        else:
            await query.edit_message_text(
                details,
                reply_markup=create_product_detail_keyboard(product_id, back_callback, lang=lang)
            )


async def availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle availability button - show all available products."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    with get_db_session() as session:
        categories = session.query(Category).all()
        products_by_category = {}

        for category in categories:
            products = session.query(Product).filter_by(
                category_id=category.id,
                is_active=True
            ).limit(15).all()

            if products:
                products_by_category[category.name] = products

        if not products_by_category:
            await query.edit_message_text(
                t("no_products_available", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

        text = build_availability_text(products_by_category, lang=lang)

        await query.edit_message_text(
            text,
            reply_markup=create_back_support_keyboard(lang)
        )


def _masked_reviewer(username: str | None) -> str:
    """Hide most of a Telegram username while keeping the last two characters."""
    clean_username = (username or "").strip().lstrip("@")
    if not clean_username:
        return "@anonymous"
    if len(clean_username) <= 2:
        return "@" + ("*" * len(clean_username))
    return "@" + ("*" * (len(clean_username) - 2)) + clean_username[-2:]


def _review_stars(rating: int) -> str:
    """Render a compact star rating like the store screenshot."""
    return "⭐" * max(0, min(5, rating))


async def reviews_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show published store reviews with average rating and pagination."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)
    callback_data = query.data or "reviews"
    page = 0
    if "_page_" in callback_data:
        try:
            page = max(0, int(callback_data.rsplit("_page_", 1)[1]))
        except ValueError:
            page = 0

    with get_db_session() as session:
        reviews = (
            session.query(Review)
            .filter_by(is_published=True)
            .order_by(Review.created_at.desc())
            .all()
        )

        if not reviews:
            await query.edit_message_text(
                t("no_reviews", lang),
                reply_markup=create_reviews_keyboard(0, 1, lang)
            )
            return

        page_size = 5
        total_pages = (len(reviews) + page_size - 1) // page_size
        page = min(page, total_pages - 1)
        page_reviews = reviews[page * page_size:(page + 1) * page_size]
        average = sum(review.rating for review in reviews) / len(reviews)
        average_stars = _review_stars(round(average))

        lines = [
            t(
                "review_summary",
                lang,
                stars=average_stars,
                average=f"{average:.1f}",
                count=len(reviews)
            ),
            ""
        ]
        for review in page_reviews:
            lines.append(
                t(
                    "review_item",
                    lang,
                    stars=_review_stars(review.rating),
                    rating=review.rating,
                    username=_masked_reviewer(review.reviewer_username),
                    date=review.created_at.strftime("%Y-%m-%d")
                )
            )

        if total_pages > 1:
            lines.extend(["", t("page_indicator", lang, current=page + 1, total=total_pages).strip()])

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=create_reviews_keyboard(page, total_pages, lang)
        )


async def review_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the star picker for a completed order."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    if check_user_banned(user_id):
        await query.edit_message_text(t("banned_message", lang))
        return

    try:
        order_id = int(query.data.rsplit("_", 1)[1])
    except (AttributeError, ValueError):
        await query.edit_message_text(t("order_not_found", lang))
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        order = session.query(Order).filter_by(id=order_id, user_id=user.id if user else None).first()
        if not order or order.status != OrderStatus.COMPLETED:
            await query.edit_message_text(
                t("review_order_unavailable", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return
        if session.query(Review).filter_by(order_id=order.id).first():
            await query.edit_message_text(
                t("review_already_submitted", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

    await query.edit_message_text(
        t("review_prompt", lang),
        reply_markup=create_review_rating_keyboard(order_id, lang)
    )


async def review_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a customer's completed orders that still need a review."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    if check_user_banned(user_id):
        await query.edit_message_text(t("banned_message", lang))
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text(
                t("no_reviewable_orders", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

        completed_orders = (
            session.query(Order)
            .filter_by(user_id=user.id, status=OrderStatus.COMPLETED)
            .order_by(Order.created_at.desc())
            .all()
        )
        reviewable_orders = [
            order for order in completed_orders
            if session.query(Review).filter_by(order_id=order.id).first() is None
        ]

    if not reviewable_orders:
        await query.edit_message_text(
            t("no_reviewable_orders", lang),
            reply_markup=create_back_support_keyboard(lang)
        )
        return

    await query.edit_message_text(
        t("select_review_order", lang),
        reply_markup=create_review_orders_keyboard(reviewable_orders, lang)
    )


async def review_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save one rating per completed order."""
    query = update.callback_query
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    if check_user_banned(user_id):
        await query.answer()
        await query.edit_message_text(t("banned_message", lang))
        return

    try:
        _prefix, _label, order_id_text, rating_text = query.data.split("_")
        order_id = int(order_id_text)
        rating = int(rating_text)
    except (AttributeError, ValueError):
        await query.answer(t("review_invalid", lang), show_alert=True)
        return

    if rating not in range(1, 6):
        await query.answer(t("review_invalid", lang), show_alert=True)
        return

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        order = session.query(Order).filter_by(id=order_id, user_id=user.id if user else None).first()
        if not order or order.status != OrderStatus.COMPLETED:
            await query.answer(t("review_order_unavailable", lang), show_alert=True)
            return

        existing_review = session.query(Review).filter_by(order_id=order.id).first()
        if existing_review:
            await query.answer(t("review_already_submitted", lang), show_alert=True)
            return

        session.add(Review(
            user_id=user.id,
            order_id=order.id,
            rating=rating,
            reviewer_username=update.effective_user.username,
        ))

    await query.answer(t("review_submitted", lang), show_alert=False)
    await query.edit_message_text(
        t("review_submitted_message", lang, stars=_review_stars(rating)),
        reply_markup=create_back_support_keyboard(lang)
    )


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support button - show support page."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    with get_db_session() as session:
        store_settings = session.query(Settings).first()

        support_username = store_settings.support_username if store_settings else ""
        channel_username = store_settings.channel_username if store_settings else ""

        message = t("support_message", lang)

        await query.edit_message_text(
            message,
            reply_markup=create_support_keyboard(support_username, channel_username, lang=lang)
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - short friendly overview of how to use the store."""
    telegram_id = update.effective_user.id

    if check_user_banned(telegram_id):
        lang = get_user_language(telegram_id)
        await update.message.reply_text(t("banned_message", lang))
        return

    lang = get_user_language(telegram_id)

    with get_db_session() as session:
        store_settings = session.query(Settings).first()
        support_username = store_settings.support_username if store_settings else ""
        channel_username = store_settings.channel_username if store_settings else ""

    support_line = (
        f"Need a hand? Message @{support_username} any time."
        if support_username
        else "Need a hand? Use the ☎️ Support button in the main menu."
    )
    channel_line = (
        f"\n📢 News and restocks are posted in @{channel_username}."
        if channel_username
        else ""
    )

    message = (
        "🤖 How to use this store\n\n"
        "🛒 Browse products — tap \"Products\" in the main menu to explore categories "
        "and open a product for its price, stock and description.\n\n"
        "💰 Top up your wallet — tap \"Top Up\", pick an amount and a payment method. "
        "Your balance is credited once the payment is verified.\n\n"
        "🧾 Buy — open a product, tap \"Buy Now\" and confirm. The price is taken from "
        "your wallet balance and the item is delivered right here in chat. Past "
        "purchases live under \"Order History\".\n\n"
        "❓ Questions — the \"FAQ\" button answers the most common ones.\n\n"
        f"☎️ {support_line}{channel_line}"
    )

    await update.message.reply_text(
        message,
        reply_markup=create_support_keyboard(support_username, channel_username, lang=lang)
    )


async def faq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle FAQ button - show the admin-editable FAQ text."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    with get_db_session() as session:
        store_settings = session.query(Settings).first()
        faq_text = (store_settings.faq_text if store_settings and store_settings.faq_text else DEFAULT_FAQ_TEXT)
        support_username = store_settings.support_username if store_settings else ""
        channel_username = store_settings.channel_username if store_settings else ""

    await query.edit_message_text(
        faq_text,
        reply_markup=create_support_keyboard(support_username, channel_username, lang=lang)
    )


async def order_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order history button - show user's order history as clickable list."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

        if not user:
            await query.edit_message_text(t("user_not_found", lang))
            return

        orders = session.query(Order).filter_by(user_id=user.id).order_by(Order.created_at.desc()).limit(10).all()

        if not orders:
            await query.edit_message_text(
                t("no_orders", lang),
                reply_markup=create_back_support_keyboard(lang)
            )
            return

        # Build keyboard with order buttons
        keyboard = []
        for order in orders:
            status_emoji = {
                OrderStatus.PROCESSING: "⏳",
                OrderStatus.COMPLETED: "✅",
                OrderStatus.CANCELLED: "❌"
            }.get(order.status, "❓")

            dispute_indicator = ""
            if order.dispute_status == DisputeStatus.OPENED:
                dispute_indicator = " 🚨"
            elif order.dispute_status == DisputeStatus.RESOLVED:
                dispute_indicator = " ✔️"

            button_text = f"{status_emoji} Order #{order.id} | {format_price(order.total_amount)}{dispute_indicator}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"user_order_detail_{order.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton(t("btn_back_to_menu", lang), callback_data="main_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            t("order_history_title", lang),
            reply_markup=reply_markup
        )


async def user_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order detail view with dispute button for user."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    # Extract order_id from callback data
    order_id = int(query.data.split("_")[3])

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text(t("user_not_found", lang))
            return

        order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order:
            await query.edit_message_text(t("order_not_found", lang))
            return

        order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

        # Build order details message
        items_text = ""
        for item in order_items:
            items_text += f"  📦 {item.product.name} (x{item.quantity}) - {format_price(item.price * item.quantity)}\n"

            # Add delivered assets (keys or download links)
            if item.delivered_asset:
                if item.product.product_type == ProductType.KEY:
                    items_text += f"  {t('order_keys_label', lang)}\n{item.delivered_asset}\n"
                elif item.product.product_type == ProductType.FILE:
                    items_text += f"  {t('order_download_label', lang, link=item.delivered_asset)}\n"
            items_text += "\n"

        status_emoji = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED: "✅",
            OrderStatus.CANCELLED: "❌"
        }.get(order.status, "❓")

        status_translation_key = {
            OrderStatus.PROCESSING: "order_status_processing",
            OrderStatus.COMPLETED: "order_status_completed",
            OrderStatus.CANCELLED: "order_status_cancelled"
        }.get(order.status)
        status_label = t(status_translation_key, lang) if status_translation_key else order.status.value

        dispute_text = ""
        if order.dispute_status == DisputeStatus.OPENED:
            dispute_text = "\n" + t("dispute_open", lang)
        elif order.dispute_status == DisputeStatus.RESOLVED:
            dispute_text = "\n" + t("dispute_resolved", lang)

        message = "{title}\n\n{number}\n{status_emoji} {status}\n{total}\n{date}\n\n{items_label}\n{items_text}{dispute_text}".format(
            title=t("order_detail_title", lang),
            number=t("order_number", lang, id=order.id),
            status_emoji=status_emoji,
            status=t("order_status_label", lang, status=status_label),
            total=t("order_total", lang, amount=format_price(order.total_amount)),
            date=t("order_date", lang, date=format_datetime(order.created_at)),
            items_label=t("order_items_label", lang),
            items_text=items_text,
            dispute_text=dispute_text
        )

        # Build keyboard based on order status
        keyboard = []

        # Completed orders already have everything a receipt needs
        # (delivered assets, final total) — let the buyer pull it up
        # reformatted instead of re-parsing the original chat message.
        if order.status == OrderStatus.COMPLETED:
            keyboard.append([
                InlineKeyboardButton(
                    t("btn_view_receipt", lang),
                    callback_data=f"view_receipt_{order.id}"
                )
            ])

        # Let a buyer rate each completed order once.
        if order.status == OrderStatus.COMPLETED and not session.query(Review).filter_by(order_id=order.id).first():
            keyboard.append([
                InlineKeyboardButton(
                    t("btn_leave_review", lang),
                    callback_data=f"review_order_{order.id}"
                )
            ])

        # Add dispute button if no dispute is open/resolved
        if order.dispute_status == DisputeStatus.NIL:
            keyboard.append([InlineKeyboardButton(t("btn_open_dispute", lang), callback_data=f"open_dispute_{order.id}")])

        # Add back button
        keyboard.append([InlineKeyboardButton(t("btn_back_to_orders", lang), callback_data="order_history")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup)


async def view_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a formatted receipt for one of the buyer's own completed orders.

    Reuses build_receipt_text() — the same helper that renders the
    post-purchase confirmation in payment_handlers.confirm_purchase() — so
    an old order's receipt looks identical to the one shown at checkout,
    just rebuilt from what's stored rather than re-parsed from that chat
    message.
    """
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if check_user_banned(user_id):
        lang = get_user_language(user_id)
        await query.edit_message_text(t("banned_message", lang))
        return

    lang = get_user_language(user_id)

    # Extract order_id from callback data (format: view_receipt_123)
    order_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text(t("user_not_found", lang))
            return

        # Scoped to this buyer's own orders, same as the detail view above.
        order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
        if not order or order.status != OrderStatus.COMPLETED:
            await query.edit_message_text(t("order_not_found", lang))
            return

        order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

        receipt_text = build_receipt_text(order, order_items, wallet_balance=user.wallet_balance, lang=lang)

        keyboard = [[InlineKeyboardButton(
            t("btn_back", lang), callback_data=f"user_order_detail_{order.id}"
        )]]

        await query.edit_message_text(receipt_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def back_to_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back to products - show category list."""
    # Just redirect to products_callback
    await products_callback(update, context)
