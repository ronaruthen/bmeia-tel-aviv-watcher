# Austrian Embassy Tel Aviv — appointment watcher

Checks the [BMEIA appointment system](https://appointment.bmeia.gv.at/) **every hour**
and sends a **Telegram** message the moment a slot earlier than the best one seen so far
opens up (e.g. when someone cancels). Built for the **Tel Aviv** office, category
**1 · Passports/IDs/Citizenship for newborns**.

It does **not** book — it tells you to grab the slot yourself.

## How it works

`check_bmeia.py` (Python standard library only — no dependencies) replays the public
booking flow with a cookie session:

1. `GET /` → cookie
2. select **Office** = `TEL-AVIV`
3. select **CalendarId** (the service category)
4. select **PersonCount**
5. pass the info page → land on the **calendar**, which auto-jumps to the earliest week
   with availability. Open slots are radio inputs like `9/22/2026 11:30:00 AM`.

It takes the earliest slot **on/after** `EARLIEST_ACCEPTABLE` (the coming Sunday by
default), compares it to `earliest_seen` in [`state.json`](state.json), and if it's
earlier, sends a Telegram alert and lowers the baseline. De-duped so you aren't pinged
twice for the same slot.

## Scheduling

[`.github/workflows/check.yml`](.github/workflows/check.yml) runs it hourly via GitHub
Actions (free, runs even when your laptop is closed) and commits the updated `state.json`
back so the baseline persists between runs. There's also a **Run workflow** button for an
on-demand check (tick *announce* to get a status ping even if nothing changed).

> GitHub's cron is best-effort — runs can drift a few minutes under load. Fine for
> catching cancellations that linger.

## Setup

Two repository secrets are required (**Settings → Secrets and variables → Actions**):

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram user/chat id |

## Configuration (optional env overrides)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BMEIA_CALENDAR_ID` | `25889593` | Service category (see table below) |
| `BMEIA_PERSON_COUNT` | `1` | Number of people/documents |
| `BMEIA_EARLIEST_ACCEPTABLE` | `2026-06-28` | Ignore slots before this date |
| `BMEIA_ALERT_IF_BEFORE` | *(unset)* | Only alert if the slot is before this date |
| `BMEIA_ANNOUNCE` | *(unset)* | `1` → send a status message even with no change |
| `BMEIA_INSECURE_SSL` | *(unset)* | `1` → skip TLS verify (only for broken local CA bundles) |

### Tel Aviv categories (`CalendarId`)

| Id | Category |
| --- | --- |
| `25889593` | 1 · Passports / IDs / Citizenship for newborns |
| `25894778` | 2 · Only 1st Passport — descendants of Nazi-persecution victims |
| `25891519` | 3 · Visa / Residence Permit |
| `25893141` | 4 · Other Consular Matters |
| `33558575` | 5 · Collecting "Bescheid" + applying for 1st passport |
| `48078709` | NBoE 2026 |

## Run locally

```bash
# Print the current earliest slot without touching the saved baseline:
BMEIA_STATE_FILE=/tmp/probe.json BMEIA_ANNOUNCE=1 python3 check_bmeia.py

# On a Mac with a broken CA bundle, prefix with: BMEIA_INSECURE_SSL=1
```

If the embassy changes the booking form, the parser may need a tweak — failures show up
as red runs in the **Actions** tab.
