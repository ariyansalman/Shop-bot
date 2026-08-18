"""Admin error alerts with simple in-memory rate limiting.

Shared by the bot process (async, via python-telegram-bot's
Application.add_error_handler) and the Flask webhook process (sync, via a
plain requests.post to the Bot API — no async Application inside a sync
route).

Rate limiting is per-process and in memory on purpose: it only exists to
stop a repeating error from flooding the admin's DMs, so losing the state
on restart is fine.
"""

import time
import threading
import traceback as _traceback

# Telegram hard-caps messages at 4096 chars; leave room for the header.
TELEGRAM_MAX_CHARS = 4096
_TRACEBACK_BUDGET = 2500

# Don't re-alert the same error key more often than this.
ALERT_COOLDOWN_SECONDS = 300  # 5 minutes

_last_alerts = {}
_lock = threading.Lock()


def should_alert(error_key: str, cooldown: int = ALERT_COOLDOWN_SECONDS) -> bool:
    """True if this error key hasn't been alerted in the last `cooldown` seconds.

    Records the send time as a side effect when it returns True.
    """
    now = time.monotonic()
    with _lock:
        last = _last_alerts.get(error_key)
        if last is not None and (now - last) < cooldown:
            return False
        _last_alerts[error_key] = now
        # Keep the dict from growing without bound if error keys vary a lot.
        if len(_last_alerts) > 200:
            for key, seen in list(_last_alerts.items()):
                if (now - seen) > cooldown:
                    _last_alerts.pop(key, None)
        return True


def reset_rate_limit():
    """Clear the cooldown state (used by tests)."""
    with _lock:
        _last_alerts.clear()


def format_traceback(error: BaseException, limit: int = _TRACEBACK_BUDGET) -> str:
    """Render a traceback, keeping the tail (the actual failure point)."""
    text = "".join(
        _traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    if len(text) > limit:
        text = "...(truncated)...\n" + text[-limit:]
    return text


def build_alert(source: str, error: BaseException, context_lines=None) -> str:
    """Build the alert text sent to the admin. Always <= 4096 chars."""
    lines = [
        f"\U0001F6A8 <b>Unhandled error</b> in {source}",
        f"<b>{type(error).__name__}</b>: {str(error) or '(no message)'}",
    ]
    for line in (context_lines or []):
        if line:
            lines.append(line)
    header = "\n".join(lines)
    tb = format_traceback(error, limit=max(500, TELEGRAM_MAX_CHARS - len(header) - 60))
    message = f"{header}\n\n<pre>{_escape(tb)}</pre>"
    if len(message) > TELEGRAM_MAX_CHARS:
        message = message[:TELEGRAM_MAX_CHARS - 13] + "...</pre>"
    return message


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_admin_alert_sync(source: str, error: BaseException, context_lines=None) -> bool:
    """Send an alert from a sync process (Flask webhooks). Never raises.

    Returns True if a message was actually sent.
    """
    import requests
    from config.settings import settings

    error_key = f"{source}:{type(error).__name__}"
    if not should_alert(error_key):
        return False

    token = getattr(settings, "BOT_TOKEN", None)
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", None)
    if not token or not admin_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": build_alert(source, error, context_lines),
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if response.status_code != 200:
            print(f"⚠️ Could not alert admin ({response.status_code}): {response.text[:200]}")
            return False
        return True
    except Exception as alert_error:  # never let alerting break the webhook
        print(f"⚠️ Could not alert admin: {alert_error}")
        return False
