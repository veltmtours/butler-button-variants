#!/usr/bin/env python3
"""
Demo-call booking for butler-button-variants.

`GET /booking/slots` and `POST /booking/book` (see tools/serve.py) let a visitor
pick an open time and get a Google Meet call put on the team's shared Google
Calendar. This module talks to the Google Calendar API as that one "common"
account:

  * open times  = the bookable window (08:00-19:00 US Eastern, Mon-Fri, 30
                  minutes by default) minus whatever is already busy on the
                  common calendar, and
  * a booking   = one calendar event with a Meet link. Google emails the
                  invitation to the visitor and to the team guests.

Standard library only, like tools/request_forms.py.

Nothing here knows a password. The server holds an OAuth refresh token limited
to calendar events and free/busy, set with `heroku config:set` and never
committed. Until GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN
are all present the endpoints answer `booking_not_configured` and the page keeps
using its plain request form, so shipping this changes nothing by itself.

Two rules worth keeping when editing:

  * The invitation text is fixed. Nothing a visitor types goes into the event,
    because Google would then email that text, from our account, to whatever
    address was typed into the form.
  * A doubtful answer from Google means "not available", never "free". Offering
    a slot that is really taken is the one mistake this must not make.

Run this module directly to check a deployment without booking anything:
    python tools/booking.py

See .env.example for the variables and the one-time Google setup.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import request_forms  # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────────

_CONFIG_ERRORS: list[str] = []


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _number(name: str, default: float, minimum: float, whole: bool = True) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name}={raw!r} is not a number")
        return default
    if value < minimum:
        _CONFIG_ERRORS.append(f"{name}={raw!r} is below the minimum of {minimum:g}")
        return default
    return value


_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_weekdays(text: str) -> frozenset[int]:
    """'mon-fri' or 'mon,wed,fri' or 'mon-fri,sun' -> {0..6}, Monday is 0."""
    days: set[int] = set()
    for part in text.lower().replace(" ", "").split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            start = _DAY_NAMES.index(first[:3])
            stop = _DAY_NAMES.index(last[:3]) if last else start
        except ValueError:
            raise ValueError(f"unknown day in {text!r}") from None
        day = start
        while True:
            days.add(day)
            if day == stop:
                break
            day = (day + 1) % 7
    if not days:
        raise ValueError(f"no days in {text!r}")
    return frozenset(days)


def parse_hours(text: str) -> tuple[int, int]:
    """'08:00-19:00' -> (480, 1140), minutes after local midnight."""
    try:
        first, last = text.replace(" ", "").split("-")
        marks = []
        for mark in (first, last):
            hour, _, minute = mark.partition(":")
            marks.append(int(hour) * 60 + int(minute or 0))
    except ValueError:
        raise ValueError(f"expected HH:MM-HH:MM, got {text!r}") from None
    if not 0 <= marks[0] < marks[1] <= 24 * 60:
        raise ValueError(f"opening must be before closing in {text!r}")
    return marks[0], marks[1]


# Off switch that needs no credentials removed: BB_BOOKING_ENABLED=0.
ENABLED = _env("BB_BOOKING_ENABLED", "1").lower() not in ("0", "false", "no", "off")

# OAuth client + the refresh token minted once for the common account.
CLIENT_ID = _env("GOOGLE_CLIENT_ID")
CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
REFRESH_TOKEN = _env("GOOGLE_REFRESH_TOKEN")

# Which calendar holds the calls. "primary" is the common account's own.
CALENDAR_ID = _env("BB_BOOKING_CALENDAR_ID", "primary")

# Invited to every call so it lands on their own calendars. Hidden from the
# visitor (and, as a consequence, the visitor is hidden from their copies).
TEAM_GUESTS: tuple[str, ...] = ()
for _guest in _env("BB_BOOKING_TEAM_GUESTS").replace(";", ",").split(","):
    _guest = _guest.strip().lower()
    if not _guest:
        continue
    if request_forms.EMAIL_RE.match(_guest):
        if _guest not in TEAM_GUESTS:
            TEAM_GUESTS += (_guest,)
    else:
        _CONFIG_ERRORS.append(f"BB_BOOKING_TEAM_GUESTS has an invalid address: {_guest!r}")

# The bookable window, in the zone it is defined in. A zone name (not a fixed
# offset) so daylight saving is followed: "8 to 7 Eastern" all year.
TZ_NAME = _env("BB_BOOKING_TZ", "America/New_York")
try:
    OPEN_MINUTE, CLOSE_MINUTE = parse_hours(_env("BB_BOOKING_HOURS", "08:00-19:00"))
except ValueError as _exc:
    _CONFIG_ERRORS.append(f"BB_BOOKING_HOURS: {_exc}")
    OPEN_MINUTE, CLOSE_MINUTE = 8 * 60, 19 * 60
try:
    WEEKDAYS = parse_weekdays(_env("BB_BOOKING_WEEKDAYS", "mon-fri"))
except ValueError as _exc:
    _CONFIG_ERRORS.append(f"BB_BOOKING_WEEKDAYS: {_exc}")
    WEEKDAYS = frozenset(range(5))

SLOT_MINUTES = int(_number("BB_BOOKING_MINUTES", 30, 5))
# Nobody can book a call starting sooner than this, or further out than that.
MIN_NOTICE_HOURS = _number("BB_BOOKING_MIN_NOTICE_HOURS", 4, 0, whole=False)
DAYS_AHEAD = int(_number("BB_BOOKING_DAYS_AHEAD", 30, 1))

# Brakes on a public endpoint that makes a Google account send invitations.
DAILY_MAX = int(_number("BB_BOOKING_DAILY_MAX", 25, 1))
RATE_MAX = int(_number("BB_BOOKING_RATE_MAX", 5, 1))
RATE_WINDOW = int(_number("BB_BOOKING_RATE_WINDOW", 600, 1))

# Overridable so the tests can stand a stub in for Google.
TOKEN_URL = _env("BB_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
API_BASE = _env("BB_GOOGLE_API_BASE", "https://www.googleapis.com/calendar/v3").rstrip("/")
GOOGLE_TIMEOUT = _number("BB_GOOGLE_TIMEOUT", 6, 0.2, whole=False)

# One booking request may spend this long talking to Google. Heroku's router
# gives up on a request at 30 seconds; stay well inside it.
BOOK_BUDGET = 20.0
LOCK_WAIT = 8.0
# How long one read of the calendar is reused for the list of open times.
BUSY_TTL = _number("BB_BOOKING_CACHE_SECONDS", 60, 0, whole=False)

SLOT = timedelta(minutes=SLOT_MINUTES)

# What the invitation says. Fixed on purpose - see the module docstring.
EVENT_TITLE = "Butler Button demo"

# name -> max length, taken from the request form so both accept the same input.
FIELDS = ("name", "email", "phone", "company", "role", "location",
          "property_type", "rooms", "timezone", "message", "page")
REQUIRED = tuple(request_forms.REQUIRED)


class GoogleError(RuntimeError):
    """A failed call to Google. `status` is 0 when no HTTP answer came back."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status} {reason}".strip() + (f": {detail}" if detail else ""))
        self.status = status
        self.reason = reason
        self.detail = detail


