# Render / Heroku-style process types (documentation; Railway's bot service
# is actually driven by railway.toml, which points at render_service.py).
#
# `bot`     — the Telegram bot itself. Binds $PORT with a tiny /health
#             endpoint (render_service.py) so Railway/Render's health check
#             passes, then runs the bot via long-polling (bot.py's main()).
# `webhook` — the payment-gateway webhook receiver. Only needed if you use a
#             gateway requiring a public HTTPS callback (CryptoBot, ZiniPay).
#             Deploy as its own Railway/Render service with its own $PORT;
#             override that service's Start Command to the line below.
bot: python render_service.py
webhook: gunicorn webhook_server:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
