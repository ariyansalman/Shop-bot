"""Webhook server for receiving CryptoBot payment notifications.

This server receives real-time payment notifications from CryptoBot
when invoices are paid, providing immediate payment confirmation.

Setup:
1. Install Flask: pip install flask
2. For local testing, use ngrok: ngrok http 5000
3. Configure webhook in CryptoBot:
   - Open @CryptoBot in Telegram
   - Go to Crypto Pay → My Apps → Select your app → Webhooks
   - Enable webhooks and set URL: https://your-domain.com/webhook/cryptobot
4. For production, deploy this on a server with HTTPS
"""

from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import logging
import os
from datetime import datetime
from database.db import get_db_session
from database.models import Transaction, TransactionStatus, User, PaymentMethod
from config.settings import settings
from services import zinipay
from utils.error_alerts import send_admin_alert_sync
from utils.telegram_notify import notify_payment_credited

app = Flask(__name__)

# Structured-ish logging: one line per event, `event key=value` style, so
# payment failures are greppable in Railway/gunicorn logs instead of being
# swallowed. Secrets (bot token, Crypto Pay token, signatures) are never
# logged — only invoice/transaction identifiers and amounts.
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger('webhook_server')

# Invoices are always created in fiat USD for the exact top-up amount.
EXPECTED_FIAT = 'USD'
# Float tolerance when comparing money values (1 cent).
AMOUNT_TOLERANCE = 0.01



def verify_signature(body: bytes, signature: str) -> bool:
    """
    Verify CryptoBot webhook signature.

    Args:
        body: Raw request body bytes
        signature: Signature from crypto-pay-api-signature header

    Returns:
        True if signature is valid, False otherwise
    """
    # Create secret key from SHA256 hash of API token
    secret_key = hashlib.sha256(settings.CRYPTO_BOT_API_KEY.encode()).digest()

    # Calculate HMAC-SHA256 signature
    calculated_signature = hmac.new(
        secret_key,
        body,
        hashlib.sha256
    ).hexdigest()

    # Compare signatures
    return hmac.compare_digest(calculated_signature, signature)


def _verify_invoice_matches_transaction(invoice_data: dict, transaction) -> str:
    """Check the invoice really is the one we created for this transaction.

    Invoices are created as fiat/USD for the exact top-up amount (see
    services.crypto_bot.CryptoBotService.generate_payment_address), so the
    webhook payload must echo those values back. Returns an error string
    when the payload does not match, or "" when it is acceptable.

    Fields that CryptoBot does not include in a given payload version are
    simply not asserted — we never invent stricter rules than the invoice
    we actually created.
    """
    currency_type = invoice_data.get('currency_type')
    if currency_type and currency_type != 'fiat':
        return f"unexpected currency_type={currency_type} (expected fiat)"

    fiat = invoice_data.get('fiat')
    if fiat and fiat.upper() != EXPECTED_FIAT:
        return f"unexpected fiat currency={fiat} (expected {EXPECTED_FIAT})"

    raw_amount = invoice_data.get('amount')
    if raw_amount is not None:
        try:
            invoice_amount = float(raw_amount)
        except (TypeError, ValueError):
            return f"unparsable invoice amount={raw_amount!r}"
        if abs(invoice_amount - float(transaction.amount)) > AMOUNT_TOLERANCE:
            return (f"amount mismatch: invoice={invoice_amount:.2f} "
                    f"expected={float(transaction.amount):.2f}")

    return ""


