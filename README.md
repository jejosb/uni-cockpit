# uni-cockpit

Personal student cockpit for Joshua at [Hochschule Reutlingen](https://www.reutlingen-university.de/). It imports deadlines from [RELAX](https://relax.reutlingen-university.de) (the university Moodle) through the personal calendar export and shows what is still open, sorted by due date, with the course and the time remaining.

> Kurz: Ein persönliches Cockpit für Fristen aus RELAX. Die Kalender-URL bleibt lokal. Zeiten liegen intern in UTC und werden in Europe/Berlin angezeigt. Telegram erinnert vor der Frist.

![Screenshot placeholder of the deadline list](docs/screenshot-placeholder.svg)

This repository is the Sprint 1 cockpit: store the feed once, import it, show open deadlines, and send a Telegram reminder before each one. A manual "Erledigt" action comes next.

## What it does today

- Reads `RELAX_ICAL_URL`, or a URL saved from the settings page into the local SQLite database.
- Imports every complete event with its title, course, due instant, and iCalendar UID.
- Skips a broken event, logs that it was skipped, and keeps importing the rest.
- If the feed cannot be loaded or is not iCalendar, shows an error and leaves existing rows in place.
- Lists open deadlines earliest first. Each row has the title, the course (or "Ohne Kurs"), the Europe/Berlin date and time, and a countdown such as "noch 2 Tage 4 Std.".
- Hides deadlines that are already past or marked done, and highlights anything due in less than 24 hours.
- Sends one Telegram message per configured offset, with the title, course, Europe/Berlin due time, and remaining time. Offsets that are already past are not sent later. A deadline that is done or removed by the time the job runs produces no message.

The calendar URL contains a personal token. It is never committed and never written to the log. Tests use anonymized `.ics` files under `tests/fixtures/` and do not call RELAX or Telegram.

## Getting the RELAX export URL

1. Sign in to RELAX.
2. Open **Calendar**.
3. Open **Import or export** (Kalender importieren/exportieren).
4. Choose the events and the time window that cover the semester.
5. Click **Get calendar URL** and copy it.

The URL looks like this:

`https://relax.reutlingen-university.de/calendar/export_execute.php?userid=…&authtoken=…&preset_what=courses&preset_time=custom`

`authtoken` is a secret. Do not commit it, paste it into issues, or leave it in a screenshot.

## Setup

Python 3.12 is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Put the export URL in `.env` as `RELAX_ICAL_URL`. You can also leave that variable empty and paste the URL on the Kalender page. The page stores it in `data/cockpit.db`, which is git-ignored. When both are set, the environment variable wins.

By default the app accepts only `https://` calendar URLs. `http://` (including `localhost` and `127.0.0.1`), `file://`, and local `.ics` paths are rejected, on the settings page and when they come from `RELAX_ICAL_URL`. That keeps a deployed process from reading local files or calling internal HTTP services. Set `DEV_ALLOW_LOCAL_FEEDS=true` only on your own machine when you want a fixture or `http://localhost`. Leave it unset or `false` everywhere else. Redirects are not followed; the RELAX export is used as given.

`REMINDER_OFFSETS_HOURS` defaults to `72,24` (3 days and 24 hours before the due instant). Each value is a whole number of real elapsed UTC hours from 1 to 720 (30 days). If any entry is invalid, the app logs a warning and falls back to `72,24` instead of exiting.

Telegram needs `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID` (the chat that should receive the messages). Leave either one empty and the cockpit still starts; the log says reminders are disabled. The bot is only used to send messages. Polling for commands is not started. The token and chat id are never written to the database and must not be committed.

The app must run as a single process (one uvicorn worker; do not pass `--workers` greater than 1), otherwise each worker sends the same reminder again.

Try the app on the sample feed, without RELAX:

```bash
DEV_ALLOW_LOCAL_FEEDS=true \
RELAX_ICAL_URL="file://$PWD/tests/fixtures/relax_deadlines.ics" \
  uvicorn uni_cockpit.main:app --reload
```

Open <http://127.0.0.1:8000>. The sample dates sit in October 2026, around the daylight-saving change.

## Tests

```bash
ruff check src tests
ruff format --check src tests
pytest
```

GitHub Actions runs the same checks on every push and pull request.

## Time handling

All instants are stored in UTC and shown in Europe/Berlin. Remaining time is the real elapsed duration between those UTC instants, so the daylight-saving switch on 25 October 2026 does not change how long is left.

`REMINDER_OFFSETS_HOURS` (default `72,24`) is how early Telegram fires. Each offset is a number of real elapsed hours in UTC, not a shift of the Berlin wall clock. A reminder that crosses the switch on 25 October therefore appears one hour off on the Berlin clock, by design. For a deadline on Monday 26 October 2026 at 12:00 Europe/Berlin (11:00 UTC):

- 24 elapsed hours before is Sunday 25 October 2026, 11:00 UTC (12:00 CET).
- 72 elapsed hours before is Friday 23 October 2026, 11:00 UTC (13:00 CEST).

## Layout

`src/uni_cockpit/feeds/` parses iCalendar into normalized events and keeps `RRULE` and `EXDATE` without expanding them, so a HISinOne timetable adapter can plug in later. `RelaxDeadlineAdapter` turns those events into deadline rows. `calendar_events` is unique on `(source, uid)`. `is_done` survives a re-import. `removed_at` is there for a later sync that needs to retire events missing from a new feed.

`compute_reminder_times` decides the UTC instants. The python-telegram-bot `JobQueue` only schedules that list. On startup, and after each successful import, the app calls `app.state.reminders.reschedule(session, now=...)`, which drops the previous jobs and queues the new ones. The same call is the hook for a later "Erledigt" action or a removed row: the job also checks `is_done` and `removed_at` when it runs, and sends nothing if either is set.

## Roadmap

1. Store the RELAX calendar URL once and import deadlines. This release closes that story.
2. Show open deadlines sorted by due date, with course and remaining time. This release closes that story.
3. Telegram reminder at the offsets in `REMINDER_OFFSETS_HOURS` (3 days and 24 hours by default). This release closes that story.
4. Periodic re-fetch without duplicates, and an "Erledigt" action in the cockpit and in Telegram. The Moodle feed has no submission status, so done is stored locally.
5. Timetable from a second iCal feed exported from HISinOne, including recurring events and exceptions.

## License

The interface vendors [htmx](https://htmx.org/) (`src/uni_cockpit/static/htmx.min.js`, BSD-2-Clause).
