#!/usr/bin/env python3
"""
Tests for demo-call booking (tools/booking.py and its routes in tools/serve.py).

Two halves:

  * the slot maths, in-process with a fixed clock, so the daylight-saving weeks
    can be tested whatever today's date is, and
  * the HTTP flow, against real `tools/serve.py` processes pointed at a stub
    that stands in for Google's token endpoint and Calendar API.

Nothing here reaches Google. Standard library only.

Usage:
    python3 tools/test_booking.py [-v]
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

BOOKING_ENV = (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
    "BB_BOOKING_ENABLED", "BB_BOOKING_CALENDAR_ID", "BB_BOOKING_TEAM_GUESTS",
    "BB_BOOKING_TZ", "BB_BOOKING_HOURS", "BB_BOOKING_WEEKDAYS", "BB_BOOKING_MINUTES",
    "BB_BOOKING_MIN_NOTICE_HOURS", "BB_BOOKING_DAYS_AHEAD", "BB_BOOKING_DAILY_MAX",
    "BB_BOOKING_RATE_MAX", "BB_BOOKING_RATE_WINDOW", "BB_BOOKING_CACHE_SECONDS",
    "BB_GOOGLE_TOKEN_URL", "BB_GOOGLE_API_BASE", "BB_GOOGLE_TIMEOUT",
)
# The defaults are what is under test, whatever the developer's shell holds.
for _key in BOOKING_ENV:
    os.environ.pop(_key, None)
import booking  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
TEAM = ["ansh@team.example", "amruta@team.example", "carl@team.example"]


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@contextlib.contextmanager
def configured(**env: str):
    """`booking` reloaded under these variables, restored afterwards."""
    saved = {k: os.environ.get(k) for k in BOOKING_ENV}
    for k in BOOKING_ENV:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        yield importlib.reload(booking)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(booking)


# ── Slot maths ───────────────────────────────────────────────────────────────


class SlotMaths(unittest.TestCase):
    """08:00-19:00 US Eastern, Monday to Friday, 30 minutes, 4 hours' notice."""

    def _by_day(self, slots):
        days: dict = {}
        for slot in slots:
            days.setdefault(slot.astimezone(EASTERN).date().isoformat(), []).append(slot)
        return days

    def test_a_weekday_runs_eight_to_half_past_six_eastern(self) -> None:
        # Sunday midday UTC; Monday 9 November 2026 is the first bookable day.
        days = self._by_day(booking.slot_starts(utc("2026-11-08T12:00:00")))
        monday = days["2026-11-09"]
        self.assertEqual(len(monday), 22)
        self.assertEqual(monday[0], utc("2026-11-09T13:00:00"))    # 08:00 EST
        self.assertEqual(monday[-1], utc("2026-11-09T23:30:00"))   # 18:30 EST, ends 19:00

    def test_every_slot_is_inside_the_window(self) -> None:
        for slot in booking.slot_starts(utc("2026-11-08T12:00:00")):
            local = slot.astimezone(EASTERN)
            self.assertLess(local.weekday(), 5, local)
            self.assertIn(local.minute, (0, 30), local)
            self.assertGreaterEqual((local.hour, local.minute), (8, 0), local)
            self.assertLessEqual((local.hour, local.minute), (18, 30), local)

    def test_weekends_are_never_offered(self) -> None:
        days = self._by_day(booking.slot_starts(utc("2026-11-08T12:00:00")))
        self.assertNotIn("2026-11-14", days)  # Saturday
        self.assertNotIn("2026-11-15", days)  # Sunday

    def test_horizon_is_thirty_days_counted_in_eastern(self) -> None:
        days = self._by_day(booking.slot_starts(utc("2026-11-09T15:00:00")))
        self.assertIn("2026-12-09", days)      # Wednesday, day 30
        self.assertNotIn("2026-12-10", days)

    def test_minimum_notice_is_four_hours(self) -> None:
        # 09:10 Eastern on a Monday: 13:10 is too soon, 13:30 is the first.
        slots = booking.slot_starts(utc("2026-11-09T14:10:00"))
        self.assertEqual(slots[0], utc("2026-11-09T18:30:00"))

    def test_a_slot_exactly_at_the_notice_boundary_is_offered(self) -> None:
        slots = booking.slot_starts(utc("2026-11-09T14:30:00"))
        self.assertEqual(slots[0], utc("2026-11-09T18:30:00"))

    def test_after_closing_the_next_day_opens(self) -> None:
        slots = booking.slot_starts(utc("2026-11-09T23:45:00"))
        self.assertEqual(slots[0], utc("2026-11-10T13:00:00"))

    def test_now_on_a_weekend_starts_on_monday(self) -> None:
        slots = booking.slot_starts(utc("2026-11-14T16:00:00"))
        self.assertEqual(slots[0], utc("2026-11-16T13:00:00"))

    def test_clocks_going_forward(self) -> None:
        """US clocks change on Sunday 14 March 2027: 8 AM moves from 13:00Z to 12:00Z."""
        days = self._by_day(booking.slot_starts(utc("2027-03-10T12:00:00")))
        self.assertEqual(days["2027-03-12"][0], utc("2027-03-12T13:00:00"))
        self.assertEqual(days["2027-03-15"][0], utc("2027-03-15T12:00:00"))
        self.assertEqual(len(days["2027-03-12"]), 22)
        self.assertEqual(len(days["2027-03-15"]), 22)

    def test_clocks_going_back(self) -> None:
        """...and on Sunday 7 November 2027 it moves back to 13:00Z."""
        days = self._by_day(booking.slot_starts(utc("2027-11-03T12:00:00")))
        self.assertEqual(days["2027-11-05"][0], utc("2027-11-05T12:00:00"))
        self.assertEqual(days["2027-11-08"][0], utc("2027-11-08T13:00:00"))
        self.assertEqual(len(days["2027-11-05"]), 22)
        self.assertEqual(len(days["2027-11-08"]), 22)

    def test_a_wall_clock_time_that_never_happens_is_not_offered(self) -> None:
        # With hours set across the 02:00-03:00 gap on the change day itself.
        with configured(BB_BOOKING_HOURS="01:00-04:00", BB_BOOKING_WEEKDAYS="sun",
                        BB_BOOKING_MIN_NOTICE_HOURS="0") as mod:
            slots = [s for s in mod.slot_starts(utc("2027-03-13T12:00:00"))
                     if s.astimezone(EASTERN).date().isoformat() == "2027-03-14"]
        times = [s.astimezone(EASTERN).strftime("%H:%M") for s in slots]
        self.assertEqual(times, ["01:00", "01:30", "03:00", "03:30"])
        self.assertEqual(len(set(slots)), len(slots))

    def test_a_slot_length_that_does_not_divide_the_day_never_overruns(self) -> None:
        with configured(BB_BOOKING_MINUTES="45") as mod:
            monday = [s for s in mod.slot_starts(utc("2026-11-08T12:00:00"))
                      if s.astimezone(EASTERN).date().isoformat() == "2026-11-09"]
        last = monday[-1].astimezone(EASTERN) + timedelta(minutes=45)
        self.assertLessEqual((last.hour, last.minute), (19, 0))


