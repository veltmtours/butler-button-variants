#!/usr/bin/env python3
"""
End-to-end test for the demo / meeting request forms.

Spins up stub mail servers — one speaking SMTP, one standing in for the
ZeptoMail HTTPS API — plus real `tools/serve.py` processes pointed at them,
posts to `/forms/request`, and checks what actually landed in the mailbox.
Standard library only (Python's `smtpd` was removed in 3.12, so the stub below
speaks just enough SMTP itself).

Usage:
    python3 tools/test_request_forms.py [-v]
"""
from __future__ import annotations

import email
import email.policy
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import request_forms  # noqa: E402


# ── Stub SMTP server ─────────────────────────────────────────────────────────


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"220 stub.local ESMTP ready\r\n")
        sender = None
        rcpts: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()

            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-stub.local\r\n250 8BITMIME\r\n")
            elif upper.startswith("MAIL FROM"):
                sender = command[10:].strip().strip("<>").split()[0].strip("<>")
                self.wfile.write(b"250 OK\r\n")
            elif upper.startswith("RCPT TO"):
                rcpts.append(command[8:].strip().split()[0].strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif upper == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                chunks: list[bytes] = []
                while True:
                    data_line = self.rfile.readline()
                    if not data_line or data_line in (b".\r\n", b".\n"):
                        break
                    if data_line.startswith(b".."):
                        data_line = data_line[1:]
                    chunks.append(data_line)
                self.server.inbox.append(  # type: ignore[attr-defined]
                    {"from": sender, "to": list(rcpts), "raw": b"".join(chunks)}
                )
                sender, rcpts = None, []
                self.wfile.write(b"250 OK queued\r\n")
            elif upper == "RSET":
                sender, rcpts = None, []
                self.wfile.write(b"250 OK\r\n")
            elif upper == "NOOP":
                self.wfile.write(b"250 OK\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"502 Not implemented\r\n")


class StubSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _SMTPHandler)
        self.inbox: list[dict] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Stub ZeptoMail API ───────────────────────────────────────────────────────


class _ZeptoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"_unparseable": raw.decode("utf-8", "replace")}

        self.server.received.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "payload": payload,
            }
        )

        status, body = self.server.next_response()  # type: ignore[attr-defined]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubZepto(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _ZeptoHandler)
        self.received: list[dict] = []
        self.fail_next = 0  # how many upcoming calls should return an error

    def next_response(self) -> tuple[int, dict]:
        if self.fail_next > 0:
            self.fail_next -= 1
            return 400, {
                "error": {
                    "code": "TM_3201",
                    "message": "Invalid sender",
                    "details": [{"message": "from address is not verified"}],
                }
            }
        return 201, {"data": [{"code": "EM_104", "message": "OK"}], "message": "OK"}


# ── Fixtures ─────────────────────────────────────────────────────────────────

SMTP_PORT = free_port()
ZEPTO_PORT = free_port()
HTTP_PORT = free_port()
# The throttle is per-IP and every test here comes from 127.0.0.1, so it gets
# its own server with a low limit rather than eating the other tests' budget.
RATE_PORT = free_port()
# A third server delivers through the stub ZeptoMail API instead of SMTP,
# and a fourth does the same with BB_FORMS_DEBUG on.
ZEPTO_SITE_PORT = free_port()
DEBUG_SITE_PORT = free_port()

BASE = f"http://127.0.0.1:{HTTP_PORT}"
FORMS_URL = f"{BASE}/forms/request"
RATE_FORMS_URL = f"http://127.0.0.1:{RATE_PORT}/forms/request"
ZEPTO_FORMS_URL = f"http://127.0.0.1:{ZEPTO_SITE_PORT}/forms/request"
DEBUG_FORMS_URL = f"http://127.0.0.1:{DEBUG_SITE_PORT}/forms/request"
RATE_MAX = 3
ZEPTO_TOKEN = "wSsVR61A.testtoken.example"

smtp_server: StubSMTP | None = None
zepto_server: StubZepto | None = None
servers: list[subprocess.Popen] = []