def process_invoice_paid(invoice_data: dict):
    """
    Process a paid invoice notification.

    Guarantees:
      * only invoices whose status is 'paid' are considered;
      * the invoice currency and amount must match the pending transaction;
      * the PENDING -> COMPLETED flip is atomic, so a duplicate webhook (or
        the polling fallback in handlers.payment_handlers) can never credit
        the same payment twice;
      * the buyer and the admin are notified exactly once, after the credit.

    Args:
        invoice_data: Invoice object from CryptoBot webhook
    """
    notification = None
    try:
        invoice_data = invoice_data or {}
        invoice_id = invoice_data.get('invoice_id')
        status = invoice_data.get('status')
        paid_at = invoice_data.get('paid_at')

        logger.info("cryptobot_webhook invoice_id=%s status=%s paid_at=%s",
                    invoice_id, status, paid_at)

        if not invoice_id:
            logger.warning("cryptobot_webhook rejected reason=missing_invoice_id")
            return

        if status != 'paid':
            logger.warning("cryptobot_webhook ignored invoice_id=%s reason=status_not_paid status=%s",
                           invoice_id, status)
            return

        # Find transaction by invoice_id in crypto_address field
        # Format is: "invoice_id|pay_url"
        with get_db_session() as session:
            # Search for transaction with this invoice_id.
            # NOTE: compare against the PaymentMethod enum member, never a raw
            # lowercase string like 'crypto_wallet' — the column is a native
            # PostgreSQL enum whose labels are the member NAMES (CRYPTO_WALLET),
            # so a raw string raises "invalid input value for enum paymentmethod".
            # Indexed, SQL-side prefix lookup on crypto_address
            # ("<invoice_id>|<pay_url>"). This used to load EVERY pending
            # crypto transaction and scan them in Python, which got slower with
            # every unpaid invoice ever created.
            transaction = session.query(Transaction).filter(
                Transaction.payment_method == PaymentMethod.CRYPTO_WALLET,
                Transaction.status == TransactionStatus.PENDING,
                Transaction.crypto_address.like(f"{invoice_id}|%")
            ).order_by(Transaction.id.desc()).first()

            if not transaction:
                # Either an unknown invoice, or one already completed by the
                # polling fallback / an earlier webhook delivery. Both are safe
                # no-ops, but log them so real losses stay visible.
                logger.warning("cryptobot_webhook no_pending_transaction invoice_id=%s", invoice_id)
                return

            mismatch = _verify_invoice_matches_transaction(invoice_data, transaction)
            if mismatch:
                logger.error(
                    "cryptobot_webhook rejected invoice_id=%s transaction_id=%s reason=%s",
                    invoice_id, transaction.id, mismatch
                )
                send_admin_alert_sync(
                    "the CryptoBot webhook (invoice validation)",
                    ValueError(f"Invoice #{invoice_id} rejected: {mismatch}"),
                )
                return

            # Idempotency guard: atomically flip PENDING -> COMPLETED in a single
            # UPDATE. If another worker (the polling job, or a duplicate webhook
            # delivery) already completed this transaction, rowcount will be 0
            # and we skip crediting the wallet a second time.
            updated_rows = session.query(Transaction).filter(
                Transaction.id == transaction.id,
                Transaction.status == TransactionStatus.PENDING
            ).update({
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow()
            }, synchronize_session=False)

            if updated_rows == 0:
                logger.info("cryptobot_webhook duplicate_skipped invoice_id=%s transaction_id=%s",
                            invoice_id, transaction.id)
                return

            # Get user
            user = session.query(User).filter_by(id=transaction.user_id).first()

            if not user:
                logger.error("cryptobot_webhook user_missing transaction_id=%s user_id=%s",
                             transaction.id, transaction.user_id)
                return

            # Add funds to user's wallet
            user.wallet_balance += transaction.amount

            # Session commits automatically on context manager exit
            logger.info(
                "cryptobot_webhook credited invoice_id=%s transaction_id=%s telegram_id=%s "
                "amount=%.2f new_balance=%.2f",
                invoice_id, transaction.id, user.telegram_id,
                transaction.amount, user.wallet_balance
            )

            # Notification payload is built inside the session (the ORM objects
            # are detached afterwards) but sent *after* commit, so a Telegram
            # outage can never roll back a credited payment.
            notification = {
                'telegram_id': user.telegram_id,
                'amount': float(transaction.amount),
                'new_balance': float(user.wallet_balance),
                'transaction_id': transaction.id,
                'payment_method': transaction.payment_method.value,
            }

    except Exception as e:
        logger.exception("cryptobot_webhook processing_error error=%s", e)
        send_admin_alert_sync("the CryptoBot webhook (invoice processing)", e)
        return

    if notification:
        # Sent once, only on the delivery that actually flipped the row.
        notify_payment_credited(
            notification['telegram_id'],
            notification['amount'],
            notification['new_balance'],
            notification['transaction_id'],
            notification['payment_method'],
        )