class BusyOverlap(unittest.TestCase):
    def setUp(self) -> None:
        self.slots = [s for s in booking.slot_starts(utc("2026-11-08T12:00:00"))
                      if s < utc("2026-11-10T00:00:00")]   # Monday 9 November

    def _left(self, *busy):
        periods = [(booking.parse_instant(a), booking.parse_instant(b)) for a, b in busy]
        return [s.astimezone(EASTERN).strftime("%H:%M")
                for s in booking.remove_busy(self.slots, periods)]

    def test_a_meeting_blocks_only_its_own_slot(self) -> None:
        left = self._left(("2026-11-09T09:00:00-05:00", "2026-11-09T09:30:00-05:00"))
        self.assertNotIn("09:00", left)
        self.assertIn("08:30", left)   # ends as the meeting starts
        self.assertIn("09:30", left)   # starts as the meeting ends
        self.assertEqual(len(left), 21)

    def test_a_meeting_straddling_two_slots_blocks_both(self) -> None:
        left = self._left(("2026-11-09T14:15:00Z", "2026-11-09T14:45:00Z"))
        self.assertNotIn("09:00", left)
        self.assertNotIn("09:30", left)
        self.assertEqual(len(left), 20)

    def test_a_block_over_several_days_clears_the_day(self) -> None:
        self.assertEqual(self._left(("2026-11-08T00:00:00Z", "2026-11-11T00:00:00Z")), [])

    def test_offsets_other_than_utc_are_understood(self) -> None:
        # 19:30-20:00 in India is 09:00-09:30 Eastern.
        left = self._left(("2026-11-09T19:30:00+05:30", "2026-11-09T20:00:00+05:30"))
        self.assertNotIn("09:00", left)
        self.assertEqual(len(left), 21)

    def test_timestamps_without_an_offset_are_refused(self) -> None:
        self.assertIsNone(booking.parse_instant("2026-11-09T09:00:00"))
        self.assertIsNone(booking.parse_instant("next tuesday"))
        self.assertIsNone(booking.parse_instant(None))


