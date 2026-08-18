"""ZiniPay payment service (bKash / Nagad / Rocket via hosted checkout).

Flow:
1. create_invoice() -> hosted payment_url; buyer picks bKash/Nagad/Rocket
   on ZiniPay's own checkout page and pays.
2. ZiniPay calls our webhook (see webhook_server.py) with {invoice_id, status}.
3. Per ZiniPay's own recommendation, the webhook body is NOT trusted as
   proof of payment by itself — verify_invoice() is always called from our
   backend before crediting anything. The same verify_invoice() call also
   backs the polling fallback in payment_handlers.check_pending_payments,
   in case a webhook delivery is lost.

Docs: https://zinipay.com/docs
"""

from decimal import Decimal
from urllib.parse import urlparse

import requests

from config.settings import settings

BASE_URL = "https://api.zinipay.com"
# Advanced Transaction Verification API (must be enabled for this brand from
# the ZiniPay dashboard). Lets a buyer send bKash/Nagad/Rocket money directly
# to the store's own registered number and submit the transaction ID/smsRef
# in the bot, instead of being redirected to a hosted checkout page.
TRX_BASE_URL = "https://api.zinipay.com/v1/trx"
AMOUNT_TOLERANCE = Decimal("0.01")


class ZiniPayError(Exception):
    """Raised when the ZiniPay API call itself fails (network, auth, etc)."""


def _headers():
    if not settings.ZINIPAY_API_KEY:
        raise ZiniPayError("ZiniPay API key is not configured.")
    return {
        "Content-Type": "application/json",
        "zini-api-key": settings.ZINIPAY_API_KEY,
    }


def _trx_headers():
    """Header format documented specifically for the /v1/trx/* endpoints."""
    if not settings.ZINIPAY_API_KEY:
        raise ZiniPayError("ZiniPay API key is not configured.")
    return {
        "Content-Type": "application/json",
        "zinipay-api-key": settings.ZINIPAY_API_KEY,
    }


