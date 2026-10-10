"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Layout of this file
  1. configuration and time helpers   (instants <-> epoch ms, always tz-aware UTC)
  2. business rules R1-R5             (one implementation, reused by punch-in, punch-out, PATCH)
  3. request models and validation
  4. indexes
  5. employees / attendance endpoints
  6. aggregation pipelines + analytics endpoints
  7. explain endpoint (uses the very same query builders as the endpoints)
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
GRACE_SECONDS = 600
MAX_TREND_DAYS = 92

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Epoch milliseconds. Strict: floats, strings and bools are rejected; the lower bound rejects "seconds".
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


# --------------------------------------------------------------------------- #
# 1. Time helpers
# --------------------------------------------------------------------------- #
def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def trunc(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else as_utc(dt).replace(microsecond=0)


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def _bad(loc: str, msg: str, where: str = "query") -> HTTPException:
    return HTTPException(422, [{"loc": [where, loc], "msg": msg, "type": "value_error"}])


def parse_date(s: str, name: str = "date", where: str = "query") -> date:
    if not _DATE_RE.match(s):
        raise _bad(name, "must be YYYY-MM-DD", where)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise _bad(name, "not a valid calendar date", where)


def month_bounds(month: str) -> tuple[str, str]:
    if not _MONTH_RE.match(month) or int(month[:4]) < 1:
        raise _bad("month", "must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


# --------------------------------------------------------------------------- #
# 2. Business rules (R1 - R5). Single source of truth.
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight-shift punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Aware IST start and end of the shift that belongs to attendance day `date_str`."""
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(d, hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    start, _ = shift_window(date_str, shift_start, shift_end)
    secs = int((trunc(punch_in) - start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift_end, counted only when >= 30."""
    _, end = shift_window(date_str, shift_start, shift_end)
    mins = int((trunc(punch_out) - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up (Decimal, never float round())."""
    secs = int((trunc(punch_out) - trunc(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(emp: dict, date_str: str, status: str, p_in: Optional[datetime], p_out: Optional[datetime]) -> dict:
    """All derived fields for a record in its final state (R2-R5, and the zero rows of DATA_MODEL)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or p_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(p_in, date_str, emp["shift_start"], emp["shift_end"])
    if p_out is not None:
        wh = compute_work_hours(p_in, p_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(p_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = wh < 4.5  # R5, on the rounded value
    return out


def check_out_after_in(p_in: datetime, p_out: datetime):
    if p_out <= p_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if p_out - p_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")



TIME_FIELDS = ("punch_in", "punch_out")


def _change_val(field: str, v: Any) -> Any:
    return to_ms(v) if field in TIME_FIELDS and isinstance(v, datetime) else v


def record_out(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _change_val(k, v.get("from")), "to": _change_val(k, v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }



class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



INDEXES = [
    # (collection, keys, options)
    ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined"}),
    ("employees", [("joined_on", ASCENDING)], {"name": "joined"}),
    # one record per (emp_code, date): the punch-in race guard, monthly analytics, lookups by natural key
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_date_unique"}),
    # GET /attendance sort order, month range scans for leaderboard / trend lookups
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
    # status filter + the same sort
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
    # punch-out: latest record with punch_in <= t
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
]


def ensure_indexes():
    for coll, keys, opts in INDEXES:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError as e:  # e.g. pre-existing duplicate data blocks a unique index
            log.error("index %s on %s failed: %s", opts.get("name"), coll, e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as e:  # never stop the app from starting; /health reports the DB state
        log.error("startup index creation failed: %s", e)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# API endpoints
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    t = to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": t}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = trunc(rec["punch_in"])
    check_out_after_in(p_in, t)
    fields = derive(emp, rec["date"], rec["status"], p_in, t)
    fields["punch_out"] = t
    # Atomic claim: only succeeds while punch_out is still null, so simultaneous requests yield one 200.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint so both run the identical query."""
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise _bad("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q, [("date", DESCENDING), ("emp_code", ASCENDING)], (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q).sort(sort).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", "path")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    sent = body.model_fields_set
    old_status = rec["status"]
    old_in, old_out = trunc(rec.get("punch_in")), trunc(rec.get("punch_out"))
    new_status = body.status if "status" in sent and body.status else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            raise HTTPException(422, "punch times cannot be supplied for ABSENT or LEAVE")
        new_in = new_out = None
    else:
        new_in = to_dt(body.punch_in) if "punch_in" in sent else old_in
        new_out = to_dt(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_out_after_in(new_in, new_out)

    if new_status == old_status and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp, date, new_status, new_in, new_out)}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}

    entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic lock: the write only applies if nobody changed history (or punch fields) since we read.
    guard: dict = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }
    if n:
        guard["history"] = {"$size": n}
    else:  # empty or missing (legacy documents)
        guard["$or"] = [{"history": {"$size": 0}}, {"history": {"$exists": False}}]
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_vals, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# Analytics aggregation pipelines
def _round_half_up(x: Decimal, places: str) -> float:
    return float(x.quantize(Decimal(places), rounding=ROUND_HALF_UP))


# --- shared expressions ---
IS_PRESENT = {"$in": ["$status", PRESENCE]}
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # half day = 1 half, else 2
DOW_OF_DATE = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}}
IS_WEEKDAY_DOC = {"$in": [DOW_OF_DATE, [2, 3, 4, 5, 6]]}  # Mongo: 1 = Sunday ... 7 = Saturday


def _stats_group() -> dict:
    """Per-scope totals over stored derived values (never recomputed from punch times)."""
    return {"$group": {
        "_id": None,
        # present_days in halves, Mon-Fri only (R5-R7)
        "halves": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY_DOC]}, HALVES, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
        "late_total": {"$sum": LATE},
        "ot_total": {"$sum": OVERTIME},
        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        # work hours as integer hundredths so the mean can be rounded exactly, half-up, in Python
        "wh_cents": {"$sum": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                                        {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "wh_n": {"$sum": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]}},
    }}


def count_weekdays(a: date, b: date) -> int:
    if a > b:
        return 0
    full, rem = divmod((b - a).days + 1, 7)
    return full * 5 + sum(1 for i in range(rem) if (a + timedelta(days=i)).weekday() < 5)


# --- employee monthly ---
def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, _stats_group()]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    row = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end))), {})
    working = count_weekdays(max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"])), date.fromisoformat(end))
    halves = int(row.get("halves", 0))
    pct = None if working == 0 else _round_half_up(Decimal(halves) * 50 / Decimal(working), "0.01")
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": _round_half_up(Decimal(halves) / 2, "0.01"),
        "leave_days": int(row.get("leave", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("late_total", 0)),
        "total_overtime_minutes": int(row.get("ot_total", 0)),
        "attendance_pct": pct,
    }


# --- department summary ---
def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Main collection is `employees`, so headcount (R9) includes people with no logs at all and
    excludes people who have not joined by month end. Logs are joined per employee via the
    (emp_code, date) index and pre-aggregated inside the $lookup."""
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department

    def first(name: str) -> dict:
        return {"$ifNull": [{"$arrayElemAt": [f"$s.{name}", 0]}, 0]}

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, _stats_group()],
            "as": "s",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "halves": {"$sum": first("halves")},
            "late_count": {"$sum": first("late_count")},
            "late_total": {"$sum": first("late_total")},
            "leave": {"$sum": first("leave")},
            "on_duty": {"$sum": first("on_duty")},
            "wh_cents": {"$sum": first("wh_cents")},
            "wh_n": {"$sum": first("wh_n")},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(start, end, department)):
        n = int(r["wh_n"])
        avg = None if n == 0 else _round_half_up(Decimal(int(r["wh_cents"])) / (Decimal(100) * n), "0.01")
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": _round_half_up(Decimal(int(r["halves"])) / 2, "0.01"),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_total"]),
            "leave_count": int(r["leave"]),
            "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


# --- late leaderboard ---
def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    p: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code",
                    "total": {"$sum": LATE},
                    "cnt": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}}}},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here
    ]
    if department:
        p.append({"$match": {"e.department": department}})  # rank inside the department only
    p += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are kept
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": "$total", "late_count": "$cnt"}},
    ]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department))
    ]
    return {"month": month, "items": items}


# --- department trend ---
def trend_range(from_: str, to: str) -> tuple[date, date]:
    f, t = parse_date(from_, "from"), parse_date(to, "to")
    if t < f:
        raise _bad("to", "'to' must not be before 'from'")
    if (t - f).days + 1 > MAX_TREND_DAYS:
        raise _bad("to", f"range must not exceed {MAX_TREND_DAYS} days")
    return f, t


def trend_pipeline(department: str, f: date, t: date) -> list:
    """One row per calendar day, generated inside the database from $range (so empty days and weekends
    exist even when there are no logs at all), then enriched and windowed with $setWindowFields.
    Rates are carried as integer ten-thousandths so half-up rounding is exact."""
    n = (t - f).days + 1
    start_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    rate_den = {"$multiply": [2, "$headcount"]}  # rate = (halves / 2) / headcount = halves / (2 * headcount)
    rate_num = {"$multiply": ["$halves", 10000]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "emps": {"$push": {"c": "$emp_code", "j": "$joined_on"}}}},
        {"$project": {"_id": 0, "emps": 1, "codes": "$emps.c", "days": {"$map": {
            "input": {"$range": [0, n]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}}}}}},
        {"$unwind": "$days"},
        {"$project": {"day": "$days", "emps": 1, "codes": 1}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$day", "codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {"_id": None,
                            "halves": {"$sum": {"$cond": [IS_PRESENT, HALVES, 0]}},
                            "late": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}}}},
            ],
            "as": "a",
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": "$emps", "as": "e", "cond": {"$lte": ["$$e.j", "$day"]}}}},
            "halves": {"$ifNull": [{"$arrayElemAt": ["$a.halves", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$a.late", 0]}, 0]},
            "is_wd": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$day", "format": "%Y-%m-%d"}}},
                              [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {"rate_x": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": [2, rate_num]}, rate_den]}, {"$multiply": [2, rate_den]}]}},
            None]}}},
        {"$sort": {"day": 1}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "w_sum": {"$sum": "$rate_x", "window": {"documents": [-6, "current"]}},
            "w_cnt": {"$sum": {"$cond": [{"$eq": ["$rate_x", None]}, 0, 1]}, "window": {"documents": [-6, "current"]}},
        }}},
        {"$addFields": {"mov_x": {"$cond": [
            {"$gt": ["$w_cnt", 0]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$w_sum"]}, "$w_cnt"]}, {"$multiply": [2, "$w_cnt"]}]}},
            None]}}},
        {"$project": {"_id": 0, "date": "$day", "is_wd": 1, "headcount": 1, "halves": 1,
                      "late_count": 1, "rate_x": 1, "mov_x": 1}},
        {"$sort": {"date": 1}},
    ]


def _x(v) -> Optional[float]:
    return None if v is None else _round_half_up(Decimal(int(v)) / Decimal(10000), "0.0001")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    f, t = trend_range(from_, to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    items = [
        {"date": r["date"], "is_working_day": bool(r["is_wd"]), "headcount": int(r["headcount"]),
         "present_count": float(r["halves"]) / 2, "late_count": int(r["late_count"]),
         "attendance_rate": _x(r.get("rate_x")), "moving_avg_7d": _x(r.get("mov_x"))}
        for r in db.employees.aggregate(trend_pipeline(department, f, t))
    ]
    return {"department": department, "items": items}


# Query execution plans
def _need(**vals):
    missing = [k for k, v in vals.items() if v is None]
    if missing:
        raise HTTPException(422, [{"loc": ["query", k], "msg": "field required", "type": "missing"} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner: dict = {"find": coll, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            coll, pipe = "attendance_logs", monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            _need(month=month)
            coll, pipe = "employees", summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            coll, pipe = "attendance_logs", leaderboard_pipeline(*month_bounds(month), limit, department)
        else:
            _need(department=department, **{"from": from_}, to=to)
            f, t = trend_range(from_, to)
            coll, pipe = "employees", trend_pipeline(department, f, t)
        inner = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    res = db.command({"explain": inner, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(res, json_options=json_util.RELAXED_JSON_OPTIONS))
    return JSONResponse({"endpoint": endpoint, "collection": coll, "explain": plain})
