"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

Layout of this file (everything lives here, as the assignment requires):
  1. configuration + indexes
  2. time helpers and the business rules R1-R5 (pure functions, no database)
  3. request models and response serializers
  4. endpoints: employees, attendance (punch-in/out, list, regularize)
  5. analytics: aggregation-pipeline builders + endpoints
  6. explain endpoint (re-uses exactly the same builders as the real endpoints)

Two conventions worth knowing before reading:
  * MongoDB stores instants as BSON dates (UTC). The client is created with tz_aware=True so every
    datetime we read is timezone-aware UTC. The API speaks epoch milliseconds, converted at the edge.
  * MongoDB's $round is round-half-to-EVEN, but R8 wants half-UP. So pipelines return exact integer
    sums/counts and `half_up_int` does the final rounding with integer arithmetic (no float error).
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (default): variables already in the environment take priority

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True -> PyMongo returns aware UTC datetimes instead of naive ones (see DATA_MODEL.md).
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index with the same name and spec is a no-op when it already exists."""
    global _indexes_ready
    # employees ---------------------------------------------------------------------------
    # Business key must be unique, enforced by the database (concurrent creates -> one wins).
    # Also serves every lookup by emp_code (punch-in, leaderboard $lookup, explain).
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # GET /employees?department=X sorted by emp_code, and the first stage of the department
    # summary / trend pipelines ($match department, $sort department+emp_code).
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # attendance_logs ----------------------------------------------------------------------
    # The one-record-per-employee-per-day invariant (punch-in race) AND the access path for
    # "all days of one employee in a date range" (punch-out lookup, monthly summary, $lookup
    # from employees in the department summary and trend).
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # GET /attendance sorted by date desc, emp_code asc (optionally with a date range), and the
    # month scan at the start of the late leaderboard.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # GET /attendance?status=X: equality first, then the same sort order, so the status filter
    # is answered from the index without an in-memory sort.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health reports 503 and retries index creation once Mongo answers.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
NO_PUNCH_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
MAX_SHIFT = timedelta(hours=24)  # punch-out / correction upper bound
HALF_DAY_HOURS = 4.5  # R5


def as_utc(dt: datetime) -> datetime:
    """Normalise to aware UTC (a naive datetime is assumed to already be UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive datetime -> epoch ms using integer arithmetic (no float rounding)."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    return time(int(value[:2]), int(value[3:5]))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier
    than shift_end (IST) belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Absolute start/end instants of the shift that belongs to attendance date `day`."""
    d = Date.fromisoformat(day)
    start = datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)  # the shift ends on the next calendar day
    return start, end


def half_up_int(num: int, den: int) -> int:
    """floor(num/den + 1/2) for non-negative ints: round-half-UP with exact integer maths."""
    return (2 * num + den) // (2 * den)


def round_half_up(num: int, den: int, places: int) -> float:
    """num/den rounded half-up to `places` decimals (R4, R8)."""
    return half_up_int(num * 10**places, den) / 10**places


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since
    shift start (not since the end of the grace period)."""
    start, _ = shift_bounds(day, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) after shift end, counted only when >= 30."""
    _, end = shift_bounds(day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (Python's round() is banker's rounding)."""
    return round_half_up(int((punch_out - punch_in).total_seconds()), 3600, 2)


def derive_fields(emp: dict, status: str, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All server-computed fields for a record (DATA_MODEL.md, 'Stored derived values')."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp["shift_start"], emp["shift_end"])
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < HALF_DAY_HOURS,  # R5: uses the ROUNDED value
    }


# --------------------------------------------------------------------------- #
# 3. Models, validation helpers, serializers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: floats, strings and booleans are rejected (422); range rejects "seconds-looking" values.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
MonthStr = Annotated[str, Query(pattern=MONTH_PATTERN)]


def unprocessable(msg: str, loc: str = "query") -> HTTPException:
    """422 in FastAPI's default body shape."""
    return HTTPException(422, detail=[{"loc": [loc], "msg": msg, "type": "value_error"}])


def parse_date_param(value: str, name: str) -> Date:
    if not DATE_RE.match(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD", name)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a valid calendar date", name)


def month_bounds(month: str) -> tuple[Date, Date]:
    try:
        year, mon = int(month[:4]), int(month[5:7])
        return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])
    except ValueError:
        raise unprocessable("month must be a valid YYYY-MM", "month")


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # client-sent derived fields are ignored
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null; omit it to leave it unchanged")
        return self


def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created) if created is not None else None,
    }