class Config(unittest.TestCase):
    def test_off_until_google_is_connected(self) -> None:
        self.assertFalse(booking.is_configured())
        status, body = booking.handle_slots()
        self.assertEqual((status, body["success"], body["error"]),
                         (200, False, "booking_not_configured"))
        self.assertNotIn("slots", body)
        self.assertEqual(booking.handle_book({})[0], 503)
        self.assertTrue(any("Google is not connected" in w for w in booking.config_warnings()))

    def test_partial_credentials_stay_off_and_say_what_is_missing(self) -> None:
        with configured(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret") as mod:
            self.assertFalse(mod.is_configured())
            self.assertTrue(any("GOOGLE_REFRESH_TOKEN" in w for w in mod.config_warnings()))

    def test_all_three_credentials_turn_it_on(self) -> None:
        with configured(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret",
                        GOOGLE_REFRESH_TOKEN="token",
                        BB_BOOKING_TEAM_GUESTS=", ".join(TEAM)) as mod:
            self.assertTrue(mod.is_configured())
            self.assertEqual(mod.config_warnings(), [])
            self.assertEqual(list(mod.TEAM_GUESTS), TEAM)

    def test_the_off_switch_wins(self) -> None:
        with configured(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret",
                        GOOGLE_REFRESH_TOKEN="token", BB_BOOKING_ENABLED="0") as mod:
            self.assertFalse(mod.is_configured())

    def test_no_team_guests_is_flagged(self) -> None:
        with configured(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret",
                        GOOGLE_REFRESH_TOKEN="token") as mod:
            self.assertTrue(any("BB_BOOKING_TEAM_GUESTS" in w for w in mod.config_warnings()))

    def test_a_bad_setting_turns_booking_off_rather_than_guessing(self) -> None:
        for env in ({"BB_BOOKING_HOURS": "19:00-08:00"}, {"BB_BOOKING_WEEKDAYS": "weekdays"},
                    {"BB_BOOKING_MINUTES": "half an hour"},
                    {"BB_BOOKING_TEAM_GUESTS": "not-an-address"},
                    {"BB_BOOKING_TZ": "Mars/Olympus_Mons"}):
            with configured(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret",
                            GOOGLE_REFRESH_TOKEN="token", **env) as mod:
                self.assertFalse(mod.is_configured(), env)
                self.assertTrue(mod.config_warnings(), env)

    def test_weekday_and_hour_parsing(self) -> None:
        self.assertEqual(booking.parse_weekdays("mon-fri"), frozenset(range(5)))
        self.assertEqual(booking.parse_weekdays("Mon, Wed,fri"), frozenset({0, 2, 4}))
        self.assertEqual(booking.parse_weekdays("sat-mon"), frozenset({5, 6, 0}))
        self.assertEqual(booking.parse_hours("08:00-19:00"), (480, 1140))
        self.assertEqual(booking.parse_hours("9-17:30"), (540, 1050))


class EventBody(unittest.TestCase):
    """What Google is asked to put in the invitation."""

    def _event(self):
        with configured(BB_BOOKING_TEAM_GUESTS=",".join(TEAM)) as mod:
            start = utc("2026-11-12T15:00:00")
            return mod.build_event(mod.event_id_for(start, "ana@hotel.example"),
                                   "BB-CALL-20261111-ABCDEF", start, "ana@hotel.example")

    def test_times_are_eastern_with_an_explicit_zone(self) -> None:
        event = self._event()
        self.assertEqual(event["start"], {"dateTime": "2026-11-12T10:00:00-05:00",
                                          "timeZone": "America/New_York"})
        self.assertEqual(event["end"]["dateTime"], "2026-11-12T10:30:00-05:00")

    def test_the_visitor_and_the_team_are_invited_but_cannot_see_each_other(self) -> None:
        event = self._event()
        self.assertEqual([a["email"] for a in event["attendees"]], ["ana@hotel.example"] + TEAM)
        self.assertIs(event["guestsCanSeeOtherGuests"], False)
        self.assertIs(event["guestsCanInviteOthers"], False)
        self.assertIs(event["guestsCanModify"], False)

    def test_a_meet_room_is_requested(self) -> None:
        request = self._event()["conferenceData"]["createRequest"]
        self.assertEqual(request["conferenceSolutionKey"], {"type": "hangoutsMeet"})
        self.assertEqual(request["requestId"], "BB-CALL-20261111-ABCDEF")

    def test_a_team_member_booking_is_not_invited_twice(self) -> None:
        with configured(BB_BOOKING_TEAM_GUESTS=",".join(TEAM)) as mod:
            start = utc("2026-11-12T15:00:00")
            event = mod.build_event("bbid", "BB-CALL-X", start, TEAM[1])
        self.assertEqual(sorted(a["email"] for a in event["attendees"]), sorted(TEAM))

    def test_event_ids_are_stable_and_valid(self) -> None:
        start = utc("2026-11-12T15:00:00")
        first = booking.event_id_for(start, "ana@hotel.example")
        self.assertEqual(first, booking.event_id_for(start, "ana@hotel.example"))
        self.assertNotEqual(first, booking.event_id_for(start, "bo@hotel.example"))
        self.assertNotEqual(first, booking.event_id_for(start + timedelta(minutes=30),
                                                        "ana@hotel.example"))
        # Google accepts base32hex only: a-v and digits, 5 to 1024 characters.
        self.assertRegex(first, r"^[a-v0-9]{5,1024}$")


# ── Stub Google ──────────────────────────────────────────────────────────────


class _GoogleHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass  # the caller gave up waiting, which is the point of that test

    def _record(self, body) -> dict:
        parts = urllib.parse.urlsplit(self.path)
        entry = {
            "method": self.command,
            "path": parts.path,
            "query": dict(urllib.parse.parse_qsl(parts.query)),
            "auth": self.headers.get("Authorization", ""),
            "body": body,
        }
        self.server.received.append(entry)  # type: ignore[attr-defined]
        return entry

    def do_POST(self):  # noqa: N802
        stub = self.server
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/token"):
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            self._record(form)
            if self.path.startswith("/token-revoked"):
                self._send(400, {"error": "invalid_grant",
                                 "error_description": "Token has been expired or revoked."})
                return
            stub.tokens += 1  # type: ignore[attr-defined]
            self._send(200, {"access_token": f"stub-access-{stub.tokens}",  # type: ignore[attr-defined]
                             "expires_in": 3599, "token_type": "Bearer"})
            return

        body = json.loads(raw or b"{}")
        entry = self._record(body)
        if not entry["auth"].startswith("Bearer stub-access-"):
            self._send(401, {"error": {"code": 401, "message": "Invalid Credentials",
                                       "errors": [{"reason": "authError"}]}})
            return

        if entry["path"].endswith("/freeBusy"):
            calendar = body["items"][0]["id"]
            if stub.freebusy_errors:  # type: ignore[attr-defined]
                self._send(200, {"kind": "calendar#freeBusy", "calendars": {
                    calendar: {"errors": [{"domain": "global", "reason": "notFound"}]}}})
                return
            self._send(200, {"kind": "calendar#freeBusy", "calendars": {
                calendar: {"busy": stub.busy_periods()}}})  # type: ignore[attr-defined]
            return

        if entry["path"].endswith("/events"):
            mode = stub.insert_mode  # type: ignore[attr-defined]
            if mode == "quota":
                self._send(403, {"error": {"code": 403,
                                           "message": "Calendar usage limits exceeded.",
                                           "errors": [{"domain": "usageLimits",
                                                       "reason": "quotaExceeded"}]}})
                return
            if mode == "fail":
                self._send(503, {"error": {"code": 503, "message": "Backend Error",
                                           "errors": [{"reason": "backendError"}]}})
                return
            if body["id"] in stub.events:  # type: ignore[attr-defined]
                self._send(409, {"error": {"code": 409,
                                           "message": "The requested identifier already exists.",
                                           "errors": [{"reason": "duplicate"}]}})
                return
            event = dict(body)
            event["status"] = "confirmed"
            link = f"https://meet.google.com/stub-{len(stub.events) + 1:04d}"  # type: ignore[attr-defined]
            request_id = body["conferenceData"]["createRequest"]["requestId"]
            if mode in ("pending", "pending_forever"):
                event["conferenceData"] = {"createRequest": {
                    "requestId": request_id, "status": {"statusCode": "pending"}}}
                if mode == "pending":
                    stub.link_on_read[body["id"]] = link  # type: ignore[attr-defined]
            else:
                event["hangoutLink"] = link
                event["conferenceData"] = {
                    "createRequest": {"requestId": request_id,
                                      "status": {"statusCode": "success"}},
                    "entryPoints": [{"entryPointType": "video", "uri": link}],
                }
            stub.events[body["id"]] = event  # type: ignore[attr-defined]
            if mode == "hang":
                time.sleep(2.5)  # created, but the answer never arrives in time
            self._send(200, event)
            return

        self._send(404, {"error": {"code": 404, "message": "Not Found"}})

    def do_GET(self):  # noqa: N802
        stub = self.server
        entry = self._record(None)
        event_id = urllib.parse.unquote(entry["path"].rsplit("/", 1)[-1])
        event = stub.events.get(event_id)  # type: ignore[attr-defined]
        if event is None:
            self._send(404, {"error": {"code": 404, "message": "Not Found",
                                       "errors": [{"reason": "notFound"}]}})
            return
        link = stub.link_on_read.pop(event_id, None)  # type: ignore[attr-defined]
        if link:
            event["hangoutLink"] = link
            event["conferenceData"]["createRequest"]["status"] = {"statusCode": "success"}
        self._send(200, event)


class StubGoogle(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _GoogleHandler)
        self.tokens = 0
        self.reset()

    def reset(self) -> None:
        self.received: list[dict] = []
        self.events: dict[str, dict] = {}
        self.extra_busy: list[dict] = []
        self.link_on_read: dict[str, str] = {}
        self.freebusy_errors = False
        self.insert_mode = "ok"

    def busy_periods(self) -> list[dict]:
        periods = list(self.extra_busy)
        for event in self.events.values():
            if event.get("status") != "cancelled":
                periods.append({"start": event["start"]["dateTime"],
                                "end": event["end"]["dateTime"]})
        return periods

    def calls(self, method: str, suffix: str) -> list[dict]:
        return [r for r in self.received if r["method"] == method and r["path"].endswith(suffix)]

    def inserts(self) -> list[dict]:
        return self.calls("POST", "/events")


# ── Fixtures ─────────────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GOOGLE_PORT = free_port()
PORTS = {name: free_port() for name in ("main", "off", "rate", "cap", "quota", "auth")}
google: StubGoogle | None = None
servers: list[subprocess.Popen] = []


def url(server: str, path: str) -> str:
    return f"http://127.0.0.1:{PORTS[server]}{path}"


def _start_server(name: str, **overrides: str) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in BOOKING_ENV}
    env.update({
        "WEB_ROOT": "proposed",
        "INDEX_FILE": "hotel-concierge-program.html",
        "ZEPTOMAIL_TOKEN": "",
        "SMTP_HOST": "",
        "GOOGLE_CLIENT_ID": "client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "client-secret",
        "GOOGLE_REFRESH_TOKEN": "refresh-token",
        "BB_GOOGLE_TOKEN_URL": f"http://127.0.0.1:{GOOGLE_PORT}/token",
        "BB_GOOGLE_API_BASE": f"http://127.0.0.1:{GOOGLE_PORT}/calendar/v3",
        "BB_GOOGLE_TIMEOUT": "1",
        "BB_BOOKING_TEAM_GUESTS": ",".join(TEAM),
        "BB_BOOKING_CACHE_SECONDS": "0",
        "BB_BOOKING_RATE_MAX": "500",
        "BB_BOOKING_DAILY_MAX": "500",
    })
    env.update(overrides)
    for key in [k for k, v in env.items() if k in BOOKING_ENV and v == ""]:
        del env[key]
    proc = subprocess.Popen(
        [sys.executable, os.path.join("tools", "serve.py"), str(PORTS[name])],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORTS[name]), timeout=0.5):
                return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"serve.py exited: {proc.communicate()[1].decode()}")
            time.sleep(0.15)
    raise RuntimeError("serve.py did not start in time")


