# REVIEW.md

Problems I found in the starter `app/main.py`, and how I fixed each one.

| # | Where | What is wrong | How you'd notice | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Works in whatever time zone the datetime happens to be in (server-local, or UTC when read from Mongo), not IST. | A 10:05 IST punch-in is compared with 09:30 in the wrong zone, so lateness is off by hours. | Shift start is built in IST for the attendance date (`shift_window`). |
| 2 | `compute_late_minutes` | Floors to whole minutes, then checks `> 10`. | 09:40:01 should give 10, but the starter gives 0. | Compare in seconds (`> 600`), then floor to minutes (R2). |
| 3 | `compute_late_minutes` | Ignores overnight shifts and fractions of a second. | A 22:00 shift punching in at 02:00 gets a wrong value. | `attendance_date` applies R1 first, and times are cut to whole seconds. |
| 4 | `compute_work_hours` | Uses Python `round()`, which is not half-up. | 3618 seconds is 1.005 hours and should be 1.01. | `Decimal` with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | 10 minutes after shift end returns 10 instead of 0. | Return the minutes only when they are at least 30. |
| 6 | `compute_overtime` | Puts shift end on the punch-in date, and mixes an IST-aware time with possibly naive times. | Overnight punch-out gives a big negative number. A naive value raises `TypeError`. | Shift end comes from the shift window (next day if overnight). Everything is timezone-aware UTC. |
| 7 | `MongoClient(...)` | Not `tz_aware`, so datetimes come back naive. | Epoch conversion shifts by the server's local offset. | `tz_aware=True` plus `to_dt` and `to_ms` helpers. |
| 8 | `create_employee` | Checks for a duplicate, then inserts, and has no unique index. | Two parallel creates both return 201. | Unique index on `emp_code`; a duplicate-key error becomes 409. |
| 9 | `EmployeeIn` | No validation of code, email, lengths, shift format or dates. | `emp_code: "x"` and `joined_on: "2026-02-30"` are accepted. | Pydantic rules and validators; 422 on failure. |
| 10 | `create_employee` | `created_at` is a naive local time, returned as a datetime. | The response has no epoch-millisecond number. | Store a UTC datetime and return epoch ms. |
| 11 | `list_employees` | `skip = page * page_size`. | `page=1` skips the first 20 employees. | `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` ignores the department filter, there is no sort, and page limits are unchecked. | Filtered list shows the whole collection's total. | Count with the same filter, sort by `emp_code`, bound `page` and `page_size`. |
| 13 | `punch_in` | An unknown employee gives `None`, and `emp["shift_start"]` then crashes. | POST with an unknown `emp_code` returns 500. | Return 404. |
| 14 | `punch_in` | `punched_at` goes through `fromtimestamp` (server time), anything is accepted, and `0` counts as "now". | A value in seconds is accepted and the date depends on the server's zone. | Strict epoch-ms type with a range check (422), converted in UTC. |
| 15 | `punch_in` | Any `status` string is accepted. | `ABSENT` creates a record with a punch time. | Only `PRESENT`, `WFH`, `ON_DUTY`. |
| 16 | `punch_in` | `date` comes from server-local time and ignores the overnight rule. | A punch-in at 00:30 IST lands on the wrong day. | `attendance_date()` (R1). |
| 17 | `punch_in` | Duplicate check is `find_one` then insert. | Two parallel requests both get 201. | Unique `(emp_code, date)` index; duplicate-key error becomes 409. |
| 18 | `punch_in` response | Leaks `id` and returns raw datetimes. | `id` appears, `punch_in` is not a number. | `record_out()` returns no `_id` and epoch ms. |
| 19 | `list_attendance` | Loads every record into memory and sorts in Python, by date only. | Slow on 100k records, and same-date rows are not ordered by `emp_code`. | Database sort (date desc, `emp_code` asc) with skip and limit, backed by an index. |
| 20 | `list_attendance` | No validation of dates or status, no `date_from > date_to` check, unbounded paging. | `date_from=garbage` quietly matches nothing. | Date parsing (422), status enum, bounded `page` and `page_size`. |
| 21 | `list_attendance` | `total = len(docs)` after loading everything. | Cost grows with the collection. | `count_documents` (or `estimated_document_count` with no filter). |
| 22 | `/health` | Always returns ok. | Stop MongoDB and it still says 200. | Pings MongoDB and returns 503 on failure. |
| 23 | whole app | No indexes. | `explain` shows `COLLSCAN`. | `ensure_indexes()` at startup, safe to run repeatedly. |

## Looked at and decided were not defects
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct. IST has no daylight saving.
- Ignoring unknown fields in a request body is Pydantic's default, and the contract wants derived fields ignored.
- `load_dotenv()` does not override real environment variables, which is what the contract asks for.