def _ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return dt_to_ms(value) if value is not None else None


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        before, after = change.get("from"), change.get("to")
        if field in ("punch_in", "punch_out"):  # BSON dates nested in an object: easy to forget
            before, after = _ms_or_none(before), _ms_or_none(after)
        changes[field] = {"from": before, "to": after}
    return {"at": dt_to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """Stored document -> API shape. Never exposes _id; fills defaults for legacy records."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(e) for e in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created_at = datetime.now(UTC)
    created_at = created_at.replace(microsecond=created_at.microsecond // 1000 * 1000)  # BSON keeps ms
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    skip = min((page - 1) * page_size, 10**9)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": db.employees.count_documents(q),  # filtered total, not the whole collection
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": emp["emp_code"],
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, body.status, day, ts, None),
        "history": [],
    }
    try:
        # Atomic: the unique (emp_code, date) index lets exactly one of N simultaneous
        # punch-ins insert; the rest raise DuplicateKeyError and become 409.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    for _ in range(3):
        # "Most recent record whose punch_in is at or before punched_at" - works across midnight
        # because the record is found by punch_in, not by the calendar date of punch_out.
        rec = db.attendance_logs.find_one(
            {"emp_code": emp["emp_code"], "punch_in": {"$lte": out}}, sort=[("date", DESCENDING)]
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        punch_in_at = as_utc(rec["punch_in"])
        if out <= punch_in_at:
            raise unprocessable("punched_at must be after punch_in", "punched_at")
        if out - punch_in_at > MAX_SHIFT:
            raise unprocessable("punched_at is more than 24 hours after punch_in", "punched_at")
        derived = derive_fields(emp, rec["status"], rec["date"], punch_in_at, out)
        # Compare-and-set: only close the record if it is still open AND punch_in/status are what we
        # computed from. Of N simultaneous punch-outs exactly one matches; the others get None.
        updated = db.attendance_logs.find_one_and_update(
            {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
            {"$set": {"punch_out": out, **derived}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return serialize_attendance(updated)
        # Lost the race: next loop iteration re-reads and returns 409 (closed) or recomputes (edited).
    raise HTTPException(409, "record changed concurrently")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    cursor = db.attendance_logs.find(
        spec["filter"], {"_id": 0}, sort=spec["sort"], skip=spec["skip"], limit=spec["limit"]
    )
    return {
        "items": [serialize_attendance(d) for d in cursor],
        "total": db.attendance_logs.count_documents(spec["filter"]),
        "page": page,
        "page_size": page_size,
    }


def attendance_find_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The one place the attendance-list query is built; GET /attendance and the explain endpoint share it."""
    lo = parse_date_param(date_from, "date_from") if date_from is not None else None
    hi = parse_date_param(date_to, "date_to") if date_to is not None else None
    if lo and hi and lo > hi:
        raise unprocessable("date_from must not be after date_to", "date_from")
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = lo.isoformat()
        if hi:
            q["date"]["$lte"] = hi.isoformat()
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": min((page - 1) * page_size, 10**9),
        "limit": page_size,
    }


REGULARIZE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date_param(date, "date")
    emp = get_employee_or_404(emp_code)
    for _ in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
        new_status = body.status or cur_status
        given_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
        given_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

        # ---- decide the final state, rejecting impossible combinations (422) ----
        if new_status in NO_PUNCH_STATUSES:
            if given_in is not None or given_out is not None:
                raise unprocessable(f"status {new_status} clears punch times; do not send punch_in/punch_out", "body")
            new_in = new_out = None
        else:
            new_in = given_in if given_in is not None else cur_in
            new_out = given_out if given_out is not None else cur_out
            if new_in is None:
                raise unprocessable("a presence status requires punch_in", "punch_in")
            new_in = as_utc(new_in)
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
                raise unprocessable("punch_in must stay on the record's attendance date", "punch_in")
            if new_out is not None:
                new_out = as_utc(new_out)
                if not (new_in < new_out <= new_in + MAX_SHIFT):
                    raise unprocessable("punch_out must be after punch_in and within 24 hours", "punch_out")

        # "A request that changes nothing is 422" is judged on the fields the client may change.
        if new_status == cur_status and _same(new_in, cur_in) and _same(new_out, cur_out):
            raise unprocessable("request changes nothing", "body")

        # ---- recompute derived fields and diff every field against the stored value ----
        new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                      **derive_fields(emp, new_status, date, new_in, new_out)}
        old_values = {
            "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
            "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
            "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False),
        }
        changes = {
            f: {"from": old_values[f], "to": new_values[f]}
            for f in REGULARIZE_FIELDS
            if not _same(old_values[f], new_values[f])
        }
        entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # ---- compare-and-set write ----
        # The filter pins everything we based the decision on, including the history LENGTH. If another
        # correction/punch-out landed after our read, nothing matches, we re-read and recompute, so no
        # history entry is ever lost or overwritten. $push appends; entries are never rewritten.
        history = rec.get("history")
        cas = {"_id": rec["_id"], "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
               "history": {"$size": len(history)} if isinstance(history, list) else None}
        updated = db.attendance_logs.find_one_and_update(
            cas, {"$set": new_values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return serialize_attendance(updated)
    raise HTTPException(409, "record was modified concurrently; please retry")


# --------------------------------------------------------------------------- #
# 5. Analytics: pipeline builders (shared with the explain endpoint) + endpoints
# --------------------------------------------------------------------------- #
# Reusable pieces of aggregation expressions ------------------------------------------------
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
# $dayOfWeek: 1 = Sunday ... 7 = Saturday. The `date` string is parsed as a calendar date, so
# no time zone can shift it.
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}
IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# Present-days in HALF units (full day = 2, half day = 1) so everything stays an exact integer.
PRESENT_HALVES = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}