def setUpModule() -> None:  # noqa: N802
    global google
    google = StubGoogle(GOOGLE_PORT)
    threading.Thread(target=google.serve_forever, daemon=True).start()
    servers.append(_start_server("main"))
    servers.append(_start_server("off", GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="",
                                 GOOGLE_REFRESH_TOKEN=""))
    servers.append(_start_server("rate", BB_BOOKING_RATE_MAX="2"))
    servers.append(_start_server("cap", BB_BOOKING_DAILY_MAX="1"))
    servers.append(_start_server("quota"))
    servers.append(_start_server(
        "auth", BB_GOOGLE_TOKEN_URL=f"http://127.0.0.1:{GOOGLE_PORT}/token-revoked"))


def tearDownModule() -> None:  # noqa: N802
    for proc in servers:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    if google is not None:
        google.shutdown()
        google.server_close()


def call(method: str, target: str, payload=None, headers: dict | None = None,
         raw: bytes | None = None) -> tuple[int, dict]:
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(target, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def stub() -> StubGoogle:
    assert google is not None
    return google


def open_times(server: str = "main") -> list[str]:
    status, body = call("GET", url(server, "/booking/slots"))
    assert status == 200, body
    return body["slots"]


VISITOR = {
    "name": "José Ferreira",
    "email": "jose@thebeachhouse.example",
    "phone": "+351 912 345 678",
    "company": "The Beach House",
    "role": "General Manager",
    "location": "Lisbon, Portugal",
    "property_type": "Hotel",
    "rooms": "48",
    "message": "Most guest questions are about restaurants and airport transfers.",
    "timezone": "Europe/Lisbon",
    "page": "/hotel-concierge-program.html",
}


def book(start: str, server: str = "main", headers: dict | None = None, **changes):
    return call("POST", url(server, "/booking/book"), {**VISITOR, "start": start, **changes},
                headers=headers)


class BookingCase(unittest.TestCase):
    def setUp(self) -> None:
        stub().reset()


# ── HTTP flow ────────────────────────────────────────────────────────────────


class Slots(BookingCase):
    def test_lists_open_times_as_utc_instants(self) -> None:
        status, body = call("GET", url("main", "/booking/slots"))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertEqual(body["timezone"], "America/New_York")
        self.assertEqual(body["minutes"], 30)
        self.assertTrue(body["slots"])
        soonest = datetime.now(UTC) + timedelta(hours=4) - timedelta(seconds=5)
        for text in body["slots"]:
            self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:00Z$")
            slot = booking.parse_instant(text)
            self.assertGreaterEqual(slot, soonest)
            self.assertLess(slot.astimezone(EASTERN).weekday(), 5)
        self.assertEqual(body["slots"], sorted(body["slots"]))

    def test_the_calendar_is_read_as_the_common_account(self) -> None:
        open_times()
        token = stub().calls("POST", "/token")[-1]["body"] if stub().calls("POST", "/token") else None
        query = stub().calls("POST", "/freeBusy")[-1]
        self.assertTrue(query["auth"].startswith("Bearer stub-access-"))
        self.assertEqual(query["body"]["items"], [{"id": "primary"}])
        if token is not None:  # a token cached by an earlier test is not asked for again
            self.assertEqual(token["grant_type"], "refresh_token")
            self.assertEqual(token["refresh_token"], "refresh-token")

    def test_busy_times_are_left_out(self) -> None:
        first, second = open_times()[:2]
        end = booking.iso_z(booking.parse_instant(first) + timedelta(minutes=30))
        stub().extra_busy = [{"start": first, "end": end}]
        after = open_times()
        self.assertNotIn(first, after)
        self.assertIn(second, after)

    def test_an_unreadable_calendar_is_not_treated_as_free(self) -> None:
        """Google answers 200 with `errors` and no `busy` when it cannot read a calendar."""
        stub().freebusy_errors = True
        status, body = call("GET", url("main", "/booking/slots"))
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"], "booking_unavailable")
        self.assertNotIn("slots", body)

    def test_not_configured_says_so(self) -> None:
        # Asked on every page view, so "off" is a 200 with success false, not
        # an error status that would show up as a failing site.
        status, body = call("GET", url("off", "/booking/slots"))
        self.assertEqual((status, body["success"], body["error"]),
                         (200, False, "booking_not_configured"))
        self.assertNotIn("slots", body)
        status, body = book("2026-11-12T15:00:00Z", server="off")
        self.assertEqual((status, body["error"]), (503, "booking_not_configured"))
        self.assertEqual(stub().received, [], "an unconfigured server must not call Google")


class Book(BookingCase):
    def test_books_a_call_with_a_meet_link(self) -> None:
        start = open_times()[0]
        status, body = book(start)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertRegex(body["booking_id"], r"^BB-CALL-\d{8}-[A-HJ-NP-Z2-9]{6}$")
        self.assertEqual(body["start"], start)
        self.assertEqual(booking.parse_instant(body["end"]) - booking.parse_instant(start),
                         timedelta(minutes=30))
        self.assertTrue(body["meet_url"].startswith("https://meet.google.com/"))

        inserts = stub().inserts()
        self.assertEqual(len(inserts), 1)
        # Without these two, no Meet link is made and nobody is emailed.
        self.assertEqual(inserts[0]["query"], {"conferenceDataVersion": "1", "sendUpdates": "all"})
        self.assertTrue(inserts[0]["path"].endswith("/calendars/primary/events"))
        event = inserts[0]["body"]
        self.assertEqual([a["email"] for a in event["attendees"]], [VISITOR["email"]] + TEAM)
        self.assertIs(event["guestsCanSeeOtherGuests"], False)
        self.assertEqual(event["start"]["timeZone"], "America/New_York")
        self.assertEqual(booking.iso_z(booking.parse_instant(event["start"]["dateTime"])), start)
        self.assertIn(body["booking_id"], event["description"])

    def test_nothing_the_visitor_typed_reaches_the_invitation(self) -> None:
        """Google emails the event text to whatever address was typed in."""
        start = open_times()[0]
        status, body = book(start, name="Click http://evil.example now",
                            company="FREE MONEY", message="visit http://evil.example")
        self.assertEqual(status, 200, body)
        sent = json.dumps(stub().inserts()[0]["body"])
        for typed in ("evil.example", "FREE MONEY", "Click", VISITOR["phone"],
                      VISITOR["location"], VISITOR["role"]):
            self.assertNotIn(typed, sent)
        self.assertEqual(stub().inserts()[0]["body"]["summary"], "Butler Button demo")

    def test_a_booked_time_is_no_longer_offered(self) -> None:
        start = open_times()[0]
        self.assertEqual(book(start)[0], 200)
        self.assertNotIn(start, open_times())

    def test_a_second_visitor_cannot_take_the_same_time(self) -> None:
        start = open_times()[0]
        self.assertEqual(book(start)[0], 200)
        status, body = book(start, email="someone.else@hotel.example")
        self.assertEqual((status, body["error"]), (409, "slot_taken"))
        self.assertEqual(len(stub().inserts()), 1)

    def test_the_same_visitor_submitting_twice_gets_the_same_booking(self) -> None:
        start = open_times()[0]
        first = book(start)[1]
        status, second = book(start)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["booking_id"], first["booking_id"])
        self.assertEqual(second["meet_url"], first["meet_url"])
        self.assertEqual(len(stub().events), 1)

    def test_email_case_does_not_make_a_second_booking(self) -> None:
        start = open_times()[0]
        first = book(start)[1]
        second = book(start, email=VISITOR["email"].upper())[1]
        self.assertEqual(second["booking_id"], first["booking_id"])
        self.assertEqual(len(stub().events), 1)

    def test_required_fields_and_email_are_checked(self) -> None:
        start = open_times()[0]
        for change in ({"name": ""}, {"company": " "}, {"location": ""},
                       {"email": "not-an-email"}):
            status, body = book(start, **change)
            self.assertEqual((status, body["error"]), (400, "invalid_request"), change)
        # No time, an unreadable one, and one with no offset (so no fixed instant).
        for bad_start in ("", "tomorrow at ten", "2026-11-12T15:00:00"):
            status, body = book(bad_start)
            self.assertEqual((status, body["error"]), (400, "invalid_request"), bad_start)
        self.assertEqual(stub().inserts(), [])

    def test_honeypot_books_nothing(self) -> None:
        status, body = book(open_times()[0], company_website="http://spam.example")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertEqual(stub().inserts(), [])

    def test_only_times_this_server_offers_can_be_booked(self) -> None:
        first = booking.parse_instant(open_times()[0])
        saturday = first + timedelta(days=(5 - first.astimezone(EASTERN).weekday()) % 7 or 7)
        for label, when in (
            ("off the half-hour grid", first + timedelta(minutes=7)),
            ("in the past", datetime.now(UTC) - timedelta(days=1)),
            ("inside the minimum notice", datetime.now(UTC) + timedelta(minutes=20)),
            ("on a Saturday", saturday),
            ("beyond the horizon", first + timedelta(days=90)),
            ("before opening", first.astimezone(EASTERN).replace(hour=6, minute=0)),
        ):
            status, body = book(booking.iso_z(when))
            self.assertEqual((status, body["error"]), (409, "slot_taken"), label)
        self.assertEqual(stub().inserts(), [])
        self.assertEqual(stub().calls("POST", "/freeBusy")[1:], [],
                         "a time that is not on offer must not reach Google at all")

    def test_an_unreadable_calendar_blocks_booking(self) -> None:
        start = open_times()[0]
        stub().freebusy_errors = True
        status, body = book(start)
        self.assertEqual((status, body["error"]), (502, "booking_unavailable"))
        self.assertEqual(stub().inserts(), [])

    def test_a_timed_out_insert_is_settled_by_looking(self) -> None:
        """The event and its invitations exist; reporting a failure would be wrong."""
        start = open_times()[0]
        stub().insert_mode = "hang"
        status, body = book(start)
        self.assertEqual(status, 200, body)
        self.assertRegex(body["booking_id"], r"^BB-CALL-")
        self.assertTrue(body["meet_url"])
        self.assertEqual(len(stub().events), 1)
        self.assertEqual(len(stub().inserts()), 1, "the insert must not be sent twice")

    def test_a_failed_insert_is_reported_and_books_nothing(self) -> None:
        start = open_times()[0]
        stub().insert_mode = "fail"
        status, body = book(start)
        self.assertEqual((status, body["error"]), (502, "booking_unavailable"))
        self.assertEqual(stub().events, {})
        stub().insert_mode = "ok"
        self.assertIn(start, open_times(), "the time must still be on offer")

    def test_a_meet_link_that_arrives_late_is_picked_up(self) -> None:
        stub().insert_mode = "pending"
        status, body = book(open_times()[0])
        self.assertEqual(status, 200, body)
        self.assertTrue(body["meet_url"].startswith("https://meet.google.com/"))

    def test_a_meet_link_that_never_arrives_does_not_fail_the_booking(self) -> None:
        stub().insert_mode = "pending_forever"
        status, body = book(open_times()[0])
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["meet_url"])
        self.assertEqual(len(stub().events), 1)

    def test_a_time_the_team_cancelled_can_be_booked_again(self) -> None:
        """A deleted event keeps its ID in Google; the rebooking needs a new one."""
        start = open_times()[0]
        first = book(start)[1]
        for event in stub().events.values():
            event["status"] = "cancelled"
        status, second = book(start)
        self.assertEqual(status, 200, second)
        self.assertNotEqual(second["booking_id"], first["booking_id"])
        live = [e for e in stub().events.values() if e["status"] != "cancelled"]
        self.assertEqual(len(live), 1)