def create_invoice(amount, transaction_id: int, webhook_url: str = None) -> tuple[str, str]:
    """Create a hosted ZiniPay invoice.

    Returns:
        (invoice_id, payment_url)

    Raises:
        ZiniPayError on any failure.
    """
    payload = {
        "amount": float(amount),
        "metadata": {"order_id": str(transaction_id)},
        "redirect_url": settings.ZINIPAY_REDIRECT_URL,
        "cancel_url": settings.ZINIPAY_CANCEL_URL,
    }
    if webhook_url:
        payload["webhook_url"] = webhook_url

    try:
        response = requests.post(
            f"{BASE_URL}/v1/payment/create",
            headers=_headers(),
            json=payload,
            timeout=15
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay API: {e}")

    if response.status_code != 200:
        raise ZiniPayError(f"ZiniPay API error {response.status_code}: {response.text[:300]}")

    data = response.json()
    if not data.get("status"):
        raise ZiniPayError(f"ZiniPay invoice creation failed: {data.get('message')}")

    payment_url = data.get("payment_url", "")
    if not payment_url:
        raise ZiniPayError("ZiniPay response missing payment_url.")

    # The Create Invoice response doesn't return invoice_id directly — it's
    # the last path segment of payment_url
    # (https://secure.zinipay.com/payment/INVOICE_ID).
    invoice_id = urlparse(payment_url).path.rstrip('/').split('/')[-1]
    if not invoice_id:
        raise ZiniPayError(f"Could not extract invoice_id from payment_url: {payment_url}")

    return invoice_id, payment_url


def verify_invoice(invoice_id: str) -> dict:
    """Verify an invoice's current status directly from ZiniPay's backend.

    Returns the raw response dict: cus_name, cus_email, amount, invoice_id,
    payment_method, transaction_id, status (PENDING/COMPLETED/FAILED).

    Raises:
        ZiniPayError on any failure.
    """
    try:
        response = requests.post(
            f"{BASE_URL}/v1/payment/verify",
            headers=_headers(),
            json={"invoice_id": invoice_id},
            timeout=15
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay API: {e}")

    if response.status_code != 200:
        raise ZiniPayError(f"ZiniPay API error {response.status_code}: {response.text[:300]}")

    return response.json()


def is_paid(invoice_id: str, expected_amount) -> bool:
    """Convenience check used by the polling job: COMPLETED and amount matches."""
    try:
        data = verify_invoice(invoice_id)
    except ZiniPayError as e:
        print(f"ZiniPay verify error for {invoice_id}: {e}")
        return False

    if data.get("status") != "COMPLETED":
        return False

    amount = Decimal(str(data.get("amount", "0")))
    expected = Decimal(str(expected_amount))
    return abs(amount - expected) <= AMOUNT_TOLERANCE


def verify_payment(reference: str, expected_amount) -> tuple[bool, str]:
    """Verify a buyer-submitted bKash/Nagad/Rocket transaction ID (or smsRef)
    against ZiniPay's Transaction Verification API.

    Same (reference, expected_amount) -> (ok, message) shape as
    binance_pay.verify_payment / bybit_pay.verify_payment, so
    payment_reference_received() in handlers/payment_handlers.py can call
    all three interchangeably.

    This is a strict two-step flow:
      1. POST /verify  — looks up the transaction, atomically deducts 1
         ZiniPay verification credit, and returns masked sender data. The
         transaction is NOT yet consumed at this point.
      2. POST /confirm — using the id returned by /verify, permanently marks
         the transaction as used so the same payment can never be credited
         twice, even across different top-up attempts.

    Note: if /verify succeeds but this function's caller then rejects the
    top-up for an unrelated reason (e.g. the app-level external_reference
    duplicate check), /confirm is deliberately skipped, so the transaction
    stays unused on ZiniPay's side and can still be submitted again — the
    only cost is the 1 verification credit already spent by /verify.
    """
    reference = (reference or "").strip()
    if not reference:
        return False, "No reference provided."

    amount = float(Decimal(str(expected_amount)))

    try:
        verify_resp = requests.post(
            f"{TRX_BASE_URL}/verify",
            headers=_trx_headers(),
            json={"transactionId": reference, "amount": amount},
            timeout=15,
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay verify API: {e}")

    if verify_resp.status_code == 404:
        return False, "No matching transaction found yet. Double-check the transaction ID or wait a minute and try again."

    if verify_resp.status_code == 400:
        try:
            msg = verify_resp.json().get("message", "Invalid transaction.")
        except ValueError:
            msg = "Invalid transaction."
        if "already used" in msg.lower():
            return False, "This transaction ID has already been used."
        return False, msg

    if verify_resp.status_code == 402:
        # Store ran out of / has expired verification credits — not the
        # buyer's fault, surface it as a hard error so an admin gets paged
        # via the caller's error handling instead of the buyer being told
        # their payment is invalid.
        raise ZiniPayError("ZiniPay verification credits are insufficient or expired.")

    if verify_resp.status_code == 403:
        raise ZiniPayError(
            "ZiniPay Transaction Verification API is not enabled for this brand. "
            "Enable it from the ZiniPay dashboard."
        )

    if verify_resp.status_code != 200:
        raise ZiniPayError(f"ZiniPay verify error {verify_resp.status_code}: {verify_resp.text[:300]}")

    verify_data = verify_resp.json()
    data = verify_data.get("data") or {}
    internal_id = data.get("id")
    trx_id = data.get("trxID") or reference
    provider = data.get("provider", "")

    if not internal_id or data.get("status") != "UNUSED":
        return False, "This transaction is not available to verify (already used or invalid)."

    # Step 2: confirm — irreversibly consumes the transaction on ZiniPay's
    # side. Only call this once we're about to credit the wallet.
    try:
        confirm_resp = requests.post(
            f"{TRX_BASE_URL}/confirm",
            headers=_trx_headers(),
            json={"transactionId": trx_id, "amount": amount, "id": internal_id},
            timeout=15,
        )
    except requests.RequestException as e:
        raise ZiniPayError(f"Network error calling ZiniPay confirm API: {e}")

    if confirm_resp.status_code != 200:
        return False, (
            f"Transaction verified but confirmation failed (HTTP {confirm_resp.status_code}). "
            f"An admin will review it."
        )

    try:
        confirm_data = confirm_resp.json()
    except ValueError:
        confirm_data = {}

    if not confirm_data.get("success"):
        return False, f"Confirmation was not successful: {confirm_data.get('message', 'unknown error')}"

    provider_label = f" via {provider}" if provider else ""
    return True, f"Verified{provider_label} through ZiniPay Transaction Verification API."
