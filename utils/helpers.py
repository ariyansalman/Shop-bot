"""Helper utility functions for the Telegram bot."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes, filters
from config.settings import settings
from database import get_db_session, User, Admin, AdminRole, AdminActionLog, ProductType

# In-memory cache for ban status (telegram_id: (is_banned, timestamp))
_ban_cache = {}
_BAN_CACHE_TTL = 30  # Cache ban status for 30 seconds

# In-memory cache for admin role (telegram_id: (role_or_None, timestamp)).
# Same 30s TTL pattern as the ban cache above: is_admin() runs on essentially
# every admin interaction, so this keeps it from being a DB hit each time.
_admin_cache = {}
_ADMIN_CACHE_TTL = 30


def get_admin_role(user_id: int):
    """Return the AdminRole for a Telegram ID, or None if not an admin.

    Backward compatibility: the .env ADMIN_TELEGRAM_ID is always treated as
    OWNER, even if the DB lookup fails (e.g. table not migrated yet), so a
    deployment can never lock its owner out of the panel.
    """
    global _admin_cache

    if user_id in _admin_cache:
        cached_value, cached_time = _admin_cache[user_id]
        if (datetime.utcnow() - cached_time).total_seconds() < _ADMIN_CACHE_TTL:
            return cached_value

    role = None
    try:
        with get_db_session() as session:
            role = session.query(Admin.role).filter_by(telegram_id=user_id).scalar()
    except Exception as e:  # pragma: no cover - defensive, DB may be unavailable
        print(f"Error loading admin role: {e}")

    if role is None and settings.ADMIN_TELEGRAM_ID and user_id == settings.ADMIN_TELEGRAM_ID:
        role = AdminRole.OWNER

    _admin_cache[user_id] = (role, datetime.utcnow())
    return role


def is_admin(user_id: int) -> bool:
    """Check if a user is an admin (OWNER or STAFF)."""
    return get_admin_role(user_id) is not None


def is_owner(user_id: int) -> bool:
    """Check if a user is an OWNER (full permissions)."""
    return get_admin_role(user_id) == AdminRole.OWNER


def clear_admin_cache(telegram_id: int = None):
    """Clear cached admin roles (called whenever admins are added/removed)."""
    global _admin_cache
    if telegram_id is None:
        _admin_cache.clear()
    elif telegram_id in _admin_cache:
        del _admin_cache[telegram_id]


# ---------------------------------------------------------------------------
# Simple in-memory per-user rate limiting
# ---------------------------------------------------------------------------
# Same style as _ban_cache / _admin_cache above: a plain module-level dict,
# {telegram_id: [timestamps]}, pruned lazily whenever that user is checked.
# No extra dependency needed at this scale.
#
# LIMITATION: this state lives in the process' memory. If the bot is ever run
# across multiple worker processes/dynos, each worker keeps its own counters,
# so the effective limit becomes (max_calls x number of workers) and is not
# consistent across processes. For a single-worker deployment (which is what
# this bot assumes) that's fine; a shared store such as Redis would be needed
# for multi-worker correctness — deliberately not added here.
_rate_limit_calls = {}

# Default limits, tunable per-handler via the decorator arguments.
RATE_LIMIT_MAX_CALLS = 5
RATE_LIMIT_PER_SECONDS = 60


def _rate_limit_check(key, max_calls: int, per_seconds: int):
    """Record a call for `key` and return (allowed, seconds_until_retry)."""
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=per_seconds)

    timestamps = [ts for ts in _rate_limit_calls.get(key, []) if ts > window_start]

    if len(timestamps) >= max_calls:
        _rate_limit_calls[key] = timestamps
        retry_after = int((timestamps[0] - window_start).total_seconds()) + 1
        return False, max(retry_after, 1)

    timestamps.append(now)
    _rate_limit_calls[key] = timestamps
    return True, 0


def clear_rate_limits(telegram_id: int = None):
    """Clear rate-limit counters (all users, or one user)."""
    if telegram_id is None:
        _rate_limit_calls.clear()
        return
    for key in [k for k in _rate_limit_calls if k[0] == telegram_id]:
        del _rate_limit_calls[key]


def rate_limited(max_calls: int = RATE_LIMIT_MAX_CALLS,
                 per_seconds: int = RATE_LIMIT_PER_SECONDS):
    """Decorator limiting a handler to `max_calls` per `per_seconds` per user.

    Used on the highest-risk user-facing entry points (top-up flow, purchase
    confirmation) to stop spamming/abuse. Admins are intentionally exempt —
    admin_only handlers are trusted and throttling them would just get in
    the way.

    On limit exceeded the user gets a visible "please slow down" reply rather
    than the update being silently dropped, and the wrapped handler returns
    None, which leaves any ConversationHandler state unchanged.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if user is None:
                return await func(update, context)

            # Never throttle admins.
            if is_admin(user.id):
                return await func(update, context)

            allowed, retry_after = _rate_limit_check(
                (user.id, func.__name__), max_calls, per_seconds
            )

            if not allowed:
                warning = (
                    "🐢 You're doing that too fast. "
                    f"Please slow down and try again in {retry_after}s."
                )
                if update.callback_query:
                    await update.callback_query.answer(warning, show_alert=True)
                elif update.message:
                    await update.message.reply_text(warning)
                return

            return await func(update, context)
        return wrapper
    return decorator


