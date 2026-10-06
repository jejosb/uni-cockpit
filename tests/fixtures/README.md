# Fixtures

Anonymized iCalendar samples for the test suite. They use `calendar.example.edu`
and invented courses. Do not put real RELAX export URLs, authtokens, student
names, or Telegram tokens in this directory.

Add a new case as its own `.ics` file and load it from a test with
`read_fixture`. CI must keep using these files instead of the live RELAX or
HISinOne feeds. `hisinone_*.ics` are anonymized timetable shapes: a weekly
series, an EXDATE, a moved RECURRENCE-ID, the same exceptions after the
DST change (both the Berlin-normalized instant and the unshifted RFC `Z`
instant), four ways of writing the same Berlin wall time, an empty
calendar, a past-only calendar, and an open-ended weekly rule.