def _start_server(port: int, rate_max: int, transport: str = "smtp",
                  debug: bool = False) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "WEB_ROOT": "proposed",
            "INDEX_FILE": "home.html",
            # Sends come FROM the ZeptoMail-verified domain (veltmtours.com);
            # they land in the butlerbutton.co inbox. Recipients need no setup.
            "MAIL_FROM": "partners@veltmtours.com",
            "BB_TEAM_EMAIL": "partners@butlerbutton.co",
            "BB_FORMS_RATE_MAX": str(rate_max),
            "BB_FORMS_RATE_WINDOW": "600",
            # Clear both transports, then enable exactly the one under test.
            "ZEPTOMAIL_TOKEN": "",
            "SMTP_HOST": "",
            "BB_FORMS_DEBUG": "1" if debug else "",
        }
    )
    if transport == "zeptomail":
        env["ZEPTOMAIL_TOKEN"] = ZEPTO_TOKEN
        env["ZEPTOMAIL_API_URL"] = f"http://127.0.0.1:{ZEPTO_PORT}/v1.1/email"
    else:
        env.update(
            {
                "SMTP_HOST": "127.0.0.1",
                "SMTP_PORT": str(SMTP_PORT),
                "SMTP_SECURITY": "none",
                "SMTP_USER": "",
            }
        )
    proc = subprocess.Popen(
        [sys.executable, os.path.join("tools", "serve.py"), str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"serve.py exited: {proc.communicate()[1].decode()}")
            time.sleep(0.15)
    raise RuntimeError("serve.py did not start in time")


def setUpModule() -> None:  # noqa: N802
    global smtp_server, zepto_server
    smtp_server = StubSMTP(SMTP_PORT)
    threading.Thread(target=smtp_server.serve_forever, daemon=True).start()
    zepto_server = StubZepto(ZEPTO_PORT)
    threading.Thread(target=zepto_server.serve_forever, daemon=True).start()
    servers.append(_start_server(HTTP_PORT, rate_max=500))
    servers.append(_start_server(RATE_PORT, rate_max=RATE_MAX))
    servers.append(_start_server(ZEPTO_SITE_PORT, rate_max=500, transport="zeptomail"))
    servers.append(_start_server(DEBUG_SITE_PORT, rate_max=500, transport="zeptomail", debug=True))


def tearDownModule() -> None:  # noqa: N802
    for proc in servers:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    for server in (smtp_server, zepto_server):
        if server is not None:
            server.shutdown()
            server.server_close()


def post(payload: dict, url: str = FORMS_URL) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def inbox() -> list[dict]:
    assert smtp_server is not None
    return smtp_server.inbox


def parsed(entry: dict) -> email.message.EmailMessage:
    return email.message_from_bytes(entry["raw"], policy=email.policy.default)


def plain_body(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


DEMO = {
    "request_type": "demo",
    "name": "José Ferreira",
    "email": "jose@thebeachhouse.example",
    "phone": "+351 912 345 678",
    "company": "The Beach House",
    "role": "General Manager",
    "location": "Lisbon, Portugal",
    "property_type": "Hotel",
    "rooms": "48",
    "message": "Most guest questions are about restaurants and airport transfers.",
    "page": "/hotel-concierge-program.html",
}

MEETING = {
    "request_type": "meeting",
    "name": "Priya Nair",
    "email": "priya@wavesresort.example",
    "company": "Waves Resort",
    "location": "Goa, India",
    "preferred_date": "2026-08-04",
    "preferred_time": "Morning (08:00 - 12:00)",
    "timezone": "Asia/Kolkata",
}


class DemoRequest(unittest.TestCase):
    def setUp(self) -> None:
        inbox().clear()

    def test_demo_request_mails_team_and_requester(self) -> None:
        status, body = post(DEMO)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])

        rid = body["request_id"]
        self.assertRegex(rid, r"^BB-DEMO-\d{8}-[A-HJ-NP-Z2-9]{6}$")

        self.assertEqual(len(inbox()), 2, "expected a team mail and an acknowledgement")
        team, ack = parsed(inbox()[0]), parsed(inbox()[1])

        # Sent from the ZeptoMail-verified domain, delivered to the team inbox.
        self.assertIn("partners@veltmtours.com", team["From"])
        self.assertIn("partners@veltmtours.com", ack["From"])

        # Team mail: request ID in the subject, requester on Reply-To.
        self.assertIn(rid, team["Subject"])
        self.assertIn("Demo request", team["Subject"])
        self.assertIn("The Beach House", team["Subject"])
        self.assertEqual(inbox()[0]["to"], ["partners@butlerbutton.co"])
        self.assertIn(DEMO["email"], team["Reply-To"])

        team_text = plain_body(team)
        for value in ("The Beach House", "General Manager", "Lisbon, Portugal", "48", "Hotel"):
            self.assertIn(value, team_text)

        # Acknowledgement: same request ID, addressed to the requester.
        self.assertIn(rid, ack["Subject"])
        self.assertEqual(inbox()[1]["to"], [DEMO["email"]])
        self.assertIn("partners@butlerbutton.co", ack["Reply-To"])
        ack_text = plain_body(ack)
        self.assertIn(rid, ack_text)
        self.assertIn("demo", ack_text.lower())

        # Both carry an HTML alternative.
        for msg in (team, ack):
            self.assertTrue(any(p.get_content_type() == "text/html" for p in msg.walk()))

    def test_meeting_request_uses_its_own_id_and_fields(self) -> None:
        status, body = post(MEETING)
        self.assertEqual(status, 200, body)
        rid = body["request_id"]
        self.assertRegex(rid, r"^BB-MTG-\d{8}-[A-HJ-NP-Z2-9]{6}$")

        team, ack = parsed(inbox()[0]), parsed(inbox()[1])
        self.assertIn(rid, team["Subject"])
        self.assertIn("15-minute meeting request", team["Subject"])
        team_text = plain_body(team)
        self.assertIn("2026-08-04", team_text)
        self.assertIn("Asia/Kolkata", team_text)
        # Demo-only labels must not leak into a meeting request.
        self.assertNotIn("Rooms or keys", team_text)
        self.assertIn(rid, plain_body(ack))

    def test_ids_are_unique(self) -> None:
        ids = {post(DEMO)[1]["request_id"] for _ in range(4)}
        self.assertEqual(len(ids), 4)