class Brakes(BookingCase):
    def test_rate_limit_is_per_address(self) -> None:
        times = open_times("rate")
        seen = [book(times[i], server="rate", email=f"v{i}@hotel.example")[0] for i in range(4)]
        self.assertEqual(seen, [200, 200, 429, 429])
        self.assertEqual(len(stub().inserts()), 2)

    def test_rate_limit_cannot_be_dodged_with_a_forged_forwarded_header(self) -> None:
        """Heroku appends the real address last; the earlier entries are the sender's."""
        times = open_times("rate")
        seen = []
        for i in range(4):
            headers = {"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"}
            seen.append(book(times[10 + i], server="rate", headers=headers,
                             email=f"w{i}@hotel.example")[0])
        self.assertEqual(seen, [200, 200, 429, 429])
        other = {"X-Forwarded-For": "10.0.0.1, 203.0.113.77"}
        self.assertEqual(book(times[20], server="rate", headers=other,
                              email="x@hotel.example")[0], 200)

    def test_daily_cap_sends_further_requests_back_to_the_form(self) -> None:
        times = open_times("cap")
        self.assertEqual(book(times[0], server="cap")[0], 200)
        status, body = book(times[1], server="cap", email="next@hotel.example")
        self.assertEqual((status, body["error"]), (503, "booking_cap_reached"))
        self.assertEqual(len(stub().inserts()), 1)

    def test_googles_usage_limit_pauses_booking(self) -> None:
        start = open_times("quota")[0]
        stub().insert_mode = "quota"
        status, body = book(start, server="quota")
        self.assertEqual((status, body["error"]), (502, "booking_unavailable"))
        stub().reset()
        status, body = call("GET", url("quota", "/booking/slots"))
        self.assertEqual((status, body["error"]), (502, "booking_unavailable"))
        self.assertEqual(stub().received, [], "a paused server must leave Google alone")

    def test_a_dead_refresh_token_turns_booking_off_quietly(self) -> None:
        for _ in range(3):
            status, body = call("GET", url("auth", "/booking/slots"))
            self.assertEqual((status, body["error"]), (502, "booking_unavailable"))
        self.assertEqual(len(stub().calls("POST", "/token-revoked")), 1,
                         "a refused token must not be retried on every request")