@app.route('/webhook/cryptobot', methods=['POST'])
def cryptobot_webhook():
    """
    Webhook endpoint for CryptoBot payment notifications.

    CryptoBot sends POST requests to this endpoint when invoices are paid.
    Always answers 200 for well-formed, signed deliveries so CryptoBot does
    not retry a payment we already handled.
    """
    try:
        # Get signature from header
        signature = request.headers.get('crypto-pay-api-signature')

        if not signature:
            logger.warning("cryptobot_webhook rejected reason=missing_signature")
            return jsonify({'error': 'No signature'}), 401

        # Get raw request body
        body = request.get_data()

        # Verify signature (the signature value itself is never logged)
        if not verify_signature(body, signature):
            logger.warning("cryptobot_webhook rejected reason=invalid_signature bytes=%d", len(body))
            return jsonify({'error': 'Invalid signature'}), 401

        # Parse JSON
        data = request.get_json(silent=True) or {}

        # Extract update info
        update_type = data.get('update_type')
        request_date = data.get('request_date')
        payload = data.get('payload') or {}

        logger.info("cryptobot_webhook received update_type=%s request_date=%s invoice_id=%s",
                    update_type, request_date, payload.get('invoice_id'))
        logger.debug("cryptobot_webhook payload=%s", json.dumps(payload, default=str))

        # Check update type
        if update_type != 'invoice_paid':
            logger.warning("cryptobot_webhook ignored reason=unknown_update_type update_type=%s",
                           update_type)
            return jsonify({'ok': True}), 200

        # Process the paid invoice
        process_invoice_paid(payload)

        return jsonify({'ok': True}), 200

    except Exception as e:
        logger.exception("cryptobot_webhook endpoint_error error=%s", e)
        send_admin_alert_sync("the CryptoBot webhook", e)
        return jsonify({'error': 'Internal error'}), 500



@app.route('/webhook/zinipay', methods=['GET', 'POST'])
def zinipay_webhook():
    """Webhook endpoint for ZiniPay (bKash/Nagad/Rocket) payment notifications.

    Legacy path: only fires for transactions created via the older hosted-
    checkout flow (services.zinipay.create_invoice). The default bot flow
    now uses the Transaction Verification API (buyer sends money directly
    and submits a transaction ID in the bot — see
    handlers.payment_handlers.payment_method_bkash_nagad), which is verified
    synchronously in-chat and never touches this endpoint. Left in place in
    case a store still uses create_invoice() directly.

    Per ZiniPay's own recommendation, the webhook body itself is never
    trusted as proof of payment — it only tells us which invoice_id to
    re-verify via ZiniPay's backend Verify Invoice API before crediting.
    ZiniPay can call this as JSON body or query params; both are accepted.
    """
    notification = None
    try:
        if request.method == 'POST' and request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.args.to_dict()

        invoice_id = data.get('invoice_id')
        if not invoice_id:
            logger.warning("zinipay_webhook rejected reason=missing_invoice_id")
            return jsonify({'error': 'Missing invoice_id'}), 400

        logger.info("zinipay_webhook received invoice_id=%s", invoice_id)

        with get_db_session() as session:
            # Enum member comparison, same rule as the CryptoBot path above.
            # Same indexed prefix lookup as the CryptoBot path above.
            transaction = session.query(Transaction).filter(
                Transaction.payment_method == PaymentMethod.BKASH_NAGAD,
                Transaction.status == TransactionStatus.PENDING,
                Transaction.crypto_address.like(f"{invoice_id}|%")
            ).order_by(Transaction.id.desc()).first()

            if not transaction:
                logger.warning("zinipay_webhook no_pending_transaction invoice_id=%s", invoice_id)
                return jsonify({'ok': True}), 200

            # Always re-verify from ZiniPay's backend before crediting — the
            # webhook payload itself is not trusted. is_paid() also checks the
            # amount against the transaction.
            if not zinipay.is_paid(invoice_id, transaction.amount):
                logger.warning("zinipay_webhook unverified invoice_id=%s transaction_id=%s",
                               invoice_id, transaction.id)
                return jsonify({'ok': True}), 200

            # Idempotency guard: same atomic flip used for the CryptoBot
            # webhook above.
            updated_rows = session.query(Transaction).filter(
                Transaction.id == transaction.id,
                Transaction.status == TransactionStatus.PENDING
            ).update({
                Transaction.status: TransactionStatus.COMPLETED,
                Transaction.completed_at: datetime.utcnow()
            }, synchronize_session=False)

            if updated_rows == 0:
                logger.info("zinipay_webhook duplicate_skipped invoice_id=%s transaction_id=%s",
                            invoice_id, transaction.id)
                return jsonify({'ok': True}), 200

            user = session.query(User).filter_by(id=transaction.user_id).first()
            if not user:
                logger.error("zinipay_webhook user_missing transaction_id=%s user_id=%s",
                             transaction.id, transaction.user_id)
                return jsonify({'ok': True}), 200

            user.wallet_balance += transaction.amount

            logger.info(
                "zinipay_webhook credited invoice_id=%s transaction_id=%s telegram_id=%s "
                "amount=%.2f new_balance=%.2f",
                invoice_id, transaction.id, user.telegram_id,
                transaction.amount, user.wallet_balance
            )

            notification = {
                'telegram_id': user.telegram_id,
                'amount': float(transaction.amount),
                'new_balance': float(user.wallet_balance),
                'transaction_id': transaction.id,
                'payment_method': transaction.payment_method.value,
            }

    except Exception as e:
        logger.exception("zinipay_webhook error error=%s", e)
        send_admin_alert_sync("the ZiniPay webhook", e)
        return jsonify({'error': 'Internal error'}), 500

    if notification:
        notify_payment_credited(
            notification['telegram_id'],
            notification['amount'],
            notification['new_balance'],
            notification['transaction_id'],
            notification['payment_method'],
        )

    return jsonify({'ok': True}), 200



