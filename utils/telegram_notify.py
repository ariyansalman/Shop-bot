"""Synchronous Telegram notifications for the Flask webhook process.

The bot process sends messages through python-telegram-bot's async
Application (see handlers.payment_handlers.check_pending_payments). The
webhook server is a plain sync Flask app running in its own process
(gunicorn), so it cannot reuse that Application instance — it talks to the
Bot API directly with `requests`, exactly like utils.error_alerts does.

Message wording is kept identical to the polling fallback so a buyer sees
the same confirmation regardless of which path credited the wallet.

Nothing here ever raises: a failed notification must never roll back or
block a payment that was already credited.
"""

import logging

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def send_telegram_message_sync(chat_id, text: str) -> bool:
    """Send a plain-text Telegram message. Returns True when delivered.

    Never raises. The bot token is only ever placed in the request URL and
    is never logged (error logs print the response body/exception only).
    """
    import requests
    from config.settings import settings

    token = getattr(settings, "BOT_TOKEN", None)
    if not token or not chat_id:
        logger.warning("telegram_notify skipped: missing bot token or chat_id")
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if response.status_code != 200:
            logger.error(
                "telegram_notify failed status=%s chat_id=%s body=%s",
                response.status_code, chat_id, response.text[:200]
            )
            return False
        return True
    except Exception as exc:  # notification failure must never break payments
        logger.error("telegram_notify error chat_id=%s error=%s", chat_id, exc)
        return False


def notify_payment_credited(user_telegram_id, amount: float, new_balance: float,
                            transaction_id: int, payment_method: str) -> None:
    """Notify the buyer and the admin that a top-up was credited.

    Mirrors the messages sent by the polling fallback in
    handlers.payment_handlers.check_pending_payments.
    """
    from config.settings import settings

    user_message = (
        "✅ Payment Confirmed!\n\n"
        f"💰 Amount: ${amount:.2f}\n"
        f"🔄 Your new wallet balance: ${new_balance:.2f}\n\n"
        "Thank you for your payment!"
    )
    send_telegram_message_sync(user_telegram_id, user_message)

    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", None)
    if admin_id:
        admin_message = (
            "💰 New Payment Received\n\n"
            f"👤 User ID: {user_telegram_id}\n"
            f"💰 Amount: ${amount:.2f}\n"
            f"📝 Transaction ID: #{transaction_id}\n"
            f"🔄 Payment Method: {payment_method}"
        )
        send_telegram_message_sync(admin_id, admin_message)
