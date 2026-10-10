# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the datetime's own zone (UTC from Mongo) instead of IST to place `shift_start`. | Punch-in 10:05 IST (04:35Z) is compared with 09:30 **UTC**, so everyone looks hours late. | Shift start is built in IST for the attendance date (`shift_window`). |
| 2 | `compute_late_minutes` | Floors to minutes *before* the `> 10` test, so 09:40:30 gives 10 minutes elapsed and passes `> 10` only by luck, and 09:40:01 gives 10 and returns 0. | 09:40:01 must return 10, starter returns 0. | Compare **seconds** (`> 600`), then floor to minutes (R2). |
| 3 | `compute_late_minutes` | Ignores overnight shifts and sub-second values. | Meera (22:00-06:00) punching in at 02:00 gets a negative or wrong value. | `attendance_date` applies R1 first; instants are truncated to whole seconds. |
| 4 | `compute_work_hours` | Python `round()` (banker's / float) instead of half-up. | 3618 s = 1.005 h rounds to 1.0, should be 1.01. | `Decimal` with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | 10 minutes after shift end returns 10, should be 0. | Return the floor minutes only when >= 30. |
| 6 | `compute_overtime` | Shift end placed on the punch-in date and mixes an aware IST value with possibly naive punch-outs. | Overnight punch-out next morning gives a large negative, naive values raise `TypeError`. | End is computed from the shift window (next day for overnight); everything is tz-aware UTC. |
| 7 | `MongoClient(...)` | Not `tz_aware`, so reads return naive datetimes. | Epoch conversion shifts by the server's local offset. | `tz_aware=True`, `tzinfo=UTC`, plus explicit `to_dt` / `to_ms`. |
| 8 | `create_employee` | Check-then-insert; no unique index. | Parallel duplicate creates both return 201. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 9 | `EmployeeIn` | No validation (code pattern, email, lengths, shift format, date validity, `shift_start != shift_end`). | `emp_code: "x"`, `joined_on: "2026-02-30"` are accepted. | Pydantic constraints and validators, 422 on failure. |
| 10 | `create_employee` | `created_at` is a naive local `datetime.now()` and is returned as a datetime. | Response has no epoch-ms integer. | UTC datetime stored, epoch ms returned. |
| 11 | `list_employees` | `skip = page * page_size` (page 1 skips the first page). | `page=1` misses the first 20 rows. | `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` ignores the department filter; no sort; page bounds unchecked. | Filtered list shows `total` of the whole collection. | `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 13 | `punch_in` | `emp` is `None` for an unknown code, so `emp["shift_start"]` raises. | POST with an unknown `emp_code` returns 500. | Explicit 404. |
| 14 | `punch_in` | `punched_at` is read with `fromtimestamp` (server-local zone), accepts anything, and `0` or negative is treated as "now". | Seconds-looking value is accepted; date depends on the server's zone. | Strict epoch-ms type (range-checked, 422), UTC conversion, truncation. |
| 15 | `punch_in` | `status` is any string (ABSENT, LEAVE, garbage). | `status: "ABSENT"` creates a record with a punch time. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 16 | `punch_in` | `date` from the server's local zone, ignores IST and the overnight rule (R1). | Punch-in at 00:30 IST lands on the wrong day; Meera at 02:00 gets the wrong date. | `attendance_date()`. |
| 17 | `punch_in` | Duplicate check is `find_one` then insert (race). | Two parallel identical requests both get 201. | Unique index on `(emp_code, date)`; `DuplicateKeyError` becomes 409. |
| 18 | `punch_in` response | Leaks `id` (`_id`) and returns raw datetimes. | `id` present, `punch_in` not an integer. | `record_out()` serializer, no `_id`, epoch ms. |
| 19 | `list_attendance` | Loads the whole collection into memory and sorts in Python; sort is by date only. | 100k records: slow and memory heavy; ties not ordered by `emp_code`. | `find().sort(date desc, emp_code asc).skip().limit()` backed by an index. |
| 20 | `list_attendance` | No validation of dates or status, no `date_from > date_to` check, unbounded paging, leaks `id`. | `date_from=garbage` silently matches nothing. | Date parsing (422), `Status` enum, `Query` bounds, serializer. |
| 21 | `list_attendance` | `total = len(docs)` after a full load. | Cost grows with the collection. | `count_documents` (or `estimated_document_count` with no filter). |
| 22 | `/health` | Always returns ok. | DB down still returns 200. | Pings Mongo, 503 on failure. |
| 23 | module | No indexes at all. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup, idempotent. |

## Looked at and decided not defects
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct (IST has no DST).
- `doc.pop("_id")` after `insert_one` is fine (insert adds `_id` to the dict); I now build the response from a serializer instead.
- Ignoring unknown body fields (derived fields) is Pydantic's default and is what the contract asks for.
- `load_dotenv()` does not override real environment variables, which matches the contract.
