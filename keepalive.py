"""
Minimal HTTP server so Render's free tier treats this process as a
Web Service (which it pings to keep the dyno classified as "live").
This has nothing to do with Telegram — it just answers GET / with "OK"
on whatever port Render assigns via the PORT environment variable.
Runs in a background thread so it never blocks the bot's polling loop.

Also provides nudge_keepalive(), which any admin's bot can call whenever
a real end-user sends it a message. This makes a small outbound HTTP
request to this same service's own public URL, which Render counts as
inbound traffic and resets the free-tier sleep timer — so real usage of
ANY admin's bot keeps the whole service (bot.py + runner.py) awake,
with no external ping service and no manufactured fake traffic.
"""
import os
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Render gives you this automatically for the service itself; set it as
# an env var (RENDER_EXTERNAL_URL) — Render injects it natively, no need
# to set it manually. Falls back to localhost (a same-process self-ping
# that won't reset Render's clock, but won't error either) if missing.
_SELF_URL = os.environ.get("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 8080)}/"

# Throttle: only actually ping at most once per this many seconds, no
# matter how many user messages come in — one hit every few minutes is
# plenty to keep Render's 15-minute idle timer from ever firing.
_MIN_PING_INTERVAL = 240  # 4 minutes
_last_ping = 0
_lock = threading.Lock()


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # silence per-request logging spam


def start_keepalive_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _PingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"🌐 Keep-alive server listening on port {port}")


def _do_ping():
    try:
        urllib.request.urlopen(_SELF_URL, timeout=10)
    except Exception:
        pass  # never let a failed ping affect the bot


def nudge_keepalive():
    """
    Call this from any place that handles a real incoming user message.
    Fires at most once every _MIN_PING_INTERVAL seconds; cheap to call
    on every message since it no-ops instantly when throttled.
    """
    global _last_ping
    now = time.time()
    with _lock:
        if now - _last_ping < _MIN_PING_INTERVAL:
            return
        _last_ping = now
    threading.Thread(target=_do_ping, daemon=True).start()
