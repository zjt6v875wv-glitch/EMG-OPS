# Vendor connectors (Spark, Arcadia, ...)

Connectors keep enrollment statuses current for the leaderboards and map without daily
uploads. Each night, at the hours you choose, the worker:

1. logs in to the vendor portal with a headless browser (or calls the vendor API);
2. downloads the enrollment export for the last N days;
3. stores the file (like every upload) and syncs statuses.

**A sync never touches payroll.** It creates no import batch, no invoice lines and no
ledger entries. Vendor invoices still come in through Imports and review.

## What a sync does

* It adds enrollments the payroll files haven't seen yet (`origin = portal`). These count
  on written leaderboards from their sale date.
* On known enrollments, it fills missing facts (rep, sale date, approval date, kWh,
  address) but never overwrites a value a payroll file set.
* It updates the status when it changes and records the change in the status history.
  Because the portal is read every day, a newly approved enrollment gets a real
  "first seen approved" date even when the vendor gives no approval date.
* Statuses that match no category are listed on the run, so you can add patterns
  (see MAP_AND_LEADERBOARDS.md).

## Setting one up (Vendor connectors screen)

1. Create a separate **read-only portal user** for the app. Don't use a person's login.
2. **Add connector:** name, vendor (spelled as in payroll, e.g. `Spark`), portal login.
3. **Allowed hosts:** the portal's host names, including its sign-in or SSO host. The
   browser can't load anything else, and credentials are typed only on these hosts.
4. **Steps:** the clicks a person makes, as JSON. For example:

```json
[
 {"goto": "https://portal.vendor.com/login"},
 {"fill": "input[name=email]", "value": "{username}"},
 {"fill": "input[type=password]", "value": "{password}"},
 {"click": "button[type=submit]"},
 {"wait_for": "text=Dashboard"},
 {"goto": "https://portal.vendor.com/enrollments?from={date_from}&to={date_to}"},
 {"download": "text=Export"}
]
```

   Step types: `goto`, `fill`, `click`, `press`, `select`, `wait_for`, `wait_ms` and
   `download`. Variables: `{username} {password} {totp} {date_from} {date_to}`, plus
   `{date_from_us} {date_to_us}` in MM/DD/YYYY.
5. **Columns:** type the export's column headers for Enrollment ID and Status (required)
   and any of rep, sale date, approval date, kWh and so on. Upload one export you
   downloaded by hand to check the mapping.
6. **Login:** username and password, plus the authenticator secret if the portal uses
   2FA codes. Credentials are encrypted with `CONNECTOR_SECRET_KEY` and are write-only;
   the app never shows them again.
7. Click **Run now**, check the run history, then switch it on.

## When something breaks

Portals change. A failed run:

* is recorded with the step that failed and a **screenshot** of the page (admins only);
* opens a non-blocking **Review queue** item, which closes itself after the next
  successful run;
* posts to the alert destination, if you set one (a leaderboard webhook destination).

Passwords are masked in error messages. SMS or email 2FA codes can't be automated. If a
portal requires one, ask the vendor for an API, a scheduled report, or an
authenticator-app option.

## Switching to an API later

Change the connector type to **API**. Each vendor API is a small function in
`engine/emg/connectors/api.py` that returns records. A generic `http_json` provider
(bearer token, dotted `records_path`, optional pagination) is included. The column
mapping then names JSON keys, for example `agent.name`. Everything after the fetch (the
sync, leaderboards and alerts) stays the same.

## Running it

* The worker (`python -m emg.services.jobs`) checks schedules every minute.
* For a manual or cron run: `python -m emg.cli run-connector "Spark portal"`.
* Docker: the engine image installs Chromium. Locally, run
  `pip install playwright && playwright install chromium`.
