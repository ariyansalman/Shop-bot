"""Main bot entry point for the Telegram Digital Products Store."""

import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler, PreCheckoutQueryHandler
from config import settings, validate_settings, ensure_storage_dirs
from database import init_db
from database.init_data import initialize_database
from migrations.run_all import MigrationError
from handlers import (
    user_handlers, admin_handlers, payment_handlers, admin_conversations,
    dispute_handlers, admin_admins, referral_handlers, admin_referrals
)
from utils import admin_filter
from scripts.backup_db import BackupError, create_backup
from utils.error_alerts import build_alert, should_alert

# M""M M"""""""`YM M""""""'YMM M"""""`'"""`YM M""""""'YMM MM""""""""`M M""MMMMM""M 
# M  M M  mmmm.  M M  mmmm. `M M  mm.  mm.  M M  mmmm. `M MM  mmmmmmmM M  MMMMM  M 
# M  M M  MMMMM  M M  MMMMM  M M  MMM  MMM  M M  MMMMM  M M`      MMMM M  MMMMP  M 
# M  M M  MMMMM  M M  MMMMM  M M  MMM  MMM  M M  MMMMM  M MM  MMMMMMMM M  MMMM' .M 
# M  M M  MMMMM  M M  MMMM' .M M  MMM  MMM  M M  MMMM' .M MM  MMMMMMMM M  MMP' .MM 
# M  M M  MMMMM  M M       .MM M  MMM  MMM  M M       .MM MM        .M M     .dMMM 
# MMMM MMMMMMMMMMM MMMMMMMMMMM MMMMMMMMMMMMMM MMMMMMMMMMM MMMMMMMMMMMM MMMMMMMMMMM 

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def send_database_backup(context):
    """Dump the database and DM the file to the admin.

    Runs daily via JobQueue (see main()). Telegram bots can only upload files
    up to 50 MB — if the store's dump ever outgrows that, switch this job off
    with BACKUP_ENABLED=false and run scripts/backup_db.py from a Railway
    scheduled job that uploads somewhere with no size limit instead.
    """
    from datetime import datetime

    backup_path = None
    try:
        backup_path = create_backup()
        size_mb = os.path.getsize(backup_path) / (1024 * 1024)

        if size_mb > 49:
            raise BackupError(
                f"backup is {size_mb:.1f} MB, above Telegram's 50 MB upload limit"
            )

        with open(backup_path, 'rb') as fh:
            await context.bot.send_document(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                document=fh,
                filename=os.path.basename(backup_path),
                caption=(
                    f"\U0001F5C4 Database backup\n"
                    f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} - {size_mb:.2f} MB"
                ),
            )
        logger.info(f"Database backup sent to admin ({size_mb:.2f} MB)")
    except BackupError as e:
        logger.error(f"Database backup failed: {e}")
        try:
            await context.bot.send_message(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                text=f"\u26A0\uFE0F Database backup failed: {e}",
            )
        except Exception:
            logger.exception("Could not notify admin about the failed backup")
    except Exception as e:
        logger.exception(f"Unexpected error while sending database backup: {e}")
    finally:
        # The file was already delivered to Telegram; don't let dumps pile up
        # on the volume (no retention policy by design).
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                logger.warning(f"Could not delete local backup file {backup_path}")