class SlotTaken(Exception):
    """The requested time is no longer free."""


def _log(*parts: str) -> None:
    print("[booking]", *parts, file=sys.stderr, flush=True)


_tz_cached = None


def _tz():
    """The booking zone. Loaded on first use so a missing tz database turns
    booking off (see is_configured) instead of stopping the server at import."""
    global _tz_cached
    if _tz_cached is None:
        from zoneinfo import ZoneInfo

        _tz_cached = ZoneInfo(TZ_NAME)
    return _tz_cached


def _tz_problem() -> str:
    try:
        _tz()
    except Exception as exc:  # noqa: BLE001
        return f"time zone {TZ_NAME!r} cannot be loaded ({type(exc).__name__}: {exc})"
    return ""


def credentials_set() -> int:
    return sum(1 for value in (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN) if value)


def is_configured() -> bool:
    return bool(ENABLED and credentials_set() == 3 and not _CONFIG_ERRORS and not _tz_problem())


def window_summary() -> str:
    days = ",".join(_DAY_NAMES[d] for d in sorted(WEEKDAYS))
    return (f"{days} {OPEN_MINUTE // 60:02d}:{OPEN_MINUTE % 60:02d}-"
            f"{CLOSE_MINUTE // 60:02d}:{CLOSE_MINUTE % 60:02d} {TZ_NAME}, "
            f"{SLOT_MINUTES} min")


