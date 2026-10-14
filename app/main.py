"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded; real environment variables win)

Layout of this file
  1. configuration and database handles
  2. time / rounding helpers and the business rules R1-R5 (pure functions)
  3. request models and validation helpers
  4. serialisation (BSON documents -> API JSON)
  5. indexes
  6. employee and attendance endpoints
  7. aggregation pipeline builders + analytics endpoints
  8. explain endpoint
"""
import json
import logging
import os
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from datetime import time as Time
from decimal import Decimal
from fractions import Fraction
from math import floor
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the environment

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so a
# stored instant can never be mistaken for local time.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NO_WORK = ("ABSENT", "LEAVE")
PunchStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AnyStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out window
MAX_TREND_DAYS = 92
MAX_PAGE = 1_000_000

EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 4_102_444_800_000

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# An instant on the wire: a strict integer count of milliseconds. strict=True
# rejects "123", 1.5, 1.0 and true; the bounds reject values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)]


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
def to_dt(epoch_s: int) -> datetime:
    """Whole epoch seconds -> UTC-aware datetime."""
    return datetime.fromtimestamp(epoch_s, tz=UTC)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def dt_to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds using exact integer arithmetic."""
    delta = _aware(dt) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def dt_to_sec(dt: datetime) -> int:
    """datetime -> whole epoch seconds (truncated, R1)."""
    return dt_to_ms(dt) // 1000


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def now_ms_dt() -> datetime:
    """Current time as a UTC datetime with millisecond precision (what BSON keeps)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def resolve_instant(punched_at_ms: Optional[int]) -> int:
    """Epoch seconds for a request: the given instant truncated to whole seconds, or now."""
    if punched_at_ms is None:
        return int(time.time())
    return punched_at_ms // 1000


def hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def attendance_date(punch_in_s: int, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning
    punch-in (earlier than shift_end) belongs to the shift that began the day before."""
    local = datetime.fromtimestamp(punch_in_s, tz=IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < hhmm_to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def shift_instant(date_str: str, hhmm: str, day_offset: int = 0) -> int:
    """Epoch seconds of HH:MM (IST) on the given attendance date (+ day_offset)."""
    h, m = hhmm.split(":")
    day = Date.fromisoformat(date_str) + timedelta(days=day_offset)
    return int(datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST).timestamp())