async def error_handler(update, context):
    """Log any unhandled exception and DM a short alert to the admin.

    Registered with application.add_error_handler() — python-telegram-bot
    routes every uncaught handler/job exception here, so individual handlers
    don't need their own try/except.
    """
    error = context.error
    logger.error("Unhandled exception while processing an update", exc_info=error)

    # Don't let a repeating error flood the admin's DMs (5 min cooldown per
    # error type).
    if not should_alert(f"bot:{type(error).__name__}"):
        return

    context_lines = []
    if isinstance(update, Update):
        user = update.effective_user
        chat = update.effective_chat
        if user:
            context_lines.append(
                f"User: {user.id} (@{user.username})" if user.username else f"User: {user.id}"
            )
        if chat:
            context_lines.append(f"Chat: {chat.id} ({chat.type})")
        if update.effective_message and update.effective_message.text:
            context_lines.append(f"Message: {update.effective_message.text[:100]}")
        elif update.callback_query:
            context_lines.append(f"Callback: {update.callback_query.data}")

    try:
        await context.bot.send_message(
            chat_id=settings.ADMIN_TELEGRAM_ID,
            text=build_alert("the bot", error, context_lines),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not send the error alert to the admin")


def main():
    """Initialize and start the bot."""
    # Validate configuration
    try:
        validate_settings()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    # Prepare the runtime file storage tree (product images, logos, broadcast
    # images, uploads, backups). STORAGE_DIR is env-configurable and defaults
    # to the project directory. This never aborts startup: if the directories
    # cannot be created we only warn, and the individual upload handlers still
    # create what they need on demand.
    unavailable = ensure_storage_dirs()
    if unavailable:
        logger.warning(
            "Could not prepare storage directories %s - uploads may fail. "
            "Check STORAGE_DIR (currently %s) and its permissions.",
            ", ".join(unavailable), settings.STORAGE_DIR,
        )
    else:
        logger.info("Runtime file storage ready at %s", settings.STORAGE_DIR)
    if settings.STORAGE_DIR == os.path.abspath('.'):
        logger.warning(
            "STORAGE_DIR is unset and defaults to the project directory. On a "
            "container platform this filesystem is EPHEMERAL - attach a "
            "persistent volume and set STORAGE_DIR to its mount path (e.g. /data)."
        )

    # Initialize database.
    #
    # A failed REQUIRED migration raises out of init_db()/initialize_database()
    # (migrations.run_all.MigrationError). Startup is aborted with a non-zero
    # exit code in that case: the bot must never start polling against a
    # partially migrated database. Nothing is dropped or reset — the operator
    # fixes the database and restarts.
    try:
        initialize_database()
    except MigrationError as e:
        logger.critical(
            "FATAL: database migration failed, the bot will not start: %s", e
        )
        raise SystemExit(1)
    except Exception as e:
        logger.critical("FATAL: database initialization error: %s", e, exc_info=True)
        raise SystemExit(1)

    # Create application.
    #
    # Application.builder() only attaches a JobQueue when python-telegram-bot
    # was installed with its [job-queue] extra (APScheduler + pytz). If the
    # deployment image was built from a stale dependency cache, PTB logs
    # "No `JobQueue` set up" and application.job_queue is None, which used to
    # blow up further down with
    # AttributeError: 'NoneType' object has no attribute 'run_repeating'.
    # Check it up front so the failure is a clear, actionable startup error
    # instead of a crash mid-registration — the scheduled jobs are required,
    # so this never degrades to "run without them".
    application = Application.builder().token(settings.BOT_TOKEN).build()

    if application.job_queue is None:
        logger.error(
            "JobQueue is not available: python-telegram-bot is installed "
            "without its [job-queue] extra. Payment polling, availability "
            "broadcasts and database backups all depend on it, so the bot "
            "will not start.\n"
            "Fix: redeploy with a clean dependency cache, or run\n"
            '    python -m pip install --no-cache-dir --force-reinstall -r requirements.txt'
        )
        raise RuntimeError(
            "python-telegram-bot[job-queue] is not installed "
            "(application.job_queue is None)"
        )

    # Register command handlers
    application.add_handler(CommandHandler("start", user_handlers.start_command))
    application.add_handler(CommandHandler("help", user_handlers.help_command))
    application.add_handler(CommandHandler("admin", admin_handlers.admin_command))

    # Register conversation handlers for multi-step flows

    # Top-up conversation
    topup_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.topup_start, pattern="^topup$")],
        states={
            payment_handlers.AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.topup_amount)],
            payment_handlers.METHOD: [
                CallbackQueryHandler(payment_handlers.payment_method_crypto, pattern="^pay_crypto$"),
                CallbackQueryHandler(payment_handlers.payment_method_card, pattern="^pay_card$"),
                CallbackQueryHandler(payment_handlers.payment_method_binance, pattern="^pay_binance$"),
                CallbackQueryHandler(payment_handlers.payment_method_bybit, pattern="^pay_bybit$"),
                CallbackQueryHandler(payment_handlers.payment_method_bkash_nagad, pattern="^pay_bkash_nagad$"),
            ],
            payment_handlers.REFERENCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.payment_reference_received),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_topup, pattern="^cancel$"),
            CallbackQueryHandler(payment_handlers.cancel_topup)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(topup_conv_handler)

    # Telegram Payments (Card) handlers — confirmation arrives via the bot's update
    # polling, not a separate job: approve the pre-checkout, then credit on success.
    application.add_handler(PreCheckoutQueryHandler(payment_handlers.precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handlers.successful_payment_callback))

    # Product creation conversation
    create_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_product_start, pattern="^admin_create_product$")],
        states={
            admin_conversations.PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_name),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_desc),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_price),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_TYPE: [
                CallbackQueryHandler(admin_conversations.product_type, pattern="^type_"),
                CallbackQueryHandler(admin_conversations.product_type, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_CATEGORY: [
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cat_"),
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_SUBCATEGORY: [
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^subcat_"),
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_IMAGE: [
                MessageHandler(filters.PHOTO | filters.TEXT, admin_conversations.product_image),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DOWNLOAD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_download_link),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_KEYS: [
                MessageHandler(filters.Document.ALL, admin_conversations.product_keys),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_keys),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_product_creation),
            CallbackQueryHandler(admin_conversations.cancel_product_creation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_product_conv)

    # Product edit conversation
    edit_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_product_start, pattern="^admin_edit_product$")],
        states={
            admin_conversations.EDIT_SELECT_PRODUCT: [
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^edit_prod_"),
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^admin_edit_product_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_products$")
            ],
            admin_conversations.EDIT_SELECT_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^edit_"),
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_NEW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_new_value),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodcat_"),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodsubcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_IMAGE_VALUE: [
                MessageHandler(filters.PHOTO, admin_conversations.edit_image_value),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^remove_product_image$"),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^cancel_edit$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_product_conv)

    # Category creation conversation
    create_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_category_start, pattern="^admin_create_category$")],
        states={
            admin_conversations.CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_name)],
            admin_conversations.CATEGORY_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_desc)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_category_conv)

    # Subcategory creation conversation
    create_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_subcategory_start, pattern="^admin_create_subcategory$")],
        states={
            admin_conversations.SUBCATEGORY_CATEGORY: [
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^subcat_cat_"),
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^cancel_subcat$")
            ],
            admin_conversations.SUBCATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.subcategory_name)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_subcategory_conv)

    # Discount code creation conversation
    create_discount_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_discount_start, pattern="^admin_create_discount$")],
        states={
            admin_conversations.DISCOUNT_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.discount_code_value),
                CallbackQueryHandler(admin_conversations.cancel_discount_creation, pattern="^cancel_discount$")
            ],
            admin_conversations.DISCOUNT_TYPE: [
                CallbackQueryHandler(admin_conversations.discount_type_select, pattern="^disctype_"),
                CallbackQueryHandler(admin_conversations.discount_type_select, pattern="^cancel_discount$")
            ],
            admin_conversations.DISCOUNT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.discount_value_input),
                CallbackQueryHandler(admin_conversations.cancel_discount_creation, pattern="^cancel_discount$")
            ],
            admin_conversations.DISCOUNT_MAX_USES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.discount_max_uses_input),
                CallbackQueryHandler(admin_conversations.cancel_discount_creation, pattern="^cancel_discount$")
            ],
            admin_conversations.DISCOUNT_EXPIRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.discount_expiry_input),
                CallbackQueryHandler(admin_conversations.cancel_discount_creation, pattern="^cancel_discount$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_discount_creation),
            CallbackQueryHandler(admin_conversations.cancel_discount_creation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_discount_conv)

    # Category edit conversation
    edit_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_category_start, pattern="^admin_edit_category$")],
        states={
            admin_conversations.EDIT_CATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^edit_cat_"),
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^admin_edit_category_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_CATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^editcat_"),
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^cancel_edit_cat$")
            ],
            admin_conversations.EDIT_CATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_category_value),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_cat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_category_conv)

    # Subcategory edit conversation
    edit_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_subcategory_start, pattern="^admin_edit_subcategory$")],
        states={
            admin_conversations.EDIT_SUBCATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^edit_subcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^admin_edit_subcategory_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^editsubcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^cancel_edit_subcat$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_subcategory_value),
                CallbackQueryHandler(admin_conversations.edit_subcategory_value, pattern="^newcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_subcat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_subcategory_conv)

    # Support username configuration conversation
    config_support_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_support_username, pattern="^admin_support_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_support_conv)

    # Channel username configuration conversation
    config_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_channel_username, pattern="^admin_channel_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_channel_conv)

    # Welcome message configuration conversation
    config_welcome_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_welcome_message, pattern="^admin_welcome_msg$")],
        states={
            admin_conversations.WELCOME_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.welcome_message_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_welcome_conv)

    # FAQ text configuration conversation
    config_faq_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_faq_text, pattern="^admin_faq_text$")],
        states={
            admin_conversations.FAQ_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.faq_text_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_faq_conv)

    # Store logo configuration conversation
    config_logo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_store_logo, pattern="^admin_store_logo$")],
        states={
            admin_conversations.STORE_LOGO: [MessageHandler(filters.PHOTO, admin_conversations.store_logo_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_logo_conv)

    # Text-only broadcast conversation
    broadcast_text_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_text_start, pattern="^admin_broadcast_text$")],
        states={
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_text_message)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_text_conv)

    # Image + Text broadcast conversation
    broadcast_image_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_image_start, pattern="^admin_broadcast_image$")],
        states={
            admin_conversations.BROADCAST_IMAGE: [
                MessageHandler(filters.PHOTO, admin_conversations.broadcast_image_photo)
            ],
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_image_text)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_image_conv)

    # Dispute conversation
    dispute_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dispute_handlers.open_dispute_start, pattern="^open_dispute_")],
        states={
            dispute_handlers.DISPUTE_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dispute_handlers.dispute_reason_received)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(dispute_handlers.dispute_cancel, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, dispute_handlers.dispute_cancel)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(dispute_conv)

    # Direct purchase conversation (Buy Now flow)
    purchase_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.buy_product_start, pattern="^buy_")],
        states={
            payment_handlers.PURCHASE_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.purchase_quantity_input),
                CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"),
            MessageHandler(filters.COMMAND, payment_handlers.cancel_purchase)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(purchase_conv)

    # Discount code entry conversation, reached from the "Have a discount
    # code?" button on the purchase confirmation screen. Product/quantity
    # context lives in user_data (set earlier in purchase_conv), not in the
    # callback data, so this conversation only needs to collect the code.
    discount_code_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.discount_code_start, pattern="^enter_discount_code$")],
        states={
            payment_handlers.DISCOUNT_CODE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.discount_code_input),
                CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"),
            MessageHandler(filters.COMMAND, payment_handlers.cancel_purchase)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(discount_code_conv)

    # Register callback query handlers
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^back$"))  # Back button goes to main menu
    application.add_handler(CallbackQueryHandler(user_handlers.back_to_products_callback, pattern="^back_to_products$"))
    application.add_handler(CallbackQueryHandler(user_handlers.products_callback, pattern="^products"))
    application.add_handler(CallbackQueryHandler(user_handlers.category_callback, pattern="^category_"))
    application.add_handler(CallbackQueryHandler(user_handlers.subcategory_callback, pattern="^subcategory_"))
    application.add_handler(CallbackQueryHandler(user_handlers.product_callback, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(user_handlers.availability_callback, pattern="^availability$"))
    application.add_handler(CallbackQueryHandler(user_handlers.reviews_callback, pattern="^reviews(?:_page_\\d+)?$"))
    application.add_handler(CallbackQueryHandler(user_handlers.review_start_callback, pattern="^review_start$"))
    application.add_handler(CallbackQueryHandler(user_handlers.review_order_callback, pattern="^review_order_\\d+$"))
    application.add_handler(CallbackQueryHandler(user_handlers.review_rating_callback, pattern="^review_rating_\\d+_[1-5]$"))
    application.add_handler(CallbackQueryHandler(user_handlers.support_callback, pattern="^support$"))
    application.add_handler(CallbackQueryHandler(user_handlers.faq_callback, pattern="^faq$"))
    application.add_handler(CallbackQueryHandler(user_handlers.language_callback, pattern="^language$"))

    # Referral program (user side)
    application.add_handler(CallbackQueryHandler(referral_handlers.referral_callback, pattern="^referral$"))
    application.add_handler(CallbackQueryHandler(referral_handlers.referral_info_callback, pattern="^referral_info$"))
    application.add_handler(CallbackQueryHandler(referral_handlers.referral_history_callback, pattern="^referral_history$"))
    application.add_handler(CallbackQueryHandler(user_handlers.set_language_callback, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(user_handlers.order_history_callback, pattern="^order_history"))
    application.add_handler(CallbackQueryHandler(user_handlers.user_order_detail_callback, pattern="^user_order_detail_"))
    application.add_handler(CallbackQueryHandler(user_handlers.view_receipt_callback, pattern="^view_receipt_\\d+$"))

    # Purchase confirmation and cancellation handlers
    application.add_handler(CallbackQueryHandler(payment_handlers.confirm_purchase, pattern="^confirm_purchase_"))
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"))
    application.add_handler(CallbackQueryHandler(payment_handlers.remove_discount_code_callback, pattern="^remove_discount_code$"))

    # Global cancel handler for payment pages (outside conversation)
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_payment_page, pattern="^cancel$"))

    # Admin callback handlers
    # Manage Admins (OWNER only)
    add_admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_admins.add_admin_start, pattern="^admin_add_admin$")],
        states={
            admin_admins.ADMIN_ID_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, admin_admins.add_admin_id),
                CallbackQueryHandler(admin_admins.cancel_add_admin, pattern="^cancel_add_admin$"),
            ],
            admin_admins.ADMIN_ROLE_INPUT: [
                CallbackQueryHandler(admin_admins.add_admin_role, pattern="^new_admin_role_"),
                CallbackQueryHandler(admin_admins.cancel_add_admin, pattern="^cancel_add_admin$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_admins.cancel_add_admin, pattern="^cancel_add_admin$"),
            MessageHandler(filters.COMMAND, admin_admins.cancel_add_admin),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(add_admin_conv)
    application.add_handler(CallbackQueryHandler(admin_admins.manage_admins_callback, pattern="^admin_manage_admins$"))
    application.add_handler(CallbackQueryHandler(admin_admins.confirm_remove_admin_callback, pattern="^admin_confirm_remove_admin_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_admins.remove_admin_callback, pattern="^admin_remove_admin_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_admins.noop_callback, pattern="^noop_admin$"))
    # The "Page x/y" pagination labels use callback_data="noop"; without a
    # handler Telegram leaves a loading spinner on the button until it times
    # out. Same absorb-and-answer behaviour as noop_admin above.
    application.add_handler(CallbackQueryHandler(admin_admins.noop_callback, pattern="^noop$"))

    application.add_handler(CallbackQueryHandler(admin_handlers.admin_menu_callback, pattern="^admin_menu$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_products_callback, pattern="^admin_products"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_restock_keys_callback, pattern="^admin_restock_keys$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_low_stock_callback, pattern="^admin_low_stock$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_manage_categories_callback, pattern="^admin_manage_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_categories_callback, pattern="^admin_view_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_users_callback, pattern="^admin_view_users"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_user_detail_callback, pattern="^view_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_ban_user_callback, pattern="^ban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_ban_user_callback, pattern="^confirm_ban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_unban_user_callback, pattern="^unban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_orders_callback, pattern="^admin_view_orders"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_order_menu, pattern="^admin_confirm_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_menu, pattern="^admin_cancel_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_payment_callback, pattern="^confirm_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_payment_callback, pattern="^cancel_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_order_detail_callback, pattern="^view_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_complete_order_callback, pattern="^complete_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_callback, pattern="^cancel_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_cancel_order_callback, pattern="^confirm_cancel_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_reactivate_order_callback, pattern="^reactivate_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_reactivate_order_callback, pattern="^confirm_reactivate_order_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_view_disputes_callback, pattern="^admin_view_disputes"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_dispute_detail_callback, pattern="^admin_dispute_detail_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_resolve_dispute_callback, pattern="^resolve_dispute_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_users_callback, pattern="^admin_users"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_orders_callback, pattern="^admin_orders"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_stats_callback, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_settings_callback, pattern="^admin_settings"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_broadcast_callback, pattern="^admin_broadcast"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_discounts_callback, pattern="^admin_discounts$"))
    # Referral management (admin side). The conversations are registered
    # before the plain callbacks so their entry-point patterns win.
    referral_value_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_referrals.admin_ref_set_value_start,
                                 pattern="^admin_ref_set_(value|min|max)$")
        ],
        states={
            admin_referrals.REFERRAL_VALUE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                               admin_referrals.admin_ref_set_value_input),
                CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            CommandHandler("cancel", admin_referrals.admin_ref_cancel),
        ],
        per_message=False,
    )
    application.add_handler(referral_value_conv)

    referral_search_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_referrals.admin_ref_search_start,
                                 pattern="^admin_ref_search$")
        ],
        states={
            admin_referrals.REFERRAL_SEARCH_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                               admin_referrals.admin_ref_search_input),
                CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            CommandHandler("cancel", admin_referrals.admin_ref_cancel),
        ],
        per_message=False,
    )
    application.add_handler(referral_search_conv)

    referral_credit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_referrals.admin_ref_credit_start,
                                 pattern="^admin_ref_credit$")
        ],
        states={
            admin_referrals.REFERRAL_CREDIT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                               admin_referrals.admin_ref_credit_input),
                CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_referrals.admin_ref_cancel, pattern="^admin_ref_cancel$"),
            CommandHandler("cancel", admin_referrals.admin_ref_cancel),
        ],
        per_message=False,
    )
    application.add_handler(referral_credit_conv)

    application.add_handler(CallbackQueryHandler(admin_referrals.admin_referrals_callback, pattern="^admin_referrals$"))
    application.add_handler(CallbackQueryHandler(admin_referrals.admin_ref_toggle_callback, pattern="^admin_ref_toggle$"))
    application.add_handler(CallbackQueryHandler(admin_referrals.admin_ref_toggle_first_callback, pattern="^admin_ref_toggle_first$"))
    application.add_handler(CallbackQueryHandler(admin_referrals.admin_ref_type_callback, pattern="^admin_ref_type$"))
    application.add_handler(CallbackQueryHandler(admin_referrals.admin_ref_stats_callback, pattern="^admin_ref_stats$"))
    application.add_handler(CallbackQueryHandler(admin_referrals.admin_ref_history_callback, pattern="^admin_ref_history"))

    application.add_handler(CallbackQueryHandler(admin_handlers.admin_audit_log_callback, pattern="^admin_audit_log"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_export_menu_callback, pattern="^admin_export_menu$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_export_csv_callback, pattern="^admin_export_(orders|users|transactions)$"))
    application.add_handler(CallbackQueryHandler(admin_conversations.admin_view_discounts_callback, pattern="^admin_view_discounts"))
    application.add_handler(CallbackQueryHandler(admin_conversations.admin_discount_detail_callback, pattern="^view_discount_"))
    application.add_handler(CallbackQueryHandler(admin_conversations.admin_toggle_discount_callback, pattern="^toggle_discount_"))

    # Restock keys conversation handler
    # Admin wallet adjustment conversation, entered from the "Adjust Balance"
    # button on the admin user detail screen.
    adjust_balance_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.adjust_balance_start, pattern="^adjust_balance_\\d+$")],
        states={
            admin_conversations.ADJUST_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, admin_conversations.adjust_balance_amount),
                CallbackQueryHandler(admin_conversations.cancel_adjust_balance, pattern="^cancel_adjust_balance$"),
            ],
            admin_conversations.ADJUST_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, admin_conversations.adjust_balance_reason),
                CallbackQueryHandler(admin_conversations.adjust_balance_confirm, pattern="^confirm_adjust_balance$"),
                CallbackQueryHandler(admin_conversations.cancel_adjust_balance, pattern="^cancel_adjust_balance$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_adjust_balance, pattern="^cancel_adjust_balance$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_adjust_balance),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(adjust_balance_conv)

    restock_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_handlers.admin_select_product_restock_callback, pattern="^select_product_")],
        states={
            admin_handlers.WAITING_FOR_KEYS: [
                MessageHandler(filters.Document.ALL & admin_filter, admin_handlers.handle_restock_keys_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, admin_handlers.handle_restock_keys_paste),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_handlers.cancel_restock, pattern="^cancel_restock$"),
            CommandHandler("cancel", admin_handlers.cancel_restock, filters=admin_filter),
        ],
    )
    application.add_handler(restock_conv)

    # Global error handler - alerts the admin instead of failing silently
    application.add_error_handler(error_handler)

    # Schedule background jobs (job_queue is guaranteed non-None by the
    # startup check above).
    job_queue = application.job_queue

    # Payment checking jobs
    job_queue.run_repeating(
        payment_handlers.check_pending_payments,
        interval=settings.PAYMENT_CHECK_INTERVAL,
        first=10
    )
    job_queue.run_repeating(
        payment_handlers.check_expired_payments,
        interval=60,
        first=30
    )

    # Availability broadcast job - runs every 12 hours (43200 seconds)
    logger.info("Scheduling availability broadcast job (first run in 10 seconds, then every 12 hours)")
    job_queue.run_repeating(
        payment_handlers.broadcast_availability_to_all_users,
        interval=43200,  # 12 hours in seconds
        first=10  # Start 10 seconds after bot starts (for testing)
    )

    # Daily database backup job - dumps the DB and DMs it to the admin
    if settings.BACKUP_ENABLED:
        backup_interval = settings.BACKUP_INTERVAL_HOURS * 3600
        logger.info(
            f"Scheduling database backup job (first run in 5 minutes, then every "
            f"{settings.BACKUP_INTERVAL_HOURS:g} hours)"
        )
        job_queue.run_repeating(
            send_database_backup,
            interval=backup_interval,
            first=300  # 5 minutes after startup, so boot isn't slowed down
        )
    else:
        logger.info("Database backup job disabled (BACKUP_ENABLED=false)")

    # Start the bot
    logger.info("Bot started successfully!")
    logger.info("Availability broadcast will run in 10 seconds...")
    application.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query"])


if __name__ == "__main__":
    main()
