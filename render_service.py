"""Railway/Render entry point for the BOT service.

Railway's health check expects the service to bind $PORT and answer HTTP —
but the bot itself runs a long-polling loop (application.run_polling() in
bot.py) and never opens a port. This wrapper starts a tiny stdlib HTTP
server on a background thread that only answers "/" and "/health", then
runs bot.py's existing main() in the main thread exactly as before.

Nothing about the bot's own startup (migrations, handlers, JobQueue, polling)
changes — this file only adds the health endpoint Railway needs to see the
service as up. The webhook payment-gateway receiver (webhook_server.py)
still deploys as its own separate Railway service; this file is unrelated
to it.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        data = json.dumps({
            "status": "ok",
            "service": "telegram-store-bot",
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


def health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


threading.Thread(target=health_server, daemon=True).start()

from bot import main
main()
