#!/usr/bin/env python3
"""
Watch the Austrian Embassy Tel Aviv appointment system for an earlier slot.

It replays the public booking flow (no login, read-only), reads the earliest
available appointment, and pushes a Telegram message when something earlier than
the best slot seen so far opens up. Stdlib only — no pip installs needed.

Booking flow (all against https://appointment.bmeia.gv.at/):
  1. GET /                                        -> cookie
  2. POST Language, Office, Command=Next
  3. POST ... CalendarId, Command=Next
  4. POST ... PersonCount, Command=Next           -> info page
  5. POST /?fromSpecificInfo=True ... Command=Next -> calendar (auto-jumps to
     the earliest week with availability; open slots are radio inputs whose
     value is like "9/22/2026 11:30:00 AM")
"""
import os
import re
import ssl
import json
import time
import html
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime

# ---------------------------------------------------------------- configuration
BASE = "https://appointment.bmeia.gv.at/"

OFFICE       = os.environ.get("BMEIA_OFFICE", "TEL-AVIV")
CALENDAR_ID  = os.environ.get("BMEIA_CALENDAR_ID", "25889593")  # 1. Passports/IDs/Citizenship for newborns
PERSON_COUNT = os.environ.get("BMEIA_PERSON_COUNT", "1")
LANGUAGE     = os.environ.get("BMEIA_LANGUAGE", "en")

# Ignore anything before this date (Rona can only take a slot from the coming Sunday on).
EARLIEST_ACCEPTABLE = os.environ.get("BMEIA_EARLIEST_ACCEPTABLE", "2026-06-28")
# Optional hard cap: only alert if the slot is before this ISO date. Empty = alert on
# every new best slot earlier than the baseline.
ALERT_IF_BEFORE = os.environ.get("BMEIA_ALERT_IF_BEFORE", "").strip()
# Send a "watcher is live, current earliest = ..." message even when nothing changed.
ANNOUNCE = os.environ.get("BMEIA_ANNOUNCE", "").strip() in ("1", "true", "yes")

MAX_WEEK_HOPS = int(os.environ.get("BMEIA_MAX_WEEK_HOPS", "6"))
STATE_FILE = os.environ.get(
    "BMEIA_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) bmeia-watcher")
TIMEOUT = 60
RETRIES = 3
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number

SLOT_FMT = "%m/%d/%Y %I:%M:%S %p"  # e.g. "9/22/2026 11:30:00 AM"


# ---------------------------------------------------------------- http helpers
# Verify TLS by default. On machines with a broken CA bundle (some macOS Python
# builds) set BMEIA_INSECURE_SSL=1 to skip verification — fine for this public,
# read-only page. GitHub's Ubuntu runners verify normally, so leave it off there.
if os.environ.get("BMEIA_INSECURE_SSL", "").strip() in ("1", "true", "yes"):
    SSL_CONTEXT = ssl._create_unverified_context()
else:
    SSL_CONTEXT = ssl.create_default_context()


def new_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    )
    opener.addheaders = [("User-Agent", USER_AGENT),
                         ("Accept-Language", "en-US,en;q=0.9")]
    return opener