def compute_late_minutes(punch_in_s: int, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole
    minutes (floor) since shift_start."""
    delta = punch_in_s - shift_instant(date_str, shift_start)
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hundredths(punch_in_s: int, punch_out_s: int) -> int:
    """R4 in integer arithmetic: round-half-up(seconds / 36) == hours * 100.
    floor(secs/36 + 1/2) == floor((2*secs + 36) / 72). No float rounding involved."""
    return (2 * (punch_out_s - punch_in_s) + 36) // 72


def compute_overtime(punch_out_s: int, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) from shift_end to punch_out, only if >= 30."""
    offset = 1 if is_overnight(shift_start, shift_end) else 0
    minutes = (punch_out_s - shift_instant(date_str, shift_end, offset)) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def closing_values(punch_in_s: int, punch_out_s: int, date_str: str, emp: dict) -> tuple:
    """(work_hours, overtime_minutes, half_day) for a closed record (R3-R5)."""
    hundredths = compute_work_hundredths(punch_in_s, punch_out_s)
    overtime = compute_overtime(punch_out_s, date_str, emp["shift_start"], emp["shift_end"])
    return hundredths / 100, overtime, hundredths < HALF_DAY_HUNDREDTHS


def to_fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(Decimal(repr(value)))  # shortest repr, i.e. the decimal a human sees
    return Fraction(value)


def round_half_up(value: Any, places: int) -> float:
    """R8: exact half-up rounding (Python's round() and Mongo's $round are half-even)."""
    scaled = to_fraction(value) * 10**places
    return floor(scaled + Fraction(1, 2)) / 10**places


def as_int(value: Any) -> int:
    return 0 if value is None else int(round(value))


# --------------------------------------------------------------------------- #
# 3. Models and validation helpers
# --------------------------------------------------------------------------- #
def invalid(loc: tuple, msg: str):
    """Raise a FastAPI-style 422 (same body shape as automatic validation errors)."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: tuple) -> Date:
    try:
        return Date.fromisoformat(value)
    except ValueError:
        invalid(loc, "not a valid calendar date (YYYY-MM-DD)")


def month_bounds(month: str) -> tuple:
    """'YYYY-MM' -> (first_day_iso, last_day_iso, first_day, last_day)."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        invalid(("query", "month"), "year out of range")
    first = Date(year, mon, 1)
    last = Date(year, mon, monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def count_working_days(first: Date, last: Date) -> int:
    """R7: Monday-Friday days from first to last inclusive (no holiday calendar)."""
    total, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # ValueError becomes a 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PunchStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Optional fields are simply left out when unchanged. They are declared with a
    # non-Optional type, so an explicit null is a 422 (as in the OpenAPI schema).
    status: AnyStatus = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation (BSON document -> API JSON)
# --------------------------------------------------------------------------- #
def _api_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_history(entry: dict) -> dict:
    changes = {
        field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
        for field, change in (entry.get("changes") or {}).items()
    }
    return {
        "at": ms_or_none(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def to_record(doc: dict) -> dict:
    """Attendance document -> API record. Missing legacy fields get their defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": as_int(doc.get("late_minutes")),
        "overtime_minutes": as_int(doc.get("overtime_minutes")),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def to_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


def emp_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", 1)], unique=True)  # identity + 409 on duplicate create
    employees.create_index([("department", 1), ("emp_code", 1)])  # list by department, sorted
    employees.create_index([("joined_on", 1)])  # headcount (R9) without a department
    employees.create_index([("department", 1), ("joined_on", 1)])  # headcount per department
    # attendance_logs
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True)  # one record per day; race-safe punch-in
    logs.create_index([("date", -1), ("emp_code", 1)])  # list sort order + month/date range scans
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])  # status filter + sort
    logs.create_index([("emp_code", 1), ("punch_in", -1)])  # "most recent punch-in" for punch-out
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # DB not reachable yet: /health will keep retrying
        log.warning("index creation deferred: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms_dt()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    filt = {} if department is None else {"department": department}
    total = employees.count_documents(filt)
    cursor = employees.find(filt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [to_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = emp_or_404(body.emp_code)
    in_s = resolve_instant(body.punched_at)
    date_str = attendance_date(in_s, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": to_dt(in_s),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(in_s, date_str, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return to_record(doc)


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Shared by GET /attendance and the explain endpoint so both run the same query."""
    d_from = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        invalid(("query", "date_from"), "date_from must not be after date_to")
    filt: dict = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if d_from or d_to:
        rng: dict = {}
        if d_from:
            rng["$gte"] = d_from.isoformat()
        if d_to:
            rng["$lte"] = d_to.isoformat()
        filt["date"] = rng
    if status is not None:
        filt["status"] = status
    return {"filter": filt, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    status: Optional[AnyStatus] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q["filter"])
    cursor = (
        logs.find(q["filter"], {"_id": 0})
        .sort(list(ATTENDANCE_SORT.items()))
        .skip(q["skip"])
        .limit(q["limit"])
    )
    return {"items": [to_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = emp_or_404(body.emp_code)
    out_s = resolve_instant(body.punched_at)
    out_dt = to_dt(out_s)

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}}, sort=[("punch_in", -1)])
    if rec is None:
        # Distinguish "never punched in" (404) from "punch-out is before every punch-in" (422).
        if logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None}}, {"_id": 1}) is not None:
            invalid(("body", "punched_at"), "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    in_s = dt_to_sec(rec["punch_in"])
    if out_s <= in_s:
        invalid(("body", "punched_at"), "punched_at must be after punch_in")
    if out_s - in_s > MAX_SHIFT_SECONDS:
        invalid(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")

    work_hours, overtime, half_day = closing_values(in_s, out_s, rec["date"], emp)
    # Compare-and-set: only closes the record if it is still open and unchanged. Of two
    # simultaneous punch-outs exactly one matches; the other gets None -> 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": out_dt, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out, or the record changed concurrently")
    return to_record(updated)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) or isinstance(b, datetime):
        return ms_or_none(a) == ms_or_none(b)
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    date: Annotated[str, Path(pattern=DATE_PATTERN)],
    body: RegularizeIn,
):
    parse_date(date, ("path", "date"))
    emp = emp_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status if "status" in sent else old_status
    in_sent, out_sent = "punch_in" in sent, "punch_out" in sent

    if new_status in NO_WORK:
        if in_sent or out_sent:
            invalid(("body",), "ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = to_dt(body.punch_in // 1000) if in_sent else old_in
        new_out = to_dt(body.punch_out // 1000) if out_sent else old_out
        if new_in is None:
            invalid(("body", "punch_in"), "a presence status requires a punch_in")
        if in_sent and attendance_date(dt_to_sec(new_in), emp["shift_start"], emp["shift_end"]) != date:
            invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and (in_sent or out_sent):
            gap = dt_to_sec(new_out) - dt_to_sec(new_in)
            if gap <= 0:
                invalid(("body", "punch_out"), "punch_out must be after punch_in")
            if gap > MAX_SHIFT_SECONDS:
                invalid(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    if _same(new_status, old_status) and _same(new_in, old_in) and _same(new_out, old_out):
        invalid(("body",), "the request changes nothing")

    # Recompute every derived field from the final state (R2-R5).
    if new_status in NO_WORK:
        work_hours, late, overtime, half_day = None, 0, 0, False
    else:
        in_s = dt_to_sec(new_in)
        late = compute_late_minutes(in_s, date, emp["shift_start"])
        if new_out is not None:
            work_hours, overtime, half_day = closing_values(in_s, dt_to_sec(new_out), date, emp)
        else:
            work_hours, overtime, half_day = None, 0, False

    old = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if not _same(old[k], new[k])}
    entry = {"at": now_ms_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the fields we based the
    # recomputation on, and the history length, are exactly what we read.
    flt: dict = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    update: dict = {"$set": dict(new)}
    history = rec.get("history")
    if isinstance(history, list):
        flt["history"] = {"$size": len(history)}
        update["$push"] = {"history": entry}
    else:  # legacy record: no history array yet
        flt["history"] = None
        update["$set"]["history"] = [entry]
    updated = logs.find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return to_record(updated)


# --------------------------------------------------------------------------- #
# 7. Aggregation pipelines + analytics endpoints
# --------------------------------------------------------------------------- #
# Expression fragments shared by the pipelines. They read the STORED derived
# values (late_minutes, half_day, work_hours) and treat missing ones as 0 / false.
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}
# `date` is "YYYY-MM-DD"; $dayOfWeek is 1=Sunday ... 7=Saturday, so Mon-Fri = 2..6.
IS_WEEKDAY = {
    "$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}},
        [2, 3, 4, 5, 6],
    ]
}
# Presence counted in halves (full day = 2, half day = 1) so every sum stays an integer.
HALVES_ANY_DAY = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}
HALVES_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def log_stats_group(group_id: Any) -> dict:
    """$group spec shared by the monthly and department-summary pipelines."""
    return {
        "_id": group_id,
        "present_halves": {"$sum": HALVES_WEEKDAY},
        "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        "late_total": {"$sum": LATE_MINUTES},
        "ot_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        # Decimal128 keeps the sum of 2-decimal values exact; the mean is formed in Python.
        "work_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, None]}},
        "work_cnt": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": log_stats_group(None)},
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from `employees` so people without logs are still counted (R9)."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$group": log_stats_group(None)},
                ],
                "as": "agg",
            }
        },
        # One stats document per employee, or "missing" when they have no logs.
        {"$set": {"agg": {"$arrayElemAt": ["$agg", 0]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # counts every employee, with or without logs
                "present_halves": {"$sum": "$agg.present_halves"},
                "late_count": {"$sum": "$agg.late_count"},
                "late_total": {"$sum": "$agg.late_total"},
                "leave_count": {"$sum": "$agg.leave_count"},
                "on_duty_count": {"$sum": "$agg.on_duty_count"},
                "work_sum": {"$sum": "$agg.work_sum"},
                "work_cnt": {"$sum": "$agg.work_cnt"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: Date, d_to: Date) -> list:
    """One row per calendar day. Rates are computed as exact integers (1/10000 units)
    so the 4-decimal half-up rounding and the moving average are exact."""
    frm, to = d_from.isoformat(), d_to.isoformat()
    n_days = (d_to - d_from).days + 1
    start_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    rate_i = {
        "$floor": {
            "$divide": [
                {"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]
        }
    }
    has_rate_cond = {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]}
    return [
        # 1. the department's employees, then each one's logs in range (indexed lookup)
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": frm, "$lte": to}}},
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "halves": HALVES_ANY_DAY,
                            "late": {"$cond": [IS_LATE, 1, 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {"$group": {"_id": "$logs.date", "halves": {"$sum": "$logs.halves"}, "late_count": {"$sum": "$logs.late"}}},
        # 2. gap filling: a zero row for every calendar day in range, merged by date below
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "d": {"$range": [0, n_days]}}},
                    {"$unwind": "$d"},
                    {
                        "$project": {
                            "_id": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$d"}},
                                }
                            },
                            "halves": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {"$group": {"_id": "$_id", "halves": {"$sum": "$halves"}, "late_count": {"$sum": "$late_count"}}},
        {"$sort": {"_id": 1}},
        # 3. calendar facts and headcount per day (R9)
        {"$set": {"is_wd": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, [2, 3, 4, 5, 6]]}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$_id"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # 4. attendance rate in 1/10000 units, null unless a working day with headcount > 0
        {"$set": {"rate_i": {"$cond": [has_rate_cond, rate_i, None]}}},
        {"$set": {"has_rate": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}}},
        # 5. moving window: this row and up to 6 preceding rows inside the range
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "win_sum": {"$sum": "$rate_i", "window": {"documents": [-6, 0]}},
                    "win_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_wd": 1,
                "headcount": 1,
                "halves": 1,
                "late_count": 1,
                "rate_i": 1,
                "win_sum": 1,
                "win_n": 1,
            }
        },
    ]


MonthQuery = Annotated[str, Query(pattern=MONTH_PATTERN, description="YYYY-MM")]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    start, end, first, last = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    agg = next(iter(logs.aggregate(employee_monthly_pipeline(emp_code, start, end))), {})

    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last)  # R7
    halves = as_int(agg.get("present_halves"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": as_int(agg.get("leave_count")),
        "late_count": as_int(agg.get("late_count")),
        "total_late_minutes": as_int(agg.get("late_total")),
        "total_overtime_minutes": as_int(agg.get("ot_total")),
        "attendance_pct": round_half_up(Fraction(halves, 2 * working_days) * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    start, end, _, _ = month_bounds(month)
    items = []
    for row in employees.aggregate(department_summary_pipeline(start, end, department)):
        work_cnt = as_int(row.get("work_cnt"))
        avg = round_half_up(to_fraction(row["work_sum"]) / work_cnt, 2) if work_cnt else None
        items.append(
            {
                "department": row["_id"],
                "headcount": as_int(row["headcount"]),
                "present_days": as_int(row.get("present_halves")) / 2,
                "avg_work_hours": avg,
                "late_count": as_int(row.get("late_count")),
                "total_late_minutes": as_int(row.get("late_total")),
                "leave_count": as_int(row.get("leave_count")),
                "on_duty_count": as_int(row.get("on_duty_count")),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQuery,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end, _, _ = month_bounds(month)
    items = [
        {
            "rank": as_int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": as_int(r["total_late_minutes"]),
            "late_count": as_int(r["late_count"]),
        }
        for r in logs.aggregate(late_leaderboard_pipeline(start, end, limit, department))
    ]
    return {"month": month, "items": items}


def trend_range(from_: str, to: str) -> tuple:
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_to < d_from:
        invalid(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        invalid(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
):
    d_from, d_to = trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees.aggregate(department_trend_pipeline(department, d_from, d_to)):
        n = as_int(r["win_n"])
        rate = None if r["rate_i"] is None else as_int(r["rate_i"]) / 10000
        # mean of the (already 4-decimal) rates in the window, half-up to 4 decimals:
        # floor(S/n + 1/2) == (2S + n) // (2n) in integer arithmetic.
        moving = None if n == 0 else ((2 * as_int(r["win_sum"]) + n) // (2 * n)) / 10000
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_wd"]),
                "headcount": as_int(r["headcount"]),
                "present_count": as_int(r["halves"]) / 2,
                "late_count": as_int(r["late_count"]),
                "attendance_rate": rate,
                "moving_avg_7d": moving,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def run_explain(command: dict) -> Any:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON types (Timestamp, Int64, dates ...) -> plain JSON
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(value: Any, name: str):
    if value is None:
        invalid(("query", name), "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    status: Optional[AnyStatus] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN)] = None,
    to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch builds the query with the very same builder the real endpoint uses.
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": q["filter"],
            "sort": ATTENDANCE_SORT,
            "skip": q["skip"],
            "limit": q["limit"],
            "projection": {"_id": 0},
        }
    elif endpoint == "employee_monthly":
        _require(emp_code, "emp_code")
        start, end, _, _ = month_bounds(_require(month, "month"))
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, start, end), "cursor": {}}
    elif endpoint == "department_summary":
        start, end, _, _ = month_bounds(_require(month, "month"))
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(start, end, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        start, end, _, _ = month_bounds(_require(month, "month"))
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": late_leaderboard_pipeline(start, end, limit, department),
            "cursor": {},
        }
    else:  # department_trend
        _require(department, "department")
        d_from, d_to = trend_range(_require(from_, "from"), _require(to, "to"))
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, d_from, d_to), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