class Routes(BookingCase):
    def test_methods(self) -> None:
        self.assertEqual(call("GET", url("main", "/booking/book"))[0], 405)
        self.assertEqual(call("POST", url("main", "/booking/slots"), {})[0], 405)

    def test_malformed_json_is_rejected(self) -> None:
        status, body = call("POST", url("main", "/booking/book"), raw=b"{not json")
        self.assertEqual((status, body["error"]), (400, "invalid_json"))
        status, body = call("POST", url("main", "/booking/book"), ["a", "list"])
        self.assertEqual((status, body["error"]), (400, "invalid_request"))

    def test_foreign_origin_is_refused(self) -> None:
        foreign = {"Origin": "https://evil.example"}
        status, body = call("GET", url("main", "/booking/slots"), headers=foreign)
        self.assertEqual((status, body["error"]), (403, "origin_not_allowed"))
        status, body = book(open_times()[0], headers=foreign)
        self.assertEqual((status, body["error"]), (403, "origin_not_allowed"))
        self.assertEqual(stub().inserts(), [])

    def test_our_own_origin_is_accepted(self) -> None:
        ours = {"Origin": "https://www.butlerbutton.co"}
        self.assertEqual(call("GET", url("main", "/booking/slots"), headers=ours)[0], 200)

    def test_responses_are_never_cached(self) -> None:
        with urllib.request.urlopen(url("main", "/booking/slots"), timeout=10) as res:
            self.assertEqual(res.headers.get("Cache-Control"), "no-store")

    def test_the_request_form_still_answers(self) -> None:
        """Booking shares serve.py's form plumbing; the form must be unaffected."""
        status, body = call("GET", url("main", "/forms/request"))
        self.assertEqual((status, body["error"]), (405, "method_not_allowed"))
        status, body = call("POST", url("main", "/forms/request"), raw=b"{not json")
        self.assertEqual((status, body["error"]), (400, "invalid_json"))


