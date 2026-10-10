# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+; uses `$setWindowFields`, `$rank`, `$dateAdd`). All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or use a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `GET /health` returns 503 until MongoDB answers.

## Notes
- Instants are epoch milliseconds at the API and BSON dates in MongoDB; everything is tz-aware UTC internally.
- Analytics read the stored derived fields; rounding is done half-up with integers/`Decimal`, never float `round()`.
- The department trend generates its day rows inside the database with `$range` (no Python loop), so weekends and empty days are always present.

## Testing status (please read)
The rules R1-R5, CRUD, punch-out, PATCH/history and validation were tested against an in-memory mock.
The sandbox I built this in had no MongoDB server, so the **four aggregation pipelines and the explain endpoint have not been run against a real MongoDB**. Please run them against the sample data and a larger seed before submitting, and check `/admin/explain/*` for `COLLSCAN`.
