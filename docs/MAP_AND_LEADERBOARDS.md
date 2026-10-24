# Sales map and leaderboards

## Where service addresses come from

Vendor payroll invoices carry no service addresses (Spark has only the state), so the
map is fed by separate files under **Sales map → Map data → Service addresses**. Every
upload produces a plan you review before anything changes.

| File | Detected by | What happens |
| --- | --- | --- |
| Project enrollment status report | `Enrollment Number` + `Service Address Line 1` | You choose the vendor. Known enrollments get the address, project and kWh. Unknown ones are created as map-only enrollments (`origin = status_report`, no sale date, so they never count on written leaderboards). |
| Address mapping export | `Full Service Address` + `Customer Name` | No enrollment id, so each row is matched by customer name and sale date within 3 days. Two candidates: the selling agent decides, then the exact date. Still ambiguous, or no match: the row is left alone and listed. `No Sale` rows are skipped. |
| Coordinates CSV | `Vendor, Enrollment ID, Latitude, Longitude` | Known locations, stored as `manual` with full confidence. |

## Locations

* **Locate new addresses** queues a `geocode.run` job. The default provider is the US
  Census batch geocoder (free, no key). Results are cached in `geocode_cache` by a
  normalised address key (unit numbers are ignored, since they don't change the
  rooftop), so each building is looked up once.
* Census `Exact` matches get confidence 0.95; `Non_Exact` matches get 0.75 and are
  marked ambiguous. The map shows locations at `MAP_MIN_CONFIDENCE` (0.8) or better,
  plus manual placements. Everything else is counted under the map as
  "not on the map", with the reason.
* **Locations are never guessed.** There is no ZIP or city centroid fallback. If the
  provider can't be reached, rows stay pending and the error is in the audit log.
  Unmatched and low-confidence addresses are listed with a box for typing in a
  latitude and longitude.

## Map

* Filters: vendor, status category, seller, sale-date range, territory or project,
  inside or outside territory, and search. Owners, admins and payroll can search by
  customer and address; managers search by enrollment, seller, city and ZIP.
* Markers are coloured by status category. Clusters are neutral navy. Markers outside
  their territory get a red ring.
* The totals cards cover the visible map area: count, kWh, approved, and outside
  territory. **Export visible area** and **Export all filtered** download CSVs that
  respect your role.
* Access: agents have no map. Managers see only their team (live memberships), with no
  customer names or addresses. Owners, admins and payroll see everything.

### Status categories

Vendor statuses are mapped to `approved, pending, needs_action, denied, cancelled,
chargeback, duplicate, other` by ordered patterns (`engine/emg/services/statuses.py`).
Admins can override them with a `status_categories` settings row:
`[{"category": "approved", "pattern": "^FNLD$"}, ...]`. Unknown statuses fall into
`other` and are never counted as approved. The status-report codes (FNLD, PFNL, CUST
and so on) are deliberately **not** mapped yet. Add patterns once their meaning is
confirmed.

## Territories, projects and capacity

* Import KML, GeoJSON or CSV (`name,wkt` or one `name,lat,lng[,order]` row per vertex).
  Each polygon becomes a `territory` or a `project`, optionally for one vendor.
* A point is **out of territory** when at least one active territory applies to its
  vendor and none contains it. With no territory for that vendor, the flag is blank
  rather than "outside".
* Capacity: set `capacity_kwh` on an area (or pass a GeoJSON property `capacity_kwh`).
  Allocate an enrollment using its actual kWh, or an estimate when kWh is unknown.
  Each enrollment can hold one active allocation. The summary shows allocated,
  estimated, remaining and over-capacity. Allocations are released, never deleted.

## Leaderboards

| Metric | Counts | Date used |
| --- | --- | --- |
| Written | unique enrollments, vendor-marked duplicates excluded | sale date |
| Newly approved | enrollments approved now whose approval falls in the period | vendor approval date; else invoice date; else the first import showing it approved **after** an earlier import showed it not approved. Every proxy is labelled on screen and in exports. |
| Written & now approved | written in the period and approved now | sale date |

* Periods are calendar days, or Monday–Sunday weeks, in the business timezone (settings
  `timezone`, else `EMG_TIMEZONE`, else America/New_York).
* Ranks use competition ranking (1, 2, 2, 4), with ties listed alphabetically. Top N
  includes everyone tied at the cut-off rank. The change column compares with the
  previous day or week.
* Manager boards credit each sale to the managers whose live membership covers that
  seller and vendor, plus a manager's own sales.
* Drill-down: anyone can open their own row, managers can open their team's rows, and
  office roles can open any row. Drill-downs and exports (PNG, PDF, CSV) never contain
  customer, address or debt details.

### Schedules and publishing

* A schedule (daily, or weekly on a chosen weekday, at a local hour) generates the last
  complete period's boards for the chosen metrics and groups, once per period, and
  keeps the PNG, PDF and CSV files.
* The worker checks schedules every minute (`leaderboards.tick`).
* A board is **published only when the schedule has publishing on and its named
  destination is enabled.** Destinations are `webhook` (Slack or Teams incoming webhook,
  https only) or `outbox` (stored, not sent). Stored webhook URLs are never sent back to
  the browser in full.
