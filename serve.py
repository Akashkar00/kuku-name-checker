"""Production entry point: the Streamlit app plus HTTP security headers.

Streamlit has no setting for response headers, so app.py is served through
st.App with a small ASGI middleware that adds them, and that hides internal
endpoints nobody outside needs.

Run:  streamlit run serve.py      (the Dockerfile does this)
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import streamlit as st
from starlette.middleware import Middleware


def _inline_script_hashes() -> str:
    """CSP hashes of the inline <script>s in Streamlit's own index.html, read
    from the installed package so an upgrade cannot silently break the page."""
    html = (Path(st.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    bodies = [b for b in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) if b.strip()]
    return " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(b.encode()).digest()).decode()}'"
                    for b in bodies)


CSP = "; ".join([
    "default-src 'self'",
    # wasm-unsafe-eval lets Streamlit compile its WebAssembly; JavaScript eval stays blocked
    f"script-src 'self' 'wasm-unsafe-eval' {_inline_script_hashes()}",
    "style-src 'self' 'unsafe-inline'",   # Streamlit's CSS-in-JS injects <style> tags at runtime
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",                 # includes the same-origin websocket
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

HEADERS = [(k.encode(), v.encode()) for k, v in {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
}.items()]
_NAMES = {k for k, _ in HEADERS}

# Prometheus counters (upload cache sizes, session counts) and the agent API
# (off anyway) have no business being reachable from the internet.
BLOCKED = ("/_stcore/metrics", "/_stcore/agent")


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":          # websocket upgrades pass straight through
            return await self.app(scope, receive, send)
        if scope["path"].startswith(BLOCKED):
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"text/plain")] + HEADERS})
            await send({"type": "http.response.body", "body": b"Not found"})
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _NAMES]
                message = {**message, "headers": kept + HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = st.App("app.py", middleware=[Middleware(SecurityHeaders)])