def log_accumulators() -> dict:
    """$group accumulators over attendance_logs, used by BOTH the monthly and department summaries.
    Everything reads STORED derived values (late_minutes, work_hours, half_day ...), never recomputes."""
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return {
        # R7: present_days counts Mon-Fri records only (weekend records still feed late/overtime).
        "present2": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_HALVES, 0]}},
        # work_hours in hundredths of an hour (they are 2-decimal values) -> exact integer sums.
        "wh_sum": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "wh_cnt": {"$sum": {"$cond": [has_hours, 1, 0]}},
        "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        "late_total": {"$sum": LATE_MINUTES},
        "ot_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, **log_accumulators()}},
    ]


def count_working_days(first: Date, last: Date, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last, no holiday calendar."""
    day, count = max(first, Date.fromisoformat(joined_on)), 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}
    working_days = count_working_days(first, last, emp["joined_on"])
    present2 = int(agg.get("present2", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present2 / 2,
        "leave_days": int(agg.get("leave_count", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("late_total", 0)),
        "total_overtime_minutes": int(agg.get("ot_total", 0)),
        # present_days / working_days * 100  ==  present2 * 50 / working_days
        "attendance_pct": round_half_up(present2 * 50, working_days, 2) if working_days else None,
    }


ACC_NAMES = list(log_accumulators())


def summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    """Employees-first, so people with ZERO logs still produce a row (headcount, R9)."""
    pipeline: list = []
    if department is not None:
        pipeline.append({"$match": {"department": department}})
    pipeline += [
        # Index-ordered scan of employees (dept_emp_code) - also keeps the plan free of COLLSCAN
        # when no department filter is given.
        {"$sort": {"department": 1, "emp_code": 1}},
        # Per employee, aggregate that month's logs INSIDE the lookup: one tiny document per
        # employee comes back instead of an array of every log (keeps documents far below 16 MB).
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {"_id": None, **log_accumulators()}},
            ],
            "as": "m",
        }},
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},  # keep employees with no logs
        {"$group": {
            "_id": "$department",
            # R9: joined on or before the last day of the month; whether or not they have logs.
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", last.isoformat()]}, 1, 0]}},
            **{name: {"$sum": f"$m.{name}"} for name in ACC_NAMES},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(first, last, department)):
        wh_cnt = int(r["wh_cnt"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["present2"]) / 2,
            # mean over RECORDS: (sum of hundredths / 100) / record count, half-up to 2 decimals
            "avg_work_hours": round_half_up(int(r["wh_sum"]), 100 * wh_cnt, 2) if wh_cnt else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_total"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking (1,2,2,4) computed by the database. $rank needs a single sort key.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking (ties at the cutoff all stay)
        {"$sort": {"total_late_minutes": -1, "_id": 1}},  # ties ordered by emp_code
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows
    ]}


def midnight_utc(d: Date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def validate_trend_range(from_: str, to: str) -> tuple[Date, Date]:
    start, end = parse_date_param(from_, "from"), parse_date_param(to, "to")
    if end < start:
        raise unprocessable("to must not be before from", "to")
    if (end - start).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days", "to")
    return start, end


def trend_pipeline(department: str, start: Date, end: Date) -> list:
    """One row per calendar day. Steps: (1) turn every employee and every log into a small event,
    (2) sum events per day, (3) $densify to add the missing days, (4) running sum = headcount,
    (5) rate, (6) 7-row moving window. All inside MongoDB."""
    f, t = start.isoformat(), end.isoformat()
    log_event = {"$map": {"input": "$logs", "as": "l",
                          "in": {"date": "$$l.date", "hc": 0, "p2": "$$l.p2", "late": "$$l.late"}}}
    # An employee adds +1 to headcount from max(joined_on, from); not at all if joined after `to`.
    join_event = {"$cond": [{"$lte": ["$joined_on", t]},
                            [{"date": {"$cond": [{"$lt": ["$joined_on", f]}, f, "$joined_on"]},
                              "hc": 1, "p2": 0, "late": 0}],
                            []]}
    # Seed event: guarantees the stream reaching $densify always contains the first day, even when no
    # employee joins and no log exists inside [from, to] (e.g. everyone in the department joins later).
    # A department with >= 1 employee (checked before the pipeline runs) always yields at least one seed.
    seed_event = [{"date": f, "hc": 0, "p2": 0, "late": 0}]
    window_all_prior = {"documents": ["unbounded", "current"]}
    window_7 = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, inside the range only
    return [
        {"$match": {"department": department}},
        {"$sort": {"department": 1, "emp_code": 1}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1,
                              "p2": PRESENT_HALVES,  # any day of the week (present_count, not present_days)
                              "late": {"$cond": [IS_LATE, 1, 0]}}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [seed_event, join_event, log_event]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "hc_new": {"$sum": "$events.hc"},
                    "p2": {"$sum": "$events.p2"}, "late": {"$sum": "$events.late"}}},
        {"$project": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                      "hc_new": 1, "p2": 1, "late": 1}},
        # Gap filling: add a row for every missing day. Upper bound is exclusive, hence end + 1 day.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day",
                                              "bounds": [midnight_utc(start), midnight_utc(end + timedelta(days=1))]}}},
        {"$addFields": {"hc_new": {"$ifNull": ["$hc_new", 0]}, "p2": {"$ifNull": ["$p2", 0]},
                        "late": {"$ifNull": ["$late", 0]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"headcount": {"$sum": "$hc_new", "window": window_all_prior}}}},
        {"$addFields": {"is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]}}},
        # rate in 1/10000 units, half-up, with exact integer maths:
        #   rate = (p2/2)/headcount  ->  floor(p2*10000/(2*headcount) + 1/2) = floor((p2*10000 + hc)/(2*hc))
        {"$addFields": {"rate4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$rate4", "window": window_7},      # $sum ignores null rates
            "win_cnt": {"$sum": "$has_rate", "window": window_7}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                      "is_working_day": 1, "headcount": 1, "p2": 1, "late": 1, "rate4": 1,
                      "win_sum": 1, "win_cnt": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start, end = validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, start, end)):
        rate4 = None if r.get("rate4") is None else int(round(r["rate4"]))
        win_cnt = int(r["win_cnt"])
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": int(r["p2"]) / 2,
            "late_count": int(r["late"]),
            "attendance_rate": None if rate4 is None else rate4 / 10000,
            # mean of the non-null (4-decimal) rates in the window, half-up to 4 decimals
            "moving_avg_7d": half_up_int(int(round(r["win_sum"])), win_cnt) / 10000 if win_cnt else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**values) -> None:
    missing = [k for k, v in values.items() if v is None]
    if missing:
        raise HTTPException(422, detail=[{"loc": ["query", k], "msg": "field required", "type": "missing"} for k in missing])


def _explain(command_body: dict) -> dict:
    result = db.command({"explain": command_body, "verbosity": "executionStats"})
    # Raw explain output made JSON-safe (Timestamps, dates, Int64 ...) without altering its content.
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
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
    """Each branch builds the query with the SAME builder function the real endpoint uses."""
    if endpoint == "attendance_list":
        spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        body = {"find": collection, "filter": spec["filter"], "sort": dict(spec["sort"]),
                "skip": spec["skip"], "limit": spec["limit"]}
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        body = {"aggregate": collection, "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "employees"
        body = {"aggregate": collection, "pipeline": summary_pipeline(first, last, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        body = {"aggregate": collection, "pipeline": leaderboard_pipeline(first, last, limit, department), "cursor": {}}
    else:  # department_trend
        _require(department=department, **{"from": from_, "to": to})
        start, end = validate_trend_range(from_, to)
        collection = "employees"
        body = {"aggregate": collection, "pipeline": trend_pipeline(department, start, end), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(body)}