# ── Page wiring ──────────────────────────────────────────────────────────────


class PageWiring(unittest.TestCase):
    """The page books through this server and still records the lead."""

    @classmethod
    def setUpClass(cls) -> None:
        with urllib.request.urlopen(url("main", "/"), timeout=10) as res:
            cls.html = res.read().decode()

    def test_the_demo_button_reads_book_a_demo(self) -> None:
        self.assertRegex(self.html, r'data-request="demo"[^>]*>Book a demo<')
        self.assertIn('data-request="meeting"', self.html)

    def test_it_calls_the_booking_endpoints_on_its_own_origin(self) -> None:
        self.assertIn("'/booking/slots'", self.html)
        self.assertIn("'/booking/book'", self.html)

    def test_the_lead_is_still_sent_after_a_booking(self) -> None:
        self.assertIn("functions/v1/butler-demo-request", self.html)
        self.assertIn("function postLead", self.html)

    def test_every_field_it_posts_is_one_the_server_reads(self) -> None:
        block = re.search(r"var BOOK_FIELDS = \[(.*?)\];", self.html, re.S)
        self.assertIsNotNone(block, "BOOK_FIELDS not found in the page")
        posted = set(re.findall(r"'(\w+)'", block.group(1)))
        self.assertTrue({"name", "email", "company", "location"} <= posted, posted)
        self.assertEqual(posted - set(booking.FIELDS), set())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
