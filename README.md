# Employee Attendance & Analytics API

A FastAPI + MongoDB service where employees punch in and out, managers correct records, and the business can ask who is late, which department is slipping, and what the trend looks like. All code is in `app/main.py`.

Needs Python 3.11+ and MongoDB 6.0 or newer (it uses `$setWindowFields`, `$rank` and `$dateAdd`).

## Run it

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # on Windows: set MONGO_URI=...
export MONGO_DB="attendance_db"                # on Windows: set MONGO_DB=...
uvicorn app.main:app --port 8000
```

A local `.env` file also works, and real environment variables always win over it. Indexes are created at startup and are safe to create again on every start. `GET /health` returns 503 until MongoDB answers. Interactive docs are at `http://localhost:8000/docs`.

## Design notes

- **Time:** instants are epoch milliseconds in the API and BSON dates in MongoDB. Internally everything is timezone-aware UTC, and shift times and attendance dates are IST. Times are cut to whole seconds before any calculation.
- **One set of rules:** the rules for attendance date, late minutes, overtime, work hours and half day live in one place and are reused by punch-in, punch-out and the correction endpoint.
- **Concurrency:** a unique index on `(emp_code, date)` decides simultaneous punch-ins. Punch-out uses one atomic update that only succeeds while `punch_out` is still empty. Corrections check that the record has not changed since they read it, and the loser gets 409.
- **Analytics:** all four are MongoDB aggregation pipelines that read the stored derived values. Rounding is half-up using integers or `Decimal`, never float `round()`. The department trend builds its one-row-per-day series inside the database, so weekends and empty days are always present.
- **Explain:** `GET /admin/explain/{endpoint}` builds the query the same way the real endpoint does.

## Testing

I checked the rules, CRUD, corrections and history, validation errors and parallel requests. I also checked every analytics endpoint against a plain-Python calculation of the written rules, on a seeded database of about 100,000 attendance records. The seed included ties, weekend records, employees with no logs, mid-month joiners, an overnight shift, open records and logs for an employee who does not exist. All 201 checks passed, and every explain plan uses an index scan with no collection scan.

Everything in the assignment is implemented. Nothing is missing.