class Validation(unittest.TestCase):
    def setUp(self) -> None:
        inbox().clear()

    def test_missing_required_field_is_rejected(self) -> None:
        payload = dict(DEMO)
        del payload["company"]
        status, body = post(payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["success"])
        self.assertIn("required", body["message"].lower())
        self.assertEqual(inbox(), [])

    def test_bad_email_is_rejected(self) -> None:
        status, body = post({**DEMO, "email": "not-an-email"})
        self.assertEqual(status, 400)
        self.assertEqual(inbox(), [])

    def test_unknown_request_type_is_rejected(self) -> None:
        status, _ = post({**DEMO, "request_type": "quote"})
        self.assertEqual(status, 400)
        self.assertEqual(inbox(), [])

    def test_honeypot_sends_nothing(self) -> None:
        status, body = post({**DEMO, "website": "http://spam.example"})
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertEqual(inbox(), [], "honeypot submissions must not be mailed")

    def test_header_injection_is_neutralised(self) -> None:
        """Injected CRLF must become inert text, never a header of its own."""
        status, body = post(
            {
                **DEMO,
                "company": "Acme\r\nBcc: attacker@evil.example",
                "name": "A\nB",
                "email": "jose@thebeachhouse.example",
            }
        )
        self.assertEqual(status, 200, body)

        raw = inbox()[0]["raw"].decode("utf-8", "replace")
        headers = raw.partition("\r\n\r\n")[0]
        # No injected header line, and the envelope still has one recipient.
        self.assertNotRegex(headers, r"(?im)^Bcc:")
        self.assertEqual(inbox()[0]["to"], ["partners@butlerbutton.co"])

        team = parsed(inbox()[0])
        self.assertIsNone(team["Bcc"])
        self.assertNotIn("\n", team["Subject"])
        self.assertEqual(team["Reply-To"].addresses[0].addr_spec, "jose@thebeachhouse.example")
        # The acknowledgement still goes only to the requester.
        self.assertEqual(inbox()[1]["to"], ["jose@thebeachhouse.example"])

    def test_oversized_message_is_truncated_not_rejected(self) -> None:
        status, body = post({**DEMO, "message": "x" * 5000})
        self.assertEqual(status, 200, body)
        runs = re.findall(r"x{3,}", plain_body(parsed(inbox()[0])))
        self.assertEqual([len(r) for r in runs], [2000])

    def test_message_keeps_its_line_breaks(self) -> None:
        status, body = post({**DEMO, "message": "Line one.\nLine two."})
        self.assertEqual(status, 200, body)
        text = plain_body(parsed(inbox()[0]))
        self.assertIn("Line one.", text)
        self.assertIn("Line two.", text)

    def test_get_is_not_allowed(self) -> None:
        req = urllib.request.Request(FORMS_URL, method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 405)

    def test_malformed_json_is_rejected(self) -> None:
        req = urllib.request.Request(
            FORMS_URL, data=b"{nope", headers={"Content-Type": "application/json"}, method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_foreign_origin_is_refused(self) -> None:
        req = urllib.request.Request(
            FORMS_URL,
            data=json.dumps(DEMO).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(inbox(), [])


class ZeptoMailTransport(unittest.TestCase):
    """The same flow, delivered through ZeptoMail's HTTPS API instead of SMTP."""

    def setUp(self) -> None:
        assert zepto_server is not None
        zepto_server.received.clear()
        zepto_server.fail_next = 0
        inbox().clear()

    @property
    def sent(self) -> list[dict]:
        assert zepto_server is not None
        return zepto_server.received

    def test_demo_request_goes_out_over_the_api(self) -> None:
        status, body = post(DEMO, url=ZEPTO_FORMS_URL)
        self.assertEqual(status, 200, body)
        rid = body["request_id"]
        self.assertRegex(rid, r"^BB-DEMO-\d{8}-[A-HJ-NP-Z2-9]{6}$")

        self.assertEqual(len(self.sent), 2, "expected a team mail and an acknowledgement")
        self.assertEqual(inbox(), [], "nothing should reach SMTP on this server")

        team, ack = self.sent[0], self.sent[1]

        # Authenticated the way ZeptoMail expects.
        for call in (team, ack):
            self.assertEqual(call["path"], "/v1.1/email")
            self.assertEqual(call["auth"], f"Zoho-enczapikey {ZEPTO_TOKEN}")
            self.assertIn("application/json", call["content_type"])

        tp = team["payload"]
        # Sent from the verified domain, delivered to the Butler Button inbox.
        self.assertEqual(tp["from"]["address"], "partners@veltmtours.com")
        self.assertEqual(tp["to"][0]["email_address"]["address"], "partners@butlerbutton.co")
        self.assertIn(rid, tp["subject"])
        self.assertIn("Demo request", tp["subject"])
        # Requester on reply_to, so a reply reaches them directly.
        self.assertEqual(tp["reply_to"][0]["address"], DEMO["email"])
        for value in ("The Beach House", "General Manager", "Lisbon, Portugal", "48"):
            self.assertIn(value, tp["textbody"])
        self.assertIn("<", tp["htmlbody"])

        ap = ack["payload"]
        self.assertEqual(ap["from"]["address"], "partners@veltmtours.com")
        self.assertEqual(ap["to"][0]["email_address"]["address"], DEMO["email"])
        self.assertIn(rid, ap["subject"], "acknowledgement must carry the same ID")
        self.assertIn(rid, ap["textbody"])
        self.assertEqual(ap["reply_to"][0]["address"], "partners@butlerbutton.co")

    def test_meeting_request_over_the_api(self) -> None:
        status, body = post(MEETING, url=ZEPTO_FORMS_URL)
        self.assertEqual(status, 200, body)
        self.assertRegex(body["request_id"], r"^BB-MTG-\d{8}-[A-HJ-NP-Z2-9]{6}$")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("15-minute meeting request", self.sent[0]["payload"]["subject"])
        self.assertIn("2026-08-04", self.sent[0]["payload"]["textbody"])

    def test_api_rejection_is_reported_not_swallowed(self) -> None:
        assert zepto_server is not None
        zepto_server.fail_next = 2  # both calls fail
        status, body = post(DEMO, url=ZEPTO_FORMS_URL)
        self.assertEqual(status, 502)
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "delivery_failed")
        self.assertIn("request_id", body)
        # Off by default: the provider's wording stays in the logs.
        self.assertNotIn("detail", body)

    def test_debug_flag_returns_the_provider_reason(self) -> None:
        assert zepto_server is not None
        zepto_server.fail_next = 2
        status, body = post(DEMO, url=DEBUG_FORMS_URL)
        self.assertEqual(status, 502)
        self.assertIn("detail", body)
        # ZeptoMail's own message and sub-detail, so the cause is actionable.
        self.assertIn("Invalid sender", body["detail"])
        self.assertIn("from address is not verified", body["detail"])
        self.assertNotIn(ZEPTO_TOKEN, body["detail"], "credentials must never be echoed")

    def test_acknowledgement_failure_does_not_fail_the_request(self) -> None:
        """The team mail is what matters; a bounced ack must not force a resubmit."""
        assert zepto_server is not None
        zepto_server.fail_next = 0
        # Let the first (team) call succeed, then fail the acknowledgement.
        original = zepto_server.next_response
        calls = {"n": 0}

        def sequenced() -> tuple[int, dict]:
            calls["n"] += 1
            if calls["n"] == 2:
                return 400, {"error": {"code": "TM_3201", "message": "Invalid recipient"}}
            return original()

        zepto_server.next_response = sequenced  # type: ignore[method-assign]
        try:
            status, body = post(DEMO, url=ZEPTO_FORMS_URL)
        finally:
            zepto_server.next_response = original  # type: ignore[method-assign]

        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertEqual(len(self.sent), 2)

    def test_validation_still_applies_and_sends_nothing(self) -> None:
        status, _ = post({**DEMO, "email": "not-an-email"}, url=ZEPTO_FORMS_URL)
        self.assertEqual(status, 400)
        self.assertEqual(self.sent, [])


class SenderDomain(unittest.TestCase):
    """ZeptoMail verifies the sending domain; the destination is unrestricted."""

    ENV_KEYS = ("ZEPTOMAIL_TOKEN", "SMTP_HOST", "BB_MAIL_TRANSPORT", "MAIL_FROM",
                "SMTP_FROM", "BB_TEAM_EMAIL", "BB_VERIFIED_SENDER_DOMAINS", "SMTP_USER")

    def _under(self, call: str, **env: str):
        import importlib

        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        # Unset rather than blank: an empty BB_VERIFIED_SENDER_DOMAINS is a
        # deliberate "skip the check", which would hide what we are asserting.
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            return getattr(importlib.reload(request_forms), call)()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(request_forms)

    def test_default_sender_is_the_verified_domain(self) -> None:
        """Defaulting MAIL_FROM to the team inbox would be rejected by ZeptoMail."""
        import importlib

        saved = {k: os.environ.get(k) for k in ("MAIL_FROM", "SMTP_FROM")}
        for k in saved:
            os.environ.pop(k, None)
        try:
            mod = importlib.reload(request_forms)
            self.assertTrue(mod.FROM_EMAIL.endswith("@veltmtours.com"), mod.FROM_EMAIL)
            self.assertEqual(mod.TEAM_EMAIL, "partners@butlerbutton.co")
            self.assertNotEqual(mod.FROM_EMAIL, mod.TEAM_EMAIL)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(request_forms)

    def test_unverified_sender_domain_warns(self) -> None:
        warnings = self._under("config_warnings", ZEPTOMAIL_TOKEN="tok",
                               MAIL_FROM="partners@butlerbutton.co")
        self.assertTrue(any("butlerbutton.co" in w and "verified" in w for w in warnings), warnings)

    def test_verified_sender_domain_does_not_warn(self) -> None:
        warnings = self._under("config_warnings", ZEPTOMAIL_TOKEN="tok",
                               MAIL_FROM="bookings@veltmtours.com")
        self.assertEqual(warnings, [])

    def test_unset_sender_is_flagged_as_a_guess(self) -> None:
        """The default address is invented; it must not pass silently."""
        warnings = self._under("config_warnings", ZEPTOMAIL_TOKEN="tok")
        self.assertTrue(any("MAIL_FROM is not set" in w for w in warnings), warnings)


class ZeptoRegion(unittest.TestCase):
    """A token is region-scoped, so the API host has to match the account."""

    ENV_KEYS = ("ZEPTOMAIL_TOKEN", "ZEPTOMAIL_API_URL", "SMTP_HOST", "SMTP_USER",
                "BB_MAIL_TRANSPORT", "MAIL_FROM", "SMTP_FROM")

    def _under(self, call: str, **env: str):
        import importlib

        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            return getattr(importlib.reload(request_forms), call)()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(request_forms)

    def test_region_is_parsed_from_host_or_url(self) -> None:
        parse = request_forms.zepto_region
        self.assertEqual(parse("smtp.zeptomail.in"), "in")
        self.assertEqual(parse("https://api.zeptomail.in/v1.1/email"), "in")
        self.assertEqual(parse("https://api.zeptomail.eu/v1.1/email"), "eu")
        self.assertEqual(parse("https://api.zeptomail.com/v1.1/email"), "com")
        self.assertEqual(parse("smtp.example.net"), "")
        self.assertEqual(parse(""), "")

    def test_default_api_host_matches_the_account_region(self) -> None:
        """The console shows smtp.zeptomail.in, so .com would fail to authenticate."""
        import importlib

        saved = os.environ.get("ZEPTOMAIL_API_URL")
        os.environ.pop("ZEPTOMAIL_API_URL", None)
        try:
            mod = importlib.reload(request_forms)
            self.assertEqual(mod.zepto_region(mod.ZEPTOMAIL_API_URL), "in", mod.ZEPTOMAIL_API_URL)
        finally:
            if saved is not None:
                os.environ["ZEPTOMAIL_API_URL"] = saved
            importlib.reload(request_forms)

    def test_region_mismatch_warns(self) -> None:
        warnings = self._under(
            "config_warnings", ZEPTOMAIL_TOKEN="tok", MAIL_FROM="x@veltmtours.com",
            ZEPTOMAIL_API_URL="https://api.zeptomail.com/v1.1/email",
            SMTP_HOST="smtp.zeptomail.in",
        )
        self.assertTrue(any("region mismatch" in w for w in warnings), warnings)

    def test_matching_regions_do_not_warn(self) -> None:
        warnings = self._under(
            "config_warnings", ZEPTOMAIL_TOKEN="tok", MAIL_FROM="x@veltmtours.com",
            ZEPTOMAIL_API_URL="https://api.zeptomail.in/v1.1/email",
            SMTP_HOST="smtp.zeptomail.in",
        )
        self.assertEqual(warnings, [])

    def test_zeptomail_smtp_settings_from_the_console_are_accepted(self) -> None:
        """The exact values the ZeptoMail console shows must raise no warnings."""
        warnings = self._under(
            "config_warnings", SMTP_HOST="smtp.zeptomail.in", SMTP_USER="emailapikey",
            MAIL_FROM="partners@veltmtours.com", BB_MAIL_TRANSPORT="smtp",
        )
        self.assertEqual(warnings, [])

    def test_missing_transport_warns(self) -> None:
        warnings = self._under("config_warnings", MAIL_FROM="partners@veltmtours.com")
        self.assertTrue(any("no mail transport" in w for w in warnings), warnings)

    def test_zeptomail_smtp_username_is_checked(self) -> None:
        warnings = self._under("config_warnings", SMTP_HOST="smtp.zeptomail.com",
                               SMTP_USER="partners@veltmtours.com",
                               MAIL_FROM="partners@veltmtours.com")
        self.assertTrue(any("emailapikey" in w for w in warnings), warnings)


class TransportSelection(unittest.TestCase):
    """`active_transport()` picks the right sender for a given configuration."""

    ENV_KEYS = ("ZEPTOMAIL_TOKEN", "SMTP_HOST", "BB_MAIL_TRANSPORT")

    def _under(self, call: str, **env: str):
        """Evaluate `request_forms.<call>()` with the module reloaded under `env`.

        The module reads its config at import time, so the value has to be taken
        while the environment is applied — reload() mutates in place, and the
        restoring reload at the end would otherwise undo what we are asserting.
        """
        import importlib

        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ.update({k: "" for k in self.ENV_KEYS})
        os.environ.update(env)
        try:
            mod = importlib.reload(request_forms)
            return getattr(mod, call)()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(request_forms)

    def test_token_alone_selects_the_api(self) -> None:
        self.assertEqual(self._under("active_transport", ZEPTOMAIL_TOKEN="tok"), "zeptomail")

    def test_smtp_host_alone_selects_smtp(self) -> None:
        self.assertEqual(self._under("active_transport", SMTP_HOST="smtp.zeptomail.com"), "smtp")

    def test_api_wins_when_both_are_set(self) -> None:
        self.assertEqual(
            self._under("active_transport", ZEPTOMAIL_TOKEN="tok", SMTP_HOST="smtp.zeptomail.com"),
            "zeptomail",
        )

    def test_explicit_smtp_overrides_the_token(self) -> None:
        self.assertEqual(
            self._under("active_transport", ZEPTOMAIL_TOKEN="tok",
                        SMTP_HOST="smtp.zeptomail.com", BB_MAIL_TRANSPORT="smtp"),
            "smtp",
        )

    def test_nothing_configured(self) -> None:
        self.assertEqual(self._under("active_transport"), "")

    def test_token_pasted_with_its_prefix_is_not_doubled(self) -> None:
        self.assertEqual(
            self._under("_zepto_auth_header", ZEPTOMAIL_TOKEN="Zoho-enczapikey abc123"),
            "Zoho-enczapikey abc123",
        )
        self.assertEqual(
            self._under("_zepto_auth_header", ZEPTOMAIL_TOKEN="abc123"),
            "Zoho-enczapikey abc123",
        )


class RateLimit(unittest.TestCase):
    def test_rate_limit_kicks_in(self) -> None:
        inbox().clear()
        seen = [post(DEMO, url=RATE_FORMS_URL)[0] for _ in range(RATE_MAX + 2)]
        self.assertEqual(seen[:RATE_MAX], [200] * RATE_MAX, seen)
        self.assertEqual(seen[RATE_MAX:], [429, 429], seen)
        self.assertEqual(len(inbox()), RATE_MAX * 2, "throttled posts must not be mailed")


class PageWiring(unittest.TestCase):
    """The page posts to the Veltm Edge Function; this server sends no mail for it."""

    # The field names butler-demo-request documents.
    API_FIELDS = {
        "full_name", "work_email", "property_name", "city_country", "role", "phone",
        "property_type", "rooms", "notes", "source", "page_url", "company_website",
    }
    @classmethod
    def setUpClass(cls) -> None:
        with urllib.request.urlopen(f"{BASE}/hotel-concierge-program.html", timeout=10) as res:
            cls.html = res.read().decode()

    def test_the_demo_button_is_wired(self) -> None:
        self.assertIn('data-request="demo"', self.html)
        self.assertIn('id="rqForm"', self.html)

    def test_the_15_minute_meeting_option_is_gone(self) -> None:
        """One way in: "Book a demo". No meeting button, and no form left behind it."""
        self.assertEqual(re.findall(r'data-request="(\w+)"', self.html), ["demo"])
        self.assertNotIn("15-minute", self.html)
        self.assertNotIn("preferred_date", self.html)
        self.assertNotIn("preferred_time", self.html)

    def test_dropdown_lists_have_solid_colours(self) -> None:
        """The browser draws a select's open list itself. Over a see-through
        background some draw it white, and our white text vanished on it."""
        option = re.search(r"\.rq-f select option\{([^}]*)\}", self.html)
        self.assertIsNotNone(option, "no colours set for the dropdown's options")
        self.assertRegex(option.group(1), r"background-color:#[0-9A-Fa-f]{6}\b")
        self.assertIn("color:var(--white)", option.group(1))
        select = re.search(r"\.rq-f select\{([^}]*)\}", self.html)
        self.assertIsNotNone(select, "the select itself has no solid background")
        self.assertRegex(select.group(1), r"background-color:#[0-9A-Fa-f]{6}\b")

    def test_posts_to_the_edge_function_with_the_anon_key(self) -> None:
        self.assertIn("functions/v1/butler-demo-request", self.html)
        self.assertIn("'apikey': API.anon", self.html)
        self.assertIn("'Authorization': 'Bearer ' + API.anon", self.html)
        # The old same-origin mail endpoint must no longer be the target.
        self.assertNotIn("window.BB_FORMS_API", self.html)

    # ── "Book a demo": a time picker in the same pop-up ──────────────────
    # The booking itself is the Veltm butler-demo-booking Edge Function, which
    # uses the Google account the Veltm Tours app is already connected to.
    # This site holds no Google keys and has no booking endpoint of its own.

    def test_the_demo_button_reads_book_a_demo(self) -> None:
        self.assertRegex(self.html, r'data-request="demo"[^>]*>Book a demo<')

    def test_booking_goes_to_the_veltm_api_with_the_anon_key(self) -> None:
        self.assertIn("functions/v1/butler-demo-booking", self.html)
        # Local previews reach it through this server's /api/* proxy.
        self.assertIn("location.origin + '/api/butler-demo-booking'", self.html)
        calls = re.findall(r"fetch\(BOOKING_URL, \{(.*?)\}\)", self.html, re.S)
        self.assertEqual(len(calls), 2, "expected one call for open times and one to book")
        for call in calls:
            self.assertIn("apiHeaders(", call)
        # Nothing may still point at a booking endpoint on this server.
        self.assertNotIn("/booking/slots", self.html)
        self.assertNotIn("/booking/book", self.html)

    def test_the_lead_is_still_sent_after_a_booking(self) -> None:
        booked = re.search(r"function booked\(.*?\n  \}", self.html, re.S)
        self.assertIsNotNone(booked, "booked() not found in the page")
        self.assertIn("postLead(data, true)", booked.group(0))

    def test_every_field_a_booking_posts_is_one_the_api_reads(self) -> None:
        block = re.search(r"var BOOK_FIELDS = \[(.*?)\];", self.html, re.S)
        self.assertIsNotNone(block, "BOOK_FIELDS not found in the page")
        posted = set(re.findall(r"'(\w+)'", block.group(1)))
        self.assertTrue({"name", "email", "company", "location"} <= posted, posted)
        # FIELDS in the Veltm repo's supabase/functions/_shared/demoBooking.ts.
        api_reads = {"name", "email", "phone", "company", "role", "location",
                     "property_type", "rooms", "timezone", "message", "page"}
        self.assertEqual(posted - api_reads, set())

    def test_days_are_grouped_in_the_visitors_zone_not_utc(self) -> None:
        grouping = re.search(r"function groupSlots\(.*?\n  \}", self.html, re.S)
        self.assertIsNotNone(grouping, "groupSlots not found in the page")
        self.assertIn("timeZone", grouping.group(0))
        self.assertNotIn("toISOString", grouping.group(0))

    def test_the_picker_adds_no_named_inputs(self) -> None:
        """Named inputs are sent as lead fields; the picker keeps its choice in JS."""
        picker = re.search(r'<div[^>]*id="rqSlots".*?</div>\s*\n\s*<div class="rq-f">',
                           self.html, re.S)
        self.assertIsNotNone(picker, "slot picker not found in the page")
        self.assertIsNone(re.search(r"<(input|select|textarea)\b", picker.group(0)))

    def test_honeypot_is_named_and_kept_off_screen(self) -> None:
        self.assertIn('name="company_website"', self.html)
        hp = re.search(r"\.rq-hp\{([^}]*)\}", self.html)
        self.assertIsNotNone(hp)
        self.assertIn("left:-9999px", hp.group(1))
        self.assertNotIn("display:none", hp.group(1),
                         "bots skip display:none, which defeats the honeypot")

    def _field_map(self) -> dict:
        """The FIELD_MAP object literal from the page, as a dict."""
        block = re.search(r"var FIELD_MAP = \{(.*?)\n  \};", self.html, re.S)
        self.assertIsNotNone(block, "FIELD_MAP not found in the page")
        return dict(re.findall(r"(\w+):\s*'(\w+)'", block.group(1)))

    def test_every_input_is_sent_somewhere(self) -> None:
        inputs = set(re.findall(
            r'<(?:input|select|textarea)[^>]*\sname="([^"]+)"', self.html))
        our_names = set(self._field_map())
        unaccounted = inputs - our_names - {"company_website"}
        self.assertEqual(unaccounted, set(), f"inputs sent nowhere: {sorted(unaccounted)}")

    def test_mapped_targets_are_all_known_api_fields(self) -> None:
        mapped = self._field_map()
        self.assertTrue(mapped, "field map not found in the page")
        unknown = set(mapped.values()) - self.API_FIELDS
        self.assertEqual(unknown, set(), f"not in the API contract: {sorted(unknown)}")
        # The four required fields must all be produced.
        for required in ("full_name", "work_email", "property_name", "city_country"):
            self.assertIn(required, mapped.values(), f"{required} is never sent")


class PageCopy(unittest.TestCase):
    """What the landing page may and may not say."""

    @classmethod
    def setUpClass(cls) -> None:
        with urllib.request.urlopen(f"{BASE}/hotel-concierge-program.html", timeout=10) as res:
            html = res.read().decode()
        # The words a visitor can read: no scripts, styles or inline image data.
        text = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
        cls.text = re.sub(r'src="data:[^"]*"', "", text).lower()

    def test_the_page_publishes_no_pricing(self) -> None:
        """Price is a conversation for the demo. The site states no price, no
        fee and no billing terms, and has no pricing section to link to.
        (Third-party figures on what a disrupted trip costs a traveler stay.)"""
        for banned in ("us$", "$99", "pricing", "setup fee", "per month", "a month",
                       "/ month", "/month", "per-guest"):
            # assertFalse, not assertNotIn: a failure should name the phrase,
            # not print the whole page.
            self.assertFalse(banned in self.text, f"pricing language on the page: {banned!r}")
        self.assertFalse('href="#terms"' in self.text, "a link to the old pricing section")

    def test_every_in_page_link_has_a_target(self) -> None:
        ids = set(re.findall(r'\bid="([^"]+)"', self.text))
        for anchor in set(re.findall(r'href="#([^"]+)"', self.text)):
            self.assertIn(anchor, ids, f"#{anchor} points at nothing")


class ProductAndProof(unittest.TestCase):
    """The product simulator ("See it work") and the proof points."""

    MESSAGES = (
        "airline data shows B6 1234 JFK-BOS delayed about 2 hrs, now departing 9:40 PM.",
        "airline data shows UA 2381 ORD-MHT on Fri Nov 14 as CANCELLED.",
        "airline data shows WN 1532 BWI-MHT has been DIVERTED to Boston Logan (BOS).",
        "the National Weather Service has issued a Winter Storm Warning for the area until Sat 7:00 AM.",
    )

    @classmethod
    def setUpClass(cls) -> None:
        with urllib.request.urlopen(f"{BASE}/hotel-concierge-program.html", timeout=10) as res:
            cls.html = res.read().decode()

    def _section(self, section_id: str) -> str:
        found = re.findall(rf'<section\b[^>]*\bid="{section_id}".*?</section>', self.html, re.S)
        self.assertEqual(len(found), 1, f'expected exactly one <section id="{section_id}">')
        return found[0]

    @staticmethod
    def _words(markup: str) -> str:
        """The text a visitor reads: inline tags vanish, block tags become a space."""
        markup = re.sub(r"</?(?:mark|span|b|i|em|small|strong)\b[^>]*>", "", markup)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))

    def test_the_sections_sit_where_the_page_needs_them(self) -> None:
        """Product straight after "Meet Butler Button"; proof before onboarding."""
        self._section("product")
        self._section("proof")
        order = [self.html.index(f'id="{name}"') for name in
                 ("solution", "product", "how", "alerts", "proof", "onboard")]
        self.assertEqual(order, sorted(order))
        nav = re.search(r'<div class="nav-links">.*?</div>', self.html, re.S)
        self.assertIsNotNone(nav, "nav links not found")
        self.assertIn('href="#product"', nav.group(0))

    def test_every_scenario_is_in_the_markup_itself(self) -> None:
        """The simulator is an enhancement. Each scenario's text is plain HTML,
        so the section is complete before, and without, any script."""
        words = self._words(self._section("product"))
        for message in self.MESSAGES:
            self.assertIn(message, words)

    def test_the_simulator_quotes_the_pages_own_example_messages(self) -> None:
        """One wording per alert across the page. The hero thread carries the
        delayed-flight text; the simulator must not drift from it."""
        hero = re.search(r'<div class="thread-body">.*?</div>\s*</div>', self.html, re.S)
        self.assertIsNotNone(hero, "hero conversation not found")
        self.assertIn(self.MESSAGES[0], self._words(hero.group(0)))

    def test_one_scenario_shows_by_default(self) -> None:
        product = self._section("product")
        buttons = re.findall(
            r'<button type="button" class="pv-tab" data-pv="(\w+)" aria-pressed="(true|false)"', product)
        self.assertEqual([name for name, _ in buttons], ["delayed", "cancelled", "diverted", "storm"])
        self.assertEqual([name for name, pressed in buttons if pressed == "true"], ["diverted"])
        self.assertIn('data-pv-scene="diverted"', product)

    def test_made_up_examples_are_labelled(self) -> None:
        self.assertIn("Illustrative example. Guest names and flights are made up.",
                      self._words(self._section("product")))

    def test_proof_draws_the_same_check_schedule_the_page_states(self) -> None:
        """One schedule, stated in Travel alerts and drawn in the proof section.
        If it ever changes, both have to."""
        stated = re.search(
            r"checks live status (\d+) hours, (\d+) hours, (\d+) hours and (\d+) minutes "
            r"before departure, then every (\d+) minutes once a flight is late",
            self._words(self._section("alerts")))
        self.assertIsNotNone(stated, "the schedule sentence in Travel alerts changed shape")
        proof = self._section("proof")
        drawn = re.findall(r'<li class="pf-c"><b>(\d+)<small>(hours|minutes)</small>', proof)
        self.assertEqual([number for number, _ in drawn], list(stated.groups()[:4]))
        self.assertEqual([unit for _, unit in drawn], ["hours", "hours", "hours", "minutes"])
        self.assertIn(f"+{stated.group(5)}<small>minutes</small>", proof)
        self.assertIn(f"every {stated.group(5)} minutes", self._words(proof))

    def test_proof_ends_on_the_demo(self) -> None:
        proof = self._section("proof")
        self.assertRegex(proof, r'<a class="btn btn-primary" href="#demo">Book a demo</a>')
        self.assertIn("demo mode", self._words(proof))


class CacheHeaders(unittest.TestCase):
    """Pages revalidate on every visit so a deploy shows up at once; assets keep the default."""

    @staticmethod
    def _cache_control(path: str) -> str:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as res:
            return res.headers.get("Cache-Control", "")

    def test_pages_are_not_served_stale(self) -> None:
        for path in ("/", "/hotel-concierge-program.html", "/hotel-concierge-program.html?v=2"):
            self.assertEqual(self._cache_control(path), "no-cache", path)

    def test_assets_are_left_alone(self) -> None:
        self.assertNotIn("no-cache", self._cache_control("/assets/logo.svg"))

    def test_head_root_describes_the_landing_page(self) -> None:
        def head(path: str) -> tuple:
            req = urllib.request.Request(f"{BASE}{path}", method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.headers.get("Content-Length"), res.headers.get("Cache-Control")
        with urllib.request.urlopen(f"{BASE}/", timeout=10) as res:
            body_len = str(len(res.read()))
        self.assertEqual(head("/"), (body_len, "no-cache"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