@app.route('/zinipay/success', methods=['GET'])
def zinipay_success():
    """Redirect target after a ZiniPay payment. Nothing to do here — the
    webhook/polling job credits the wallet; this page is just what the
    buyer sees in their browser before returning to Telegram."""
    return "<h2>✅ Payment received</h2><p>You can return to Telegram now — your balance will update shortly.</p>", 200


@app.route('/zinipay/cancel', methods=['GET'])
def zinipay_cancel():
    """Redirect target when a buyer cancels a ZiniPay payment."""
    return "<h2>❌ Payment cancelled</h2><p>You can return to Telegram and try again.</p>", 200


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'service': 'CryptoBot Webhook Receiver',
        'timestamp': datetime.utcnow().isoformat()
    }), 200


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with setup instructions."""
    return """
    <h1>CryptoBot Webhook Receiver</h1>
    <p>This server is running and ready to receive CryptoBot payment notifications.</p>

    <h2>Setup Instructions:</h2>
    <ol>
        <li>Go to <a href="https://t.me/CryptoBot">@CryptoBot</a> in Telegram</li>
        <li>Navigate to: Crypto Pay → My Apps → Select your app</li>
        <li>Tap "Webhooks..." and then "Enable Webhooks"</li>
        <li>Enter your webhook URL: <code>https://your-domain.com/webhook/cryptobot</code></li>
        <li>Save and start receiving real-time payment notifications!</li>
    </ol>

    <h2>Endpoints:</h2>
    <ul>
        <li><code>POST /webhook/cryptobot</code> - CryptoBot webhook endpoint</li>
        <li><code>GET /health</code> - Health check</li>
    </ul>

    <p><strong>Note:</strong> For local testing, use ngrok to create a public HTTPS URL.</p>
    """, 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    print("=" * 60)
    print("CryptoBot Webhook Server")
    print("=" * 60)
    print(f"Server starting on http://0.0.0.0:{port}")
    print(f"Webhook endpoint: /webhook/cryptobot")
    print()
    print("For local testing with ngrok:")
    print("  1. Run: ngrok http 5000")
    print("  2. Copy the HTTPS URL (e.g., https://abc123.ngrok.io)")
    print("  3. Set webhook in CryptoBot to: https://abc123.ngrok.io/webhook/cryptobot")
    print()
    print("Waiting for webhooks...")
    print("=" * 60)

    # Run Flask server. In production (Railway) this file is instead run
    # via gunicorn, which does not execute this __main__ block — see
    # Procfile: `gunicorn webhook_server:app --bind 0.0.0.0:$PORT`.
    app.run(host='0.0.0.0', port=port, debug=False)