def http_request(opener, url, fields=None):
    """GET if fields is None, else POST form-encoded. Retries on failure."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            data = None
            if fields is not None:
                data = urllib.parse.urlencode(fields).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - network is best-effort
            last_err = exc
            print(f"  ! request failed (attempt {attempt}/{RETRIES}): {exc}")
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"request to {url} failed after {RETRIES} attempts: {last_err}")


# ---------------------------------------------------------------- scraping
def base_fields(command):
    return {
        "Language": LANGUAGE,
        "Office": OFFICE,
        "CalendarId": CALENDAR_ID,
        "PersonCount": PERSON_COUNT,
        "Command": command,
    }


def open_calendar(opener):
    """Walk steps 1-5 and return the calendar-page HTML."""
    http_request(opener, BASE)  # 1: cookie
    http_request(opener, BASE, {"Language": LANGUAGE, "Office": OFFICE, "Command": "Next"})  # 2
    http_request(opener, BASE, {"Language": LANGUAGE, "Office": OFFICE,
                                "CalendarId": CALENDAR_ID, "Command": "Next"})  # 3
    http_request(opener, BASE, {"Language": LANGUAGE, "Office": OFFICE,
                                "CalendarId": CALENDAR_ID, "PersonCount": PERSON_COUNT,
                                "Command": "Next"})  # 4 -> info page
    return http_request(opener, BASE + "?fromSpecificInfo=True", base_fields("Next"))  # 5


def parse_slots(page):
    """Return all bookable appointment datetimes on a calendar page."""
    slots = []
    for raw in re.findall(r'type="radio"[^>]*value="([^"]+)"', page):
        value = html.unescape(raw).strip()
        try:
            slots.append(datetime.strptime(value, SLOT_FMT))
        except ValueError:
            pass  # not a slot radio (defensive)
    return slots


def is_calendar_page(page):
    return "Appointments available" in page or 'type="radio"' in page


def earliest_acceptable_slot(opener, floor):
    """
    Earliest slot >= floor. The calendar auto-opens on the first week with
    availability; if every shown slot is before the floor, hop forward a few
    weeks to look further.
    """
    page = open_calendar(opener)
    if not is_calendar_page(page):
        raise RuntimeError("did not reach the calendar page (site layout changed or transient error)")

    for hop in range(MAX_WEEK_HOPS + 1):
        slots = parse_slots(page)
        if not slots:
            return None  # genuinely no availability
        acceptable = [s for s in slots if s >= floor]
        if acceptable:
            return min(acceptable)
        # All shown slots are before the floor -> look one week further.
        page = http_request(opener, BASE, base_fields("Next week"))
    return None


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- telegram
def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("  (telegram not configured — would have sent:)")
        print("  " + text.replace("\n", "\n  "))
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=payload),
                                    timeout=30, context=SSL_CONTEXT) as resp:
            ok = json.load(resp).get("ok", False)
            print(f"  telegram sent: {ok}")
            return ok
    except Exception as exc:  # noqa: BLE001
        print(f"  ! telegram send failed: {exc}")
        return False


def fmt(dt):
    return dt.strftime("%A, %d %B %Y at %H:%M")


# ---------------------------------------------------------------- main
def main():
    floor = parse_iso(EARLIEST_ACCEPTABLE) or datetime.min
    cap = parse_iso(ALERT_IF_BEFORE)
    state = load_state()
    baseline = parse_iso(state.get("earliest_seen"))

    print(f"Checking {OFFICE} / calendar {CALENDAR_ID} / {PERSON_COUNT} person(s)")
    print(f"  floor (earliest acceptable): {floor.date()}")
    print(f"  baseline (best seen so far): {baseline}")

    opener = new_opener()
    earliest = earliest_acceptable_slot(opener, floor)
    state["last_checked"] = datetime.now().isoformat(timespec="seconds")

    if earliest is None:
        print("  -> no acceptable slots currently offered")
        state["last_status"] = "no acceptable slots"
        if ANNOUNCE:
            send_telegram("👋 BMEIA Tel Aviv watcher is live. No slots are currently "
                          "offered for your category; I'll ping you when one opens.")
        save_state(state)
        return

    print(f"  -> earliest acceptable slot: {earliest}")
    state["last_status"] = f"earliest acceptable {earliest.isoformat()}"

    booking_link = BASE
    improved = baseline is None or earliest < baseline
    under_cap = cap is None or earliest < cap

    if baseline is None:
        # No baseline recorded yet — adopt it quietly (or announce on request).
        state["earliest_seen"] = earliest.isoformat()
        msg = (f"👋 <b>BMEIA Tel Aviv watcher is live.</b>\n"
               f"Current earliest slot: <b>{fmt(earliest)}</b>.\n"
               f"I'll ping you the moment something earlier opens up.\n{booking_link}")
        if ANNOUNCE:
            send_telegram(msg)
        else:
            print("  baseline recorded (no message; set BMEIA_ANNOUNCE=1 to announce)")
    elif improved and under_cap:
        msg = (f"🔔 <b>Earlier Austrian Embassy slot available!</b>\n"
               f"📅 <b>{fmt(earliest)}</b>\n"
               f"(was {fmt(baseline)})\n"
               f"Category 1 · Passports/IDs/Citizenship · Tel Aviv\n"
               f"Book now → {booking_link}")
        send_telegram(msg)
        state["earliest_seen"] = earliest.isoformat()
        state["last_alerted"] = earliest.isoformat()
    elif improved:
        # Earlier than before but not under the cap she asked to be alerted on.
        print(f"  improved to {earliest} but not under cap {cap} — lowering baseline silently")
        state["earliest_seen"] = earliest.isoformat()
        if ANNOUNCE:
            send_telegram(f"ℹ️ Watcher live. Best slot now {fmt(earliest)} "
                          f"(no alert — your cap is before {cap.date()}).")
    else:
        print("  no improvement over baseline — no alert")
        if ANNOUNCE:
            send_telegram(f"👋 BMEIA Tel Aviv watcher is live. Current earliest slot "
                          f"is {fmt(earliest)} (= your current baseline). "
                          f"I'll ping you when something earlier opens.")

    save_state(state)


if __name__ == "__main__":
    main()
