#!/usr/bin/env python3
"""
Local dev server for butler-button-variants.

- Serves the workspace as static files (like `python3 -m http.server`).
- Adds a `/api/*` reverse proxy to the Veltm Supabase Edge Functions,
  injecting permissive CORS headers so the pages work from a Codespace
  (`*.app.github.dev`) or any other origin during local preview.
- Handles `/forms/request` — the demo / 15-minute-meeting forms. See
  tools/request_forms.py for the SMTP_* configuration it needs.

Usage:
    python3 tools/serve.py [port]   # default 8000
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import request_forms  # noqa: E402

SUPABASE_BASE = "https://glhbwpfkykycexyygwjj.supabase.co/functions/v1"
API_PREFIX = "/api/"
FORMS_PATH = "/forms/request"

# Which directory to serve as the web root, and which file to serve for "/".
# Defaults keep local dev unchanged (repo root + index.html). Production
# (Heroku) sets WEB_ROOT=proposed and INDEX_FILE=hotel-concierge-program.html
# via the Procfile so butlerbutton.co opens on the hotel concierge program page
# instead of the variant showcase.
WEB_ROOT = os.environ.get("WEB_ROOT", ".")
INDEX_FILE = os.environ.get("INDEX_FILE", "index.html")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, apikey, x-client-info",
    "Access-Control-Max-Age": "86400",
}

# The form endpoint sends real email, so it does not get the blanket `*` the
# read-only Supabase proxy uses. Same-origin posts (the normal case) send no
# Origin header at all and are unaffected by this list.
FORM_ORIGINS = tuple(
    o.strip()
    for o in os.environ.get(
        "BB_FORMS_ALLOWED_ORIGINS",
        "https://butlerbutton.co,https://www.butlerbutton.co",
    ).split(",")
    if o.strip()
)


def form_origin_allowed(origin: str) -> bool:
    if origin in FORM_ORIGINS:
        return True
    return bool(
        re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin)
        or re.match(r"^https://[\w.-]+\.app\.github\.dev$", origin)
    )


class Handler(SimpleHTTPRequestHandler):
    def _send_cors(self) -> None:
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)

    def _send_form_cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and form_origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path.startswith(API_PREFIX):
            self.send_response(204)
            self._send_cors()
            self.end_headers()
            return
        if self.path.split("?")[0] == FORMS_PATH:
            self.send_response(204)
            self._send_form_cors()
            self.end_headers()
            return
        super().do_OPTIONS() if hasattr(super(), "do_OPTIONS") else self._default_options()

    def _default_options(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith(API_PREFIX):
            self._proxy("POST")
            return
        if self.path.split("?")[0] == FORMS_PATH:
            self._handle_form()
            return
        self.send_error(405, "Method Not Allowed")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith(API_PREFIX):
            self._proxy("GET")
            return
        if self.path.split("?")[0] == FORMS_PATH:
            self._send_form_json(405, {"success": False, "error": "method_not_allowed"})
            return
        # Serve the configured landing page for the site root.
        if self.path in ("", "/"):
            self.path = "/" + INDEX_FILE
        super().do_GET()

    # ── Demo / meeting request forms ────────────────────────────────────
    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _send_form_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self._send_form_cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_form(self) -> None:
        origin = self.headers.get("Origin")
        if origin and not form_origin_allowed(origin):
            self._send_form_json(403, {"success": False, "error": "origin_not_allowed"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > request_forms.MAX_BODY_BYTES:
            self._send_form_json(413, {"success": False, "error": "payload_too_large"})
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send_form_json(
                400, {"success": False, "error": "invalid_json", "message": "Malformed request."}
            )
            return

        try:
            status, response = request_forms.handle(payload, self._client_ip())
        except Exception as exc:  # noqa: BLE001
            print(f"[forms] unhandled error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            status, response = 500, {"success": False, "error": "server_error"}
        self._send_form_json(status, response)

    def _proxy(self, method: str) -> None:
        target = SUPABASE_BASE + "/" + self.path[len(API_PREFIX):]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None

        req = urllib.request.Request(target, data=body, method=method)
        for h in ("Content-Type", "Authorization", "apikey", "x-client-info"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        # The Edge Function is CORS-locked to butlerbutton.co + www; spoof the
        # Origin so server-to-server proxy calls from this dev box are accepted
        # the same way as a real browser on production.
        req.add_header("Origin", "https://butlerbutton.co")
        req.add_header("Referer", "https://butlerbutton.co/")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self._send_cors()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read() or b""
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self._send_cors()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:  # noqa: BLE001
            msg = f'{{"success":false,"error":"proxy_error","message":{repr(str(e))}}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    web_root = os.path.abspath(WEB_ROOT)
    handler = functools.partial(Handler, directory=web_root)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Serving {web_root} on http://0.0.0.0:{port}  (root '/' -> {INDEX_FILE}; "
          f"proxy: {API_PREFIX}* -> {SUPABASE_BASE}/*; "
          f"forms: {FORMS_PATH} -> {request_forms.TEAM_EMAIL} "
          f"from {request_forms.FROM_EMAIL} [{request_forms.transport_summary()}])")
    for warning in request_forms.config_warnings():
        print(f"  [forms] WARNING: {warning}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
