# uni-cockpit

Personal student cockpit for Joshua at [Hochschule Reutlingen](https://www.reutlingen-university.de/). It imports deadlines from [RELAX](https://relax.reutlingen-university.de) (the university Moodle) through the personal calendar export and shows what is still open, sorted by due date, with the course and the time remaining.

> Kurz: Ein persönliches Cockpit für Fristen aus RELAX und den Stundenplan aus HISinOne. Die Kalender-URLs bleiben lokal. Zeiten liegen intern in UTC und werden in Europe/Berlin angezeigt. Telegram erinnert vor einer Frist, nicht vor einer Vorlesung.

![Screenshot placeholder of the deadline list](docs/screenshot-placeholder.svg)

This repository is the Sprint 1 cockpit: store the feed once, import it, show open deadlines, send a Telegram reminder before each one, and let Joshua mark a deadline done.

## What it does today

- Reads `RELAX_ICAL_URL`, or a URL saved from the settings page into the local SQLite database. Only hosts listed in `FEED_ALLOWED_HOSTS` are accepted.
- Imports every complete event with its title, course, due instant, and iCalendar UID.
- Skips a broken event, logs that it was skipped, and keeps importing the rest. That includes an event without `END:VEVENT` and an event that is not valid UTF-8; their UID counts as skipped, not removed.
- If the feed cannot be loaded or is not iCalendar, shows an error and leaves existing rows in place.
- Lists open deadlines earliest first. Each row has the title, the course (or "Ohne Kurs"), the Europe/Berlin date and time, and a countdown such as "noch 2 Tage 4 Std.".
- Hides deadlines that are already past or marked done, and highlights anything due in less than 24 hours.
- Re-fetches the RELAX feed on `SYNC_INTERVAL_MINUTES` (default 60). The same UID is updated in place, so a second import does not create a duplicate. The download and parse run in a worker thread and do not block the web server.
- If an event disappears from a later feed, or arrives with `STATUS:CANCELLED`, it is marked removed (`removed_at`). It leaves the open list and is not reminded. If that event comes back, `removed_at` is cleared. `is_done` still survives every re-import.
- A failed re-fetch logs a generic message, never the calendar URL or token, and leaves the existing rows in place.
- Sends one Telegram message per configured offset, with the title, course, Europe/Berlin due time, and remaining time. Offsets that are already past are not sent later. A deadline that is done or removed by the time the job runs produces no message.
- Each open row has an **Erledigt** button. It marks the deadline done, the row leaves the open list, and a **Rückgängig** link can open it again. The Telegram reminder has the same button. The callback data is only the event id, and the bot accepts it only from `TELEGRAM_CHAT_ID`.
- Reads `HISINONE_ICAL_URL`, or a URL saved from the Kalender page, through the same fetcher and iCalendar parser as RELAX. Lectures are stored on the `hisinone` source and never schedule a Telegram reminder.
- Expands weekly series, including EXDATE cancellations and a single moved occurrence (`RECURRENCE-ID`). A lecture keeps its Europe/Berlin wall time across the clock change on 25 October 2026.
- Shows the current week on the Stundenplan page, with the time and room, and links to the next week. Today's lectures also appear above the deadline list.
- If a successful timetable import contains no upcoming events, the cockpit asks Joshua to create a new HISinOne export link. A failed download leaves the previous lectures in place.

The calendar URLs contain a personal token. They are never committed and never written to the log. Tests use anonymized `.ics` files under `tests/fixtures/` and do not call RELAX, HISinOne, or Telegram.

## Getting the RELAX export URL

1. Sign in to RELAX.
2. Open **Calendar**.
3. Open **Import or export** (Kalender importieren/exportieren).
4. Choose a continuous range such as **Kürzlich und demnächst** (recent and upcoming), not one fixed month, so the feed keeps covering what is coming up.
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

The HISinOne timetable uses `HISINONE_ICAL_URL` the same way, or the second form on the Kalender page. In HISinOne, open your timetable and copy the iCalendar export link. That link is personal. After a semester change it often lists no upcoming events; the Stundenplan page then asks you to generate a new link and save it again.

By default the app accepts only `https://` calendar URLs whose host is listed in `FEED_ALLOWED_HOSTS`. The default list is `relax.reutlingen-university.de` alone. The HISinOne hostname is not in that list and this repository does not add it, because the host is still unknown. Add the exact host from your export URL to `FEED_ALLOWED_HOSTS` or the timetable fetch is rejected. Hosts are compared exactly after the name is lowercased, converted to ASCII (IDNA), and stripped of one trailing dot. `relax.reutlingen-university.de.` is therefore the same host, while a suffix such as `relax.reutlingen-university.de.evil.example`, a subdomain, or any other name is not. URLs with userinfo (`user@host`) and ports other than 443 are rejected. A confusable letter is a different host after IDNA, so `relаx.reutlingen-university.de` (Cyrillic а) is not the allowed host. An empty `FEED_ALLOWED_HOSTS` keeps that default and never means every host is allowed. If any entry is invalid (a URL, a port, a wildcard, a path, a space inside the name, or an IP address), the app logs one warning that does not repeat the value and uses the default list for the whole setting. It does not crash.

`http://` (including `localhost` and `127.0.0.1`), `file://`, and local `.ics` paths are rejected, on the settings page and when they come from `RELAX_ICAL_URL` or `HISINONE_ICAL_URL`. That keeps a deployed process from reading local files or calling internal services. Set `DEV_ALLOW_LOCAL_FEEDS=true` only on your own machine when you want a fixture or `http://localhost`. Those local feeds skip the host list. Any other `https://` host is still checked against `FEED_ALLOWED_HOSTS`, including in development. Leave the flag unset or `false` everywhere else. Redirects are not followed; the export URL is used as given.

The same settings object supplies the allowlist when a URL is saved and on every fetch, including the startup import and a later re-fetch. `Settings.feed_allowed_hosts` is passed into `validate_calendar_url` and into `UrlCalendarFetcher` for both feeds.

`REMINDER_OFFSETS_HOURS` defaults to `72,24` (3 days and 24 hours before the due instant). Each value is a whole number of real elapsed UTC hours from 1 to 720 (30 days). If any entry is invalid, the app logs a warning and falls back to `72,24` instead of exiting.

`SYNC_INTERVAL_MINUTES` defaults to `60`. It is a whole number of minutes from 1 to 10080 (7 days). If the value is invalid, the app logs a warning and falls back to 60 instead of exiting. The periodic job waits that long after startup, then re-imports. It uses the same `Settings` object and the same fetcher as the rest of the app (`allow_local` is `DEV_ALLOW_LOCAL_FEEDS` on that object). The network fetch and the iCalendar parse run in a thread (`asyncio.to_thread`), so the event loop keeps serving pages. Manual import and the periodic job share one lock, so they cannot insert the same UID at the same time.

A valid calendar that contains no deadlines does not delete open future deadlines. The cockpit keeps showing “RELAX hat einen leeren Kalender geliefert, deine Fristen bleiben erhalten.” until a later fetch contains deadlines again. A feed that is not iCalendar leaves the rows and the scheduled reminders unchanged.

An event the parser skips, for example because `SUMMARY` is missing, is not treated as deleted. `removed_at` is set only when the UID is really absent or the event has `STATUS:CANCELLED`. Each row stores a `source` (`relax` for this feed). Updates and removal apply only to that source, and `(source, uid)` is unique, so the same UID from HISinOne can sit beside the RELAX row.

Telegram needs `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID` (the chat that should receive the messages). Leave either one empty and the cockpit still starts; the log says reminders are disabled, and the Erledigt button in the browser still works. When both are set, the bot sends reminders and polls for the Erledigt callback only. It does not poll for commands. That polling starts in the background and does not block the web server from starting. The token and chat id are never written to the database and must not be committed.

Pressing **Erledigt** in the cockpit or on a Telegram reminder sets `is_done`, drops the pending reminders for that deadline, and hides it from the open list. In Telegram the button's callback data is the event id alone. The handler ignores a press whose chat id is not `TELEGRAM_CHAT_ID`, then edits the reminder to confirm. A second press is a no-op. **Rückgängig** in the cockpit clears `is_done` and schedules the reminders again. A later re-import does not clear `is_done`.

The app must run as a single process (one uvicorn worker; do not pass `--workers` greater than 1). A second worker would send each reminder again and run a second refresh loop. The refresh task starts and stops with the app lifespan, and starting it twice in one process does not create a second loop.

Try the app on the sample feed, without RELAX:

```bash
DEV_ALLOW_LOCAL_FEEDS=true \
RELAX_ICAL_URL="file://$PWD/tests/fixtures/relax_deadlines.ics" \
HISINONE_ICAL_URL="file://$PWD/tests/fixtures/hisinone_timetable.ics" \
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

A weekly HISinOne lecture is the other way around: the series is expanded in Europe/Berlin, so 10:15 stays 10:15 on the wall clock. That stays true when the export uses `TZID=Europe/Berlin`, a trailing `Z`, a floating local time, or a custom `VTIMEZONE` name. Before the switch that is 08:15 UTC; on Tuesday 27 October 2026 it is 09:15 UTC. Cancelled dates (`EXDATE`) are omitted, including dates after the switch. A Zulu exception may name either the unshifted RFC instant (`08:15Z`) or the Berlin-normalized instant (`09:15Z`); both refer to the same slot. An unknown time-zone name is read as Europe/Berlin, not as UTC, and logged once. A local time that falls in the spring gap, such as 02:30 on 28 March 2027, uses the offset from before the change (01:30 UTC, shown as 03:30 CEST) and is logged once. RELAX deadlines follow the same two rules through the shared helper in `services/timezones.py`. A moved date (`RECURRENCE-ID`) is shown at its new time and room, and the original slot is not kept beside it.

`REMINDER_OFFSETS_HOURS` (default `72,24`) is how early Telegram fires. Each offset is a number of real elapsed hours in UTC, not a shift of the Berlin wall clock. A reminder that crosses the switch on 25 October therefore appears one hour off on the Berlin clock, by design. For a deadline on Monday 26 October 2026 at 12:00 Europe/Berlin (11:00 UTC):

- 24 elapsed hours before is Sunday 25 October 2026, 11:00 UTC (12:00 CET).
- 72 elapsed hours before is Friday 23 October 2026, 11:00 UTC (13:00 CEST).

## Layout

`src/uni_cockpit/feeds/` parses iCalendar into normalized events and keeps `RRULE`, `EXDATE`, and `RECURRENCE-ID` without expanding them. `RelaxDeadlineAdapter` turns those events into deadline rows. `HisinoneTimetableAdapter` (`source_key="hisinone"`) expands them into lecture rows. The timetable form calls `validate_calendar_url` with `allowed_hosts=Settings.feed_allowed_hosts`, the same allowlist the RELAX form and the shared `UrlCalendarFetcher` use. Importing uses `app.state.fetcher` and does not open another HTTP client. `calendar_events` is unique on `(source, uid)`. `is_done` survives a re-import. `removed_at` is set when that source no longer contains the UID or the event has `STATUS:CANCELLED`, and cleared when the event returns. A skipped VEVENT is not a removal. A timetable feed with no upcoming events, whether it is empty or contains only past events, does not mark anything removed. It records that outcome on the `hisinone` source, and the cockpit asks to regenerate the HISinOne export link. A RELAX import does not change `hisinone` rows, and a HISinOne import does not change `relax` rows.

`compute_reminder_times` decides the UTC instants. The python-telegram-bot `JobQueue` only schedules that list. On startup, after each successful import (including the periodic refresh), after **Erledigt** / **Rückgängig**, and after a removal, the app calls `app.state.reminders.reschedule(session, now=app.state.clock.now())`, which drops the previous jobs and queues the new ones. The job also checks `is_done` and `removed_at` when it runs, and sends nothing if either is set.

## Roadmap

1. Store the RELAX calendar URL once and import deadlines. This release closes that story.
2. Show open deadlines sorted by due date, with course and remaining time. This release closes that story.
3. Telegram reminder at the offsets in `REMINDER_OFFSETS_HOURS` (3 days and 24 hours by default). This release closes that story.
4. Periodic re-fetch without duplicates, and an "Erledigt" action in the cockpit and in Telegram. The Moodle feed has no submission status, so done is stored locally. This release closes that story.
5. Timetable from a second iCal feed exported from HISinOne, including recurring events and exceptions. This release closes that story.

## License

The interface vendors [htmx](https://htmx.org/) (`src/uni_cockpit/static/htmx.min.js`, BSD-2-Clause).