def admin_only(func):
    """Decorator to restrict handler access to admins (OWNER or STAFF)."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            await update.message.reply_text("⛔ You don't have permission to access this command.")
            return
        return await func(update, context)
    return wrapper


def owner_only(func):
    """Decorator to restrict handler access to OWNER admins only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ This action is restricted to the store owner.")
            return
        return await func(update, context)
    return wrapper


class _AdminMessageFilter(filters.MessageFilter):
    """Message filter matching any admin (replaces filters.User(ADMIN_ID))."""

    def filter(self, message) -> bool:
        return bool(message.from_user) and is_admin(message.from_user.id)


admin_filter = _AdminMessageFilter()


def log_admin_action(admin_id, action, target_type=None, target_id=None,
                     details=None, session=None):
    """Write one audit-trail row describing a state-changing admin action.

    Call this immediately AFTER the change itself has been committed, so the
    log only ever records changes that actually landed.

    admin_id       Telegram ID of the admin who performed the action.
    action         Short verb, e.g. "ban_user" / "cancel_order" / "adjust_balance".
    target_type    "user" / "order" / "product" / "category" / ... (optional).
    target_id      Primary key (or Telegram ID) of the affected row (optional).
    details        Free-form text, or any JSON-serialisable object (dict/list),
                   which is stored as compact JSON.
    session        Optional already-open session. Handlers that are still
                   inside a `with get_db_session() as session:` block should
                   pass it, because the session factory is thread-scoped and
                   opening a nested block would commit/close the caller's
                   session out from under it. When omitted, the helper opens
                   its own session with get_db_session(), exactly like every
                   other DB write in this codebase.

    Logging is best-effort: a failure here is swallowed (and printed) so an
    audit-trail problem can never break or roll back the admin action that
    already succeeded.
    """
    import json

    if details is not None and not isinstance(details, str):
        try:
            details = json.dumps(details, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            details = str(details)

    entry_kwargs = dict(
        admin_telegram_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        created_at=datetime.utcnow(),
    )

    try:
        if session is not None:
            session.add(AdminActionLog(**entry_kwargs))
            session.flush()
            return
        with get_db_session() as own_session:
            own_session.add(AdminActionLog(**entry_kwargs))
    except Exception as e:  # pragma: no cover - never break the admin action
        print(f"Failed to write admin action log ({action}): {e}")


def get_or_create_user(telegram_id: int, username: str = None):
    """Get existing user or create a new one in the database."""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()

        if not user:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            session.commit()
            session.refresh(user)

        return user


def get_user_language(telegram_id: int) -> str:
    """Return the user's saved language code, defaulting to 'en'.

    Uses .scalar() like check_user_banned() above — this is called on
    essentially every menu render, so it's worth avoiding a full User load
    for a single column.
    """
    from utils.i18n import normalize_language

    with get_db_session() as session:
        lang = session.query(User.language_code).filter_by(telegram_id=telegram_id).scalar()
        return normalize_language(lang) if lang else 'en'


def set_user_language(telegram_id: int, lang_code: str) -> None:
    """Persist a user's chosen language. Creates the user row if it doesn't exist yet."""
    from utils.i18n import normalize_language

    lang_code = normalize_language(lang_code)
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            user = User(telegram_id=telegram_id, language_code=lang_code)
            session.add(user)
        else:
            user.language_code = lang_code


def format_price(price: float) -> str:
    """Format price to standard USD format."""
    return f"${price:.2f}"


def format_datetime(dt: datetime) -> str:
    """Format datetime to readable string."""
    return dt.strftime("%b %d, %Y")


def format_datetime_full(dt: datetime) -> str:
    """Format datetime including the time, for receipts. Stored timestamps
    are UTC (models default to datetime.utcnow), so the label says so
    rather than implying a local time the bot doesn't actually know."""
    return dt.strftime("%b %d, %Y, %I:%M %p UTC")


def build_receipt_text(order, order_items, wallet_balance=None, lang: str = "en") -> str:
    """Build a formatted purchase receipt for a completed order.

    Shared by the post-purchase confirmation in
    handlers.payment_handlers.confirm_purchase() and the "View Receipt"
    button on past orders in handlers.user_handlers, so both render the
    exact same layout from the exact same data instead of drifting apart.

    wallet_balance is optional: pass the user's current balance to show it
    (e.g. right after purchase, or when looking up an old order); omit it
    if it isn't available in the caller's context.
    """
    from utils.i18n import t

    lines = [t("receipt_title", lang), "", t("order_number", lang, id=order.id),
              f"📅 {format_datetime_full(order.created_at)}", ""]

    for item in order_items:
        product_name = item.product.name if item.product else t("receipt_unknown_product", lang)
        lines.append(f"📦 {product_name}")
        lines.append(t("receipt_quantity", lang, qty=item.quantity))
        lines.append(t("receipt_unit_price", lang, price=format_price(item.price)))
        lines.append("")

    lines.append(t("order_total", lang, amount=format_price(order.total_amount)))
    if wallet_balance is not None:
        lines.append(t("receipt_wallet_balance", lang, balance=format_price(wallet_balance)))

    delivered_lines = []
    for item in order_items:
        if not item.delivered_asset:
            continue
        if item.product and item.product.product_type == ProductType.KEY:
            delivered_lines.append(t("order_keys_label", lang))
            delivered_lines.append(item.delivered_asset)
        elif item.product and item.product.product_type == ProductType.FILE:
            delivered_lines.append(t("order_download_label", lang, link=item.delivered_asset))

    if delivered_lines:
        lines.append("")
        lines.append("———————————————")
        lines.append("")
        lines.extend(delivered_lines)

    return "\n".join(lines)


def calculate_expiry_time(hours: int = 1) -> datetime:
    """Calculate expiry datetime from now."""
    return datetime.utcnow() + timedelta(hours=hours)


def paginate_items(items, page: int, page_size: int = 5):
    """Paginate a list of items."""
    start = page * page_size
    end = start + page_size
    total_pages = (len(items) + page_size - 1) // page_size

    return {
        'items': items[start:end],
        'page': page,
        'total_pages': total_pages,
        'has_next': page < total_pages - 1,
        'has_prev': page > 0
    }


def validate_amount(amount_str: str) -> tuple[bool, Decimal, str]:
    """Validate user input for payment amount.

    Parses with Decimal (not float) so the value stored/added to the wallet
    never carries binary floating-point rounding error, and rounds to cents.
    """
    try:
        amount = Decimal(amount_str.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= 0:
            return False, Decimal("0"), "Amount must be greater than zero."
        if amount > Decimal("100000"):
            return False, Decimal("0"), "Amount is too large. Maximum is $100,000."
        return True, amount, ""
    except InvalidOperation:
        return False, Decimal("0"), "Invalid amount. Please enter a valid number."


def validate_signed_amount(amount_str: str) -> tuple[bool, Decimal, str]:
    """Validate a signed amount for an admin wallet adjustment.

    Same Decimal parsing/rounding rules as validate_amount(), but a negative
    value is allowed (it means "debit this much"). Zero is still rejected —
    an adjustment that changes nothing is always a mistake.
    """
    try:
        raw = amount_str.strip().lstrip("+")
        amount = Decimal(raw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount == 0:
            return False, Decimal("0"), "Amount cannot be zero. Use a positive or negative number."
        if abs(amount) > Decimal("100000"):
            return False, Decimal("0"), "Amount is too large. Maximum is $100,000."
        return True, amount, ""
    except InvalidOperation:
        return False, Decimal("0"), "Invalid amount. Please enter a valid number (e.g. 25 or -10)."



def format_product_display(product, include_description=False, lang: str = "en") -> str:
    """Format product information for display."""
    from utils.i18n import t

    text = "{name}\n{price}\n{stock}".format(
        name=t("product_label_name", lang, name=product.name),
        price=t("product_label_price", lang, price=format_price(product.price)),
        stock=t("product_label_stock", lang, stock=product.stock_count),
    )

    if include_description and product.description:
        text += "\n" + t("product_label_description", lang, description=product.description)

    return text


def get_admin_telegram_ids() -> list:
    """Return every admin's Telegram ID (always includes the .env owner)."""
    ids = []
    try:
        with get_db_session() as session:
            ids = [row[0] for row in session.query(Admin.telegram_id).all()]
    except Exception as e:  # pragma: no cover - defensive
        print(f"Error loading admin ids: {e}")

    if settings.ADMIN_TELEGRAM_ID and settings.ADMIN_TELEGRAM_ID not in ids:
        ids.append(settings.ADMIN_TELEGRAM_ID)
    return ids


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send a notification message to every admin."""
    for admin_id in get_admin_telegram_ids():
        try:
            await context.bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            print(f"Error notifying admin {admin_id}: {e}")


def build_availability_text(products_by_category, lang: str = "en") -> str:
    """Build availability page text with products grouped by category."""
    from utils.i18n import t

    text = t("availability_header", lang) + "\n\n"
    available_label = t("available_label", lang)

    for category_name, products in products_by_category.items():
        text += f"📦━━━━━{category_name}━━━━━📦\n"
        for product in products:
            text += f"{product.name} | {format_price(product.price)} | {available_label}: {product.stock_count}\n"
        text += "\n"

    return text


def parse_keys_from_text(text: str) -> list:
    """Parse keys from text input (one key per line)."""
    keys = [line.strip() for line in text.split('\n') if line.strip()]
    return keys


def check_user_banned(telegram_id: int) -> bool:
    """Check if a user is banned (with caching for performance)."""
    global _ban_cache

    # Check cache first
    if telegram_id in _ban_cache:
        cached_value, cached_time = _ban_cache[telegram_id]
        # If cache is still valid (within TTL), return cached value
        if (datetime.utcnow() - cached_time).total_seconds() < _BAN_CACHE_TTL:
            return cached_value

    # Cache miss or expired - query database
    with get_db_session() as session:
        # Use .scalar() for better performance - only fetch is_banned column
        is_banned = session.query(User.is_banned).filter_by(telegram_id=telegram_id).scalar()
        result = bool(is_banned) if is_banned is not None else False

        # Update cache
        _ban_cache[telegram_id] = (result, datetime.utcnow())

        return result


def clear_ban_cache(telegram_id: int = None):
    """Clear ban cache for a specific user or all users (called when ban status changes)."""
    global _ban_cache
    if telegram_id is None:
        _ban_cache.clear()
    elif telegram_id in _ban_cache:
        del _ban_cache[telegram_id]