def summary() -> str:
    if not ENABLED:
        return "switched off (BB_BOOKING_ENABLED)"
    if not is_configured():
        return "not configured - the page uses its request form"
    return f"Google Calendar '{CALENDAR_ID}', {window_summary()}, {len(TEAM_GUESTS)} team guest(s)"


def config_warnings() -> list[str]:
    """Configuration mistakes worth flagging before a real booking fails."""
    warnings = list(_CONFIG_ERRORS)
    if not ENABLED:
        return warnings
    have = credentials_set()
    if have == 0:
        warnings.append(
            "Google is not connected - set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REFRESH_TOKEN to turn on call booking; until then the page uses "
            "its request form"
        )
        return warnings
    if have < 3:
        missing = [name for name, value in (
            ("GOOGLE_CLIENT_ID", CLIENT_ID),
            ("GOOGLE_CLIENT_SECRET", CLIENT_SECRET),
            ("GOOGLE_REFRESH_TOKEN", REFRESH_TOKEN),
        ) if not value]
        warnings.append(f"call booking is off: {', '.join(missing)} not set")
    problem = _tz_problem()
    if problem:
        warnings.append(f"call booking is off: {problem} - add `tzdata` to requirements.txt")
    if not TEAM_GUESTS:
        warnings.append(
            "BB_BOOKING_TEAM_GUESTS is empty - calls will appear only on the common "
            "calendar and nobody on the team gets an invitation"
        )
    return warnings


# ── Time helpers ─────────────────────────────────────────────────────────────


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: object) -> datetime | None:
    """An RFC 3339 timestamp with an offset -> aware UTC datetime, else None."""
    if not isinstance(text, str) or not 10 <= len(text) <= 40:
        return None
    raw = text.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Slot maths (pure: the clock is passed in) ────────────────────────────────


def slot_starts(now: datetime) -> list[datetime]:
    """Every bookable start from `now`, as UTC instants, ignoring the calendar.

    Each slot is built from a local calendar date and a local wall-clock time
    and only then converted to UTC. Stepping a UTC time forward by 24 hours
    instead would drift by an hour across a daylight-saving change.
    """
    zone = _tz()
    earliest = now + timedelta(hours=MIN_NOTICE_HOURS)
    today = now.astimezone(zone).date()
    found: set[datetime] = set()
    for ahead in range(DAYS_AHEAD + 1):
        day = today + timedelta(days=ahead)
        if day.weekday() not in WEEKDAYS:
            continue
        minute = OPEN_MINUTE
        while minute + SLOT_MINUTES <= CLOSE_MINUTE:
            local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone)
            instant = local.astimezone(timezone.utc)
            # A wall-clock time skipped by a clock change does not survive the
            # round trip; it never existed, so it is not offered.
            exists = instant.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None)
            if exists and instant >= earliest:
                found.add(instant)
            minute += SLOT_MINUTES
    return sorted(found)


def overlaps(start: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    """Half-open: a meeting that ends exactly when the slot starts is no clash."""
    end = start + SLOT
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def remove_busy(slots: list[datetime], busy: list[tuple[datetime, datetime]]) -> list[datetime]:
    return [slot for slot in slots if not overlaps(slot, busy)]


# ── Google ───────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_token = {"value": "", "expires": 0.0}
_auth_blocked_until = 0.0
_paused_until = 0.0

# Google's own brakes. "Calendar usage limits exceeded" is what a new account
# gets for sending too many invitations; back off properly when it appears.
_PAUSE_SECONDS = {
    "quotaExceeded": 900,
    "dailyLimitExceeded": 900,
    "rateLimitExceeded": 60,
    "userRateLimitExceeded": 60,
}


def _error_reason(body: object) -> tuple[str, str]:
    """(reason, message) from either of Google's two error shapes."""
    if not isinstance(body, dict):
        return "", ""
    err = body.get("error")
    if isinstance(err, str):  # the token endpoint
        return err, str(body.get("error_description") or "")
    if isinstance(err, dict):  # the Calendar API
        reason = ""
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
        return reason or str(err.get("status") or ""), str(err.get("message") or "")
    return "", ""


def _timeout(deadline: float | None) -> float:
    if deadline is None:
        return GOOGLE_TIMEOUT
    left = deadline - time.monotonic()
    if left <= 0.3:
        raise GoogleError(0, "deadline", "out of time for this request")
    return min(GOOGLE_TIMEOUT, left)


def _http(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        reason, message = _error_reason(body)
        raise GoogleError(exc.code, reason, message) from exc
    except (OSError, ValueError) as exc:  # refused, reset, timed out, not JSON
        raise GoogleError(0, "network", f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GoogleError(0, "network", "unexpected response")
    return parsed


def _access_token(deadline: float | None = None, force: bool = False) -> str:
    global _auth_blocked_until
    with _token_lock:
        now = time.time()
        if not force and _token["value"] and now < _token["expires"]:
            return _token["value"]
        if now < _auth_blocked_until:
            raise GoogleError(401, "invalid_grant", "Google refused the stored credentials")
        form = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }).encode()
        try:
            reply = _http("POST", TOKEN_URL,
                          {"Content-Type": "application/x-www-form-urlencoded"},
                          form, _timeout(deadline))
        except GoogleError as exc:
            if exc.reason in ("invalid_grant", "invalid_client", "unauthorized_client"):
                # The refresh token is dead (revoked, or minted while the
                # consent screen was still in "Testing"). Asking again every
                # request will not revive it; say so once and stand down.
                _auth_blocked_until = now + 300
                _log(f"Google refused the stored credentials ({exc.reason}). Call booking "
                     "is off until GOOGLE_REFRESH_TOKEN is replaced - see .env.example.")
            raise
        value = reply.get("access_token")
        if not isinstance(value, str) or not value:
            raise GoogleError(0, "network", "token response had no access_token")
        try:
            lifetime = float(reply.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        _token["value"] = value
        _token["expires"] = now + max(60.0, lifetime - 60.0)
        return value


def _note_failure(exc: GoogleError) -> None:
    global _paused_until
    seconds = _PAUSE_SECONDS.get(exc.reason, 0)
    if not seconds and "usage limits" in exc.detail.lower():
        seconds = 900
    if seconds:
        _paused_until = time.time() + seconds
        _log(f"Google is limiting this account ({exc.reason or exc.detail}); "
             f"call booking paused for {seconds // 60 or 1} min")


def _paused() -> bool:
    return time.time() < _paused_until


def _google(method: str, path: str, body: dict | None = None,
            query: dict | None = None, deadline: float | None = None) -> dict:
    """One authenticated Calendar API call. Raises GoogleError."""
    url = API_BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (0, 1):
        token = _access_token(deadline, force=(attempt == 1))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            return _http(method, url, headers, data, _timeout(deadline))
        except GoogleError as exc:
            if exc.status == 401 and attempt == 0:
                continue  # an access token revoked early: refresh once and retry
            _note_failure(exc)
            raise
    raise GoogleError(401, "unauthorized")  # pragma: no cover


def _calendar_path() -> str:
    return "/calendars/" + urllib.parse.quote(CALENDAR_ID, safe="")


def fetch_busy(start: datetime, end: datetime,
               deadline: float | None = None) -> list[tuple[datetime, datetime]]:
    """Busy periods on the common calendar between two instants."""
    reply = _google("POST", "/freeBusy", {
        "timeMin": iso_z(start),
        "timeMax": iso_z(end),
        "items": [{"id": CALENDAR_ID}],
    }, deadline=deadline)

    calendars = reply.get("calendars")
    entry = None
    if isinstance(calendars, dict):
        entry = calendars.get(CALENDAR_ID)
        if entry is None and len(calendars) == 1:
            entry = next(iter(calendars.values()))
    # When Google cannot read a calendar it answers 200 with an `errors` list
    # and no `busy` key. Reading that as "nothing busy" would offer every slot.
    if not isinstance(entry, dict) or entry.get("errors") or not isinstance(entry.get("busy"), list):
        raise GoogleError(502, "freebusy_error", json.dumps(entry)[:300])

    busy: list[tuple[datetime, datetime]] = []
    for period in entry["busy"]:
        b_start = parse_instant(period.get("start")) if isinstance(period, dict) else None
        b_end = parse_instant(period.get("end")) if isinstance(period, dict) else None
        if b_start is None or b_end is None:
            raise GoogleError(502, "freebusy_error", "unreadable busy period")
        busy.append((b_start, b_end))
    return busy


def get_event(event_id: str, deadline: float | None = None) -> dict:
    return _google("GET", _calendar_path() + "/events/" + urllib.parse.quote(event_id, safe=""),
                   deadline=deadline)


def insert_event(body: dict, deadline: float | None = None) -> dict:
    # conferenceDataVersion=1 is what makes Google act on the Meet request;
    # without it the field is ignored. sendUpdates=all emails every guest.
    return _google("POST", _calendar_path() + "/events", body,
                   query={"conferenceDataVersion": "1", "sendUpdates": "all"},
                   deadline=deadline)


# ── Open slots ───────────────────────────────────────────────────────────────

_busy_lock = threading.Lock()
_busy_cache: dict = {"at": 0.0, "from": None, "to": None, "busy": []}


def _forget_busy() -> None:
    with _busy_lock:
        _busy_cache["from"] = None


def open_slots(now: datetime, deadline: float | None = None) -> list[datetime]:
    """Bookable starts that are free on the calendar right now."""
    slots = slot_starts(now)
    if not slots:
        return []
    first, last = slots[0], slots[-1] + SLOT
    with _busy_lock:
        cached = _busy_cache
        fresh = (cached["from"] is not None
                 and time.time() - cached["at"] < BUSY_TTL
                 and cached["from"] <= first and cached["to"] >= last)
        busy = list(cached["busy"]) if fresh else None
    if busy is None:
        # Only the calendar read is cached. The minimum-notice cut-off above is
        # worked out afresh on every call, so a cached answer never offers a
        # slot that has since come too close.
        busy = fetch_busy(first, last, deadline)
        with _busy_lock:
            _busy_cache.update({"at": time.time(), "from": first, "to": last, "busy": busy})
    return remove_busy(slots, busy)


# ── Throttles ────────────────────────────────────────────────────────────────

_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()
_day_count = {"day": "", "count": 0}


def rate_limited(client_ip: str) -> bool:
    """Per-IP: at most RATE_MAX booking attempts per RATE_WINDOW seconds."""
    now = time.time()
    with _hits_lock:
        for ip in list(_hits):
            kept = [t for t in _hits[ip] if now - t < RATE_WINDOW]
            if kept:
                _hits[ip] = kept
            else:
                del _hits[ip]
        seen = _hits.setdefault(client_ip, [])
        if len(seen) >= RATE_MAX:
            return True
        seen.append(now)
        return False


def _bookings_today(add: int = 0) -> int:
    """Bookings made today (UTC). In memory: a restart resets it, which at
    worst doubles the cap for one day."""
    today = _now().strftime("%Y%m%d")
    with _hits_lock:
        if _day_count["day"] != today:
            _day_count["day"], _day_count["count"] = today, 0
        _day_count["count"] += add
        return _day_count["count"]


# ── Booking ──────────────────────────────────────────────────────────────────

_book_lock = threading.Lock()


def new_booking_id() -> str:
    """`BB-CALL-20261112-4KQ9TX`, in the same family as the request IDs."""
    day = _now().strftime("%Y%m%d")
    tail = "".join(secrets.choice(request_forms.ID_ALPHABET) for _ in range(6))
    return f"BB-CALL-{day}-{tail}"


def validate(payload: object) -> tuple[dict, str | None]:
    """Return (cleaned, error_message). `error_message` is None when valid."""
    if not isinstance(payload, dict):
        return {}, "Malformed request."

    # Honeypot: the page keeps this field off-screen, so only a bot fills it.
    if request_forms._clean(payload.get("company_website"), 200):
        return {}, "spam"

    cleaned: dict = {}
    for name in FIELDS:
        label, limit = request_forms.FIELDS[name]
        cleaned[name] = request_forms._clean(
            payload.get(name), limit, multiline=(name in request_forms.MULTILINE))

    for name in REQUIRED:
        if not cleaned[name]:
            return {}, f"{request_forms.FIELDS[name][0]} is required."
    if not request_forms.EMAIL_RE.match(cleaned["email"]):
        return {}, "That email address does not look right."
    cleaned["email"] = cleaned["email"].lower()

    start = parse_instant(payload.get("start"))
    if start is None:
        return {}, "Please pick a time."
    cleaned["start"] = start
    return cleaned, None


def event_id_for(start: datetime, email: str) -> str:
    """The same visitor booking the same time always yields the same event ID.

    That is what lets a timed-out insert be settled afterwards (look the event
    up by ID) and makes a second click harmless (Google refuses the duplicate).
    Calendar event IDs are base32hex: the letters a-v and digits.
    """
    seed = f"{CALENDAR_ID}|{iso_z(start)}|{email}".encode("utf-8")
    return "bb" + hashlib.sha256(seed).hexdigest()[:40]


def build_event(event_id: str, booking_id: str, start: datetime, email: str) -> dict:
    zone = _tz()
    attendees = [{"email": email}]
    attendees += [{"email": guest} for guest in TEAM_GUESTS if guest != email]
    return {
        "id": event_id,
        # Fixed wording only - nothing the visitor typed. See the docstring.
        "summary": EVENT_TITLE,
        "description": (
            f"Your {SLOT_MINUTES}-minute Butler Button demo, on Google Meet.\n\n"
            f"Booking reference: {booking_id}\n"
            f"To change or cancel, reply to this invitation or write to "
            f"{request_forms.TEAM_EMAIL}."
        ),
        # An explicit zone, so the invitation reads in Eastern time even where
        # the recipient's mail client cannot convert it.
        "start": {"dateTime": start.astimezone(zone).isoformat(), "timeZone": TZ_NAME},
        "end": {"dateTime": (start + SLOT).astimezone(zone).isoformat(), "timeZone": TZ_NAME},
        "attendees": attendees,
        "guestsCanSeeOtherGuests": False,
        "guestsCanInviteOthers": False,
        "guestsCanModify": False,
        "conferenceData": {
            "createRequest": {
                "requestId": booking_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "extendedProperties": {"private": {"bb_booking": booking_id}},
    }


def _meet_url(event: dict) -> str:
    link = event.get("hangoutLink")
    if isinstance(link, str) and link:
        return link
    conference = event.get("conferenceData")
    points = conference.get("entryPoints") if isinstance(conference, dict) else None
    for point in points if isinstance(points, list) else []:
        if isinstance(point, dict) and point.get("entryPointType") == "video" and point.get("uri"):
            return str(point["uri"])
    return ""


def _meet_pending(event: dict) -> bool:
    conference = event.get("conferenceData")
    request = conference.get("createRequest") if isinstance(conference, dict) else None
    status = request.get("status") if isinstance(request, dict) else None
    return isinstance(status, dict) and status.get("statusCode") == "pending"


def _booking_id_of(event: dict) -> str:
    extended = event.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, dict) else None
    return str(private.get("bb_booking") or "") if isinstance(private, dict) else ""


def _live_event(event_id: str, deadline: float | None) -> dict | None:
    """The event with this ID, or None if there is none or it was cancelled."""
    try:
        event = get_event(event_id, deadline)
    except GoogleError as exc:
        if exc.status in (404, 410):
            return None
        raise
    return None if event.get("status") == "cancelled" else event


def _book(cleaned: dict, deadline: float) -> dict:
    """Create the event, or find the one this visitor already has. Raises
    SlotTaken or GoogleError. Runs under _book_lock."""
    start, email = cleaned["start"], cleaned["email"]
    event_id = event_id_for(start, email)

    # Read the calendar again, uncached: the list the visitor chose from may
    # be a minute old, and someone else may have taken the slot since.
    if overlaps(start, fetch_busy(start, start + SLOT, deadline)):
        mine = _live_event(event_id, deadline)
        if mine is not None:
            return mine  # this visitor's own booking, submitted again
        raise SlotTaken()

    booking_id = new_booking_id()
    body = build_event(event_id, booking_id, start, email)
    try:
        return insert_event(body, deadline)
    except GoogleError as exc:
        if exc.status == 409:
            # The ID exists although the slot read as free.
            mine = _live_event(event_id, deadline)
            if mine is not None:
                return mine
            # A call this visitor booked and the team later deleted still
            # holds the ID. Book again under a fresh one.
            body["id"] = "bb" + secrets.token_hex(16)
            return insert_event(body, deadline)
        if exc.status == 0 or exc.status >= 500:
            # No clear answer. The event - and its invitations - may exist, so
            # settle it by looking rather than reporting a failure that is not one.
            mine = _live_event(event_id, deadline)
            if mine is not None:
                return mine
        raise


def _unavailable(exc: Exception) -> tuple[int, dict]:
    return 502, {
        "success": False,
        "error": "booking_unavailable",
        "message": "Online booking is not available just now.",
    }


NOT_CONFIGURED = {
    "success": False,
    "error": "booking_not_configured",
    "message": "Online booking is not set up on this server.",
}


def handle_slots(now: datetime | None = None) -> tuple[int, dict]:
    """GET /booking/slots -> (http_status, json_response)."""
    if not is_configured():
        # A 200, not an error status: the page asks this on every visit, and
        # "booking is not switched on" is a normal answer, not a server fault
        # to show up in the error rate. `success: false` sends it to the form.
        return 200, dict(NOT_CONFIGURED)
    if _paused():
        return _unavailable(GoogleError(0, "paused"))
    try:
        slots = open_slots(now or _now(), time.monotonic() + BOOK_BUDGET)
    except GoogleError as exc:
        _log(f"could not read open times: {exc}")
        return _unavailable(exc)
    return 200, {
        "success": True,
        "timezone": TZ_NAME,
        "minutes": SLOT_MINUTES,
        "slots": [iso_z(slot) for slot in slots],
    }


def handle_book(payload: object, client_ip: str = "",
                now: datetime | None = None) -> tuple[int, dict]:
    """POST /booking/book -> (http_status, json_response)."""
    if not is_configured():
        return 503, dict(NOT_CONFIGURED)
    if _paused():
        return _unavailable(GoogleError(0, "paused"))

    cleaned, error = validate(payload)
    if error == "spam":
        # Say nothing useful to a bot; nothing was booked.
        return 200, {"success": True, "booking_id": new_booking_id(), "meet_url": None}
    if error:
        return 400, {"success": False, "error": "invalid_request", "message": error}

    if client_ip and rate_limited(client_ip):
        return 429, {
            "success": False,
            "error": "rate_limited",
            "message": "Too many requests from this address. Please try again shortly.",
        }
    if _bookings_today() >= DAILY_MAX:
        _log(f"daily cap of {DAILY_MAX} bookings reached - further requests fall back to the form")
        return 503, {
            "success": False,
            "error": "booking_cap_reached",
            "message": "Online booking is not available just now.",
        }

    start = cleaned["start"]
    # Never trust a time from the client: it must be one this server would
    # offer right now. Covers the past, off-grid times, weekends and a page
    # left open until the slot came inside the minimum notice.
    if start not in slot_starts(now or _now()):
        return 409, _slot_taken()

    deadline = time.monotonic() + BOOK_BUDGET
    # One booking at a time, so two visitors cannot both pass the "is it still
    # free" check for the same slot. Per process: this assumes a single dyno.
    if not _book_lock.acquire(timeout=LOCK_WAIT):
        return 503, {
            "success": False,
            "error": "booking_busy",
            "message": "Online booking is busy just now.",
        }
    try:
        event = _book(cleaned, deadline)
        _forget_busy()
    except SlotTaken:
        _forget_busy()
        return 409, _slot_taken()
    except GoogleError as exc:
        _log(f"booking failed for {cleaned['email']} at {iso_z(start)}: {exc}")
        return _unavailable(exc)
    finally:
        _book_lock.release()
    _bookings_today(add=1)

    # Google creates the Meet room asynchronously. It is nearly always there
    # already; if not, look again briefly, then let the invitation carry it.
    for _ in range(2):
        if _meet_url(event) or not _meet_pending(event):
            break
        if deadline - time.monotonic() < 2.5:
            break
        time.sleep(0.7)
        try:
            event = get_event(str(event.get("id") or ""), deadline)
        except GoogleError:
            break

    booking_id = _booking_id_of(event) or new_booking_id()
    meet = _meet_url(event)
    # Nothing is silently dropped: the visitor's details are not in the event
    # (by design), so they go to the log as well as the lead the page sends.
    _log(f"{booking_id} booked {iso_z(start)} for {cleaned['email']} - "
         f"{cleaned['name']}, {cleaned['company']}, {cleaned['location']}"
         + (f", {cleaned['phone']}" if cleaned["phone"] else "")
         + (f" - {meet}" if meet else " - Meet link pending"))
    return 200, {
        "success": True,
        "booking_id": booking_id,
        "start": iso_z(start),
        "end": iso_z(start + SLOT),
        "timezone": TZ_NAME,
        "meet_url": meet or None,
    }


def _slot_taken() -> dict:
    return {
        "success": False,
        "error": "slot_taken",
        "message": "That time has just been taken. Please pick another.",
    }


# ── Self-check ───────────────────────────────────────────────────────────────


def _selftest() -> int:
    """`python tools/booking.py`

    Prints the resolved booking configuration and, when Google is connected,
    reads the calendar and lists the next open times. Books nothing and sends
    nothing, so it is safe to run against the live account.
    """
    print("Call booking")
    print(f"  status     : {summary()}")
    print(f"  window     : {window_summary()}")
    print(f"  notice     : at least {MIN_NOTICE_HOURS:g} h ahead, up to {DAYS_AHEAD} days out")
    print(f"  calendar   : {CALENDAR_ID}")
    print(f"  team guests: {', '.join(TEAM_GUESTS) or '(none)'}")
    print(f"  credentials: {credentials_set()} of 3 set")
    for warning in config_warnings():
        print(f"  WARNING    : {warning}")
    if not is_configured():
        return 1

    print("\nReading the calendar ...")
    try:
        slots = open_slots(_now())
    except GoogleError as exc:
        print(f"FAILED: {exc}")
        if exc.reason in ("invalid_grant", "invalid_client", "unauthorized_client"):
            print("Google refused the stored credentials. Mint a new refresh token "
                  "(.env.example) and check the consent screen is 'In production'.")
        return 1
    zone = _tz()
    print(f"{len(slots)} open time(s) in the next {DAYS_AHEAD} days.")
    for slot in slots[:8]:
        print("  " + slot.astimezone(zone).strftime("%a %d %b %Y  %H:%M %Z"))
    if len(slots) > 8:
        print(f"  ... and {len(slots) - 8} more")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
