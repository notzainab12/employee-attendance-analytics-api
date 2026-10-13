"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml.  Stored data: DATA_MODEL.md.  Review notes: REVIEW.md.  Design notes: DECISIONS.md.

Layout of this file
  1. Setup & indexes
  2. Time / rounding helpers and the business rules R1-R5 (pure functions, no database)
  3. Request models and (de)serialisation
  4. Employees, attendance (punch-in / punch-out / list / regularize)
  5. Analytics (MongoDB aggregation pipelines)
  6. Explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does NOT override variables that are already set in the real environment

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Setup & indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back datetimes WITH tzinfo=UTC instead of naive ones, so we never
# have to guess what a naive datetime means.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes() -> None:
    """Idempotent: create_index with the same spec and name is a no-op if it already exists."""
    specs = [
        # employees
        ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp"}),
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined"}),
        ("employees", [("joined_on", ASCENDING)], {"name": "joined"}),
        # attendance_logs
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_emp_date"}),
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    for coll, keys, opts in specs:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError:
            # Do not crash the app (so /health can still answer), but make the failure loud:
            # a missing UNIQUE index would silently remove our race protection.
            log.exception("could not create index %s on %s", opts.get("name"), coll)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / regularize window
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6

EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 4_102_444_800_000


def as_utc(dt: datetime) -> datetime:
    """Naive datetimes (should not happen with tz_aware=True) are treated as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc_seconds(dt: datetime) -> datetime:
    """R1: drop sub-second part before any calculation."""
    return as_utc(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    # floor-divide: 09:40:00.900 -> 09:40:00 (the truncation required by R1)
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    # exact integer arithmetic (no float rounding)
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(s: str) -> Time:
    h, m = s.split(":")
    return Time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def round_half_up(value, places: int) -> Decimal:
    """Half-up rounding done on a Decimal built from the shortest repr, never on a binary float."""
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the attendance day (IST). Overnight shifts: an early-morning punch-in belongs to the previous day."""
    ist = trunc_seconds(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(emp) and ist.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day: Date, emp: dict) -> tuple[datetime, datetime]:
    """Absolute start and end instants of the shift that belongs to attendance day `day` (IST)."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(emp) else day
    end = datetime.combine(end_day, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((trunc_seconds(punch_in) - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    seconds = int((trunc_seconds(punch_out) - shift_end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: judged on the ROUNDED work_hours."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_LIMIT


def derive(emp: dict, day: str, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All four derived fields for a presence record, from its final times."""
    start, end = shift_bounds(Date.fromisoformat(day), emp)
    out = {
        "late_minutes": compute_late_minutes(punch_in, start),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


EMPTY_DERIVED = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

# --------------------------------------------------------------------------- #
# 3. Models and serialisation
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)]  # strict: no floats / strings
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            Date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a real calendar date")
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

    @model_validator(mode="after")
    def _no_explicit_null(self):
        # Omitted = unchanged (fine). An explicit null is not a valid value for these fields.
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


def unprocessable(msg: str, *loc: str) -> HTTPException:
    return HTTPException(422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def parse_date_param(value: str, name: str, where: str = "query") -> Date:
    if not DATE_RE.match(value):
        raise unprocessable("date must be YYYY-MM-DD", where, name)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable("not a real calendar date", where, name)


def month_bounds(month: str) -> tuple[str, str, Date, Date]:
    """-> (first-day string, last-day string, first Date, last Date). Strings compare correctly as ISO dates."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = Date(y, m, 1)
        last = Date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    except (ValueError, TypeError):
        raise unprocessable("invalid month", "query", "month")
    return first.isoformat(), last.isoformat(), first, last


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


PUNCH_FIELDS = ("punch_in", "punch_out")


def _history_out(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        conv = (lambda v: dt_to_ms(v) if isinstance(v, datetime) else v) if key in PUNCH_FIELDS else (lambda v: v)
        changes[key] = {"from": conv(change.get("from")), "to": conv(change.get("to"))}
    return {"at": dt_to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored document -> API shape. Fills defaults for legacy documents (missing half_day / history ...)."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pin) if pin else None,
        "punch_out": dt_to_ms(pout) if pout else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def safe_skip(page: int, page_size: int) -> Optional[int]:
    skip = (page - 1) * page_size
    return skip if skip < 2**31 else None  # absurd pages: answer empty instead of overflowing BSON ints


# --------------------------------------------------------------------------- #
# 4. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # No "find first": the UNIQUE index decides. Parallel duplicate creates -> exactly one succeeds.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    skip = safe_skip(page, page_size)
    items = []
    if skip is not None:
        items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": [employee_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, ts, None),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one of N identical requests in.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before ts. Not "by date": works across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc_seconds(rec["punch_in"])
    if ts <= pin:
        raise unprocessable("punched_at must be after punch_in", "body", "punched_at")
    if (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise unprocessable("punched_at is more than 24 hours after punch_in", "body", "punched_at")
    d = derive(emp, rec["date"], pin, ts)
    # Conditional update: only succeeds while punch_out is still empty. Of two simultaneous
    # punch-outs exactly one matches; the other gets None back and answers 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"],
                  "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date_param(date_from, "date_from") if date_from is not None else None
    d_to = parse_date_param(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must not be after date_to", "query", "date_from")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    # estimated count is metadata-only (instant) and exact when there is no filter
    total = db.attendance_logs.estimated_document_count() if not q else db.attendance_logs.count_documents(q)
    skip = safe_skip(page, page_size)
    items = []
    if skip is not None:
        # sort / skip / limit all run inside MongoDB and are served by an index
        items = list(db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip).limit(page_size))
    return {"items": [record_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


def _same(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return dt_to_ms(a) == dt_to_ms(b)
    return a == b


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """-> (new field values, changes). Raises 422 for every rule in the PATCH contract."""
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    status = body.status or old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("ABSENT/LEAVE clear the punch times; do not send punch_in / punch_out", "body", "status")
        new = {"status": status, "punch_in": None, "punch_out": None, **EMPTY_DERIVED}
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pin is None:
            raise unprocessable("a presence status requires a punch_in", "body", "punch_in")
        if body.punch_in is not None and attendance_date(pin, emp) != rec["date"]:
            raise unprocessable("punch_in must stay on the record's attendance date", "body", "punch_in")
        if pout is not None and (body.punch_in is not None or body.punch_out is not None):
            gap = (trunc_seconds(pout) - trunc_seconds(pin)).total_seconds()
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                raise unprocessable("punch_out must be after punch_in and within 24 hours", "body", "punch_out")
        new = {"status": status, "punch_in": pin, "punch_out": pout, **derive(emp, rec["date"], pin, pout)}

    changes = {k: {"from": old[k], "to": new[k]} for k in old if not _same(old[k], new[k])}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise unprocessable("request changes nothing", "body")
    return new, changes


def _regularize_guard(rec: dict) -> dict:
    """Filter that matches the record only if it is still exactly as we read it (optimistic concurrency)."""
    f = {"_id": rec["_id"], "status": rec["status"],
         "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    n = len(rec.get("history") or [])
    if n == 0:
        f["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        f["history"] = {"$size": n}
    return f


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date_param(date, "date", "path").isoformat()
    emp = get_employee_or_404(emp_code)
    for _ in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(emp, rec, body)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Write only if nobody changed the record since we read it; $push appends, never overwrites.
        updated = db.attendance_logs.find_one_and_update(
            _regularize_guard(rec), {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
        # lost a race: re-read and re-plan against the newer record
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 5. Analytics: MongoDB aggregation pipelines
# --------------------------------------------------------------------------- #
# $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
WEEKDAYS = [2, 3, 4, 5, 6]


def _is_weekday(date_expr):
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}, WEEKDAYS]}


def _is_half_day(half_expr):
    return {"$eq": [{"$ifNull": [half_expr, False]}, True]}


def _present_weekday_value(status, day, half):
    """R5-R7: 1 (or 0.5 for a half day) for a presence record on Mon-Fri, else 0."""
    return {"$cond": [{"$and": [{"$in": [status, PRESENT_STATUSES]}, _is_weekday(day)]},
                      {"$cond": [_is_half_day(half), 0.5, 1]}, 0]}


def _is_late(late_expr):
    return {"$gt": [{"$ifNull": [late_expr, 0]}, 0]}


def _count_if(cond):
    return {"$cond": [cond, 1, 0]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weekday_value("$status", "$date", "$half_day")},
            "leave_days": {"$sum": _count_if({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": _count_if(_is_late("$late_minutes"))},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def count_working_days(first: Date, last: Date, joined_on: Date) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last. Pure calendar arithmetic (no data involved)."""
    start = max(first, joined_on)
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    _, _, first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    working = count_working_days(first, last, Date.fromisoformat(emp["joined_on"]))
    present = agg.get("present_days", 0)
    pct = None if working == 0 else float(round_half_up(Decimal(str(present)) / Decimal(working) * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": float(round_half_up(present, 2)),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from `employees` (so people with zero logs still count) and $lookups their logs for the month."""
    start, end, _, _ = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9
    if department is not None:
        emp_match["department"] = department
    L = "$$l"
    is_present = {"$in": [f"{L}.status", PRESENT_STATUSES]}
    has_hours = {"$and": [is_present, {"$isNumber": f"{L}.work_hours"}]}

    def over_logs(expr):
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": over_logs(_present_weekday_value(f"{L}.status", f"{L}.date", f"{L}.half_day")),
            "late_count": over_logs(_count_if(_is_late(f"{L}.late_minutes"))),
            "total_late_minutes": over_logs({"$ifNull": [f"{L}.late_minutes", 0]}),
            "leave_count": over_logs(_count_if({"$eq": [f"{L}.status", "LEAVE"]})),
            "on_duty_count": over_logs(_count_if({"$eq": [f"{L}.status", "ON_DUTY"]})),
            # hours kept as integer hundredths so the sum is exact; divided and rounded half-up in Python
            "hours_cents": over_logs({"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": [f"{L}.work_hours", 100]}, 0]}}, 0]}),
            "hours_n": over_logs(_count_if(has_hours)),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_cents": {"$sum": "$hours_cents"},
            "hours_n": {"$sum": "$hours_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = db.employees.aggregate(department_summary_pipeline(month, department))
    items = []
    for r in rows:
        avg = None
        if r["hours_n"] > 0:
            avg = float(round_half_up(Decimal(r["hours_cents"]) / Decimal(r["hours_n"]) / Decimal(100), 2))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(round_half_up(r["present_days"], 2)),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end, _, _ = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})  # ranking happens inside the department
    pipe += [
        # $rank = standard competition ranking: ties share a rank, next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_pipeline(department: str, from_s: str, to_s: str, codes: list, n_days: int, from_dt: datetime) -> list:
    """
    One row per calendar day. It starts from a single employee document of the department (so the
    pipeline always has an input document), $lookups the per-day log totals and the join dates,
    generates the day list with $range, and lets $setWindowFields compute the 7-row moving average.
    Rates are kept as integers (10000ths) until the very end so half-up rounding is exact.
    """
    present2 = {"$cond": [{"$in": ["$status", PRESENT_STATUSES]}, {"$cond": [_is_half_day("$half_day"), 1, 2]}, 0]}
    has_rate = {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}
    window = {"documents": [-6, "current"]}
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "seed": {"$literal": 1}}},
        {"$lookup": {"from": "attendance_logs", "pipeline": [
            {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_s, "$lte": to_s}}},
            {"$group": {"_id": "$date", "present2": {"$sum": present2},
                        "late": {"$sum": _count_if(_is_late("$late_minutes"))}}},
        ], "as": "perday"}},
        {"$lookup": {"from": "employees", "pipeline": [
            {"$match": {"department": department, "joined_on": {"$lte": to_s}}},
            {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}},
        ], "as": "joins"}},
        {"$project": {"perday": 1, "joins": 1, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"d": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}},
                "in": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$d"}},
                       "wd": {"$in": [{"$dayOfWeek": "$$d"}, WEEKDAYS]}}}}}}}},
        {"$unwind": "$days"},
        {"$project": {
            "date": "$days.date",
            "is_working_day": "$days.wd",
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j._id", "$days.date"]}}},
                "as": "j", "in": "$$j.n"}}},
            "agg": {"$arrayElemAt": [{"$filter": {"input": "$perday", "as": "p",
                                                  "cond": {"$eq": ["$$p._id", "$days.date"]}}}, 0]},
        }},
        {"$project": {"date": 1, "is_working_day": 1, "headcount": 1,
                      "present2": {"$ifNull": ["$agg.present2", 0]}, "late_count": {"$ifNull": ["$agg.late", 0]}}},
        {"$addFields": {"rated": has_rate}},
        {"$addFields": {
            "has_rate": {"$cond": ["$rated", 1, 0]},
            # rate*10000 = present2*5000/headcount ; half-up -> floor((present2*10000 + headcount) / (2*headcount))
            "rate_scaled": {"$cond": ["$rated", {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present2", 10000]}, "$headcount"]},
                {"$multiply": ["$headcount", 2]}]}}, None]},
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "w_sum": {"$sum": "$rate_scaled", "window": window},
            "w_cnt": {"$sum": "$has_rate", "window": window}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present2", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": ["$rated", {"$divide": ["$rate_scaled", 10000]}, None]},
            # mean of the (rounded) rates in the window, half-up: floor((2*sum + n) / (2*n)) / 10000
            "moving_avg_7d": {"$cond": [{"$gt": ["$w_cnt", 0]}, {"$divide": [{"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$w_sum", 2]}, "$w_cnt"]}, {"$multiply": ["$w_cnt", 2]}]}}, 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


def _trend_args(department: str, from_: str, to: str):
    d_from = parse_date_param(from_, "from")
    d_to = parse_date_param(to, "to")
    if d_to < d_from:
        raise unprocessable("to must not be before from", "query", "to")
    n_days = (d_to - d_from).days + 1
    if n_days > 92:
        raise unprocessable("range exceeds 92 days", "query", "to")
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    return d_from.isoformat(), d_to.isoformat(), n_days, from_dt


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    from_s, to_s, n_days, from_dt = _trend_args(department, from_, to)
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(trend_pipeline(department, from_s, to_s, codes, n_days, from_dt)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain
# --------------------------------------------------------------------------- #
def _require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, detail=[{"loc": ["query", k], "msg": "Field required", "type": "missing"} for k in missing])


def _run_explain(inner: dict) -> dict:
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))  # BSON -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
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
    # Each branch calls the SAME builder function the real endpoint uses.
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        inner = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
                 "skip": safe_skip(page, page_size) or 0, "limit": page_size, "projection": {"_id": 0}}
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        inner = {"aggregate": "attendance_logs", "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
        coll = "attendance_logs"
    elif endpoint == "department_summary":
        _require(month=month)
        inner = {"aggregate": "employees", "pipeline": department_summary_pipeline(month, department), "cursor": {}}
        coll = "employees"
    elif endpoint == "late_leaderboard":
        _require(month=month)
        inner = {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}
        coll = "attendance_logs"
    else:  # department_trend
        _require(department=department, **{"from": from_}, to=to)
        from_s, to_s, n_days, from_dt = _trend_args(department, from_, to)
        codes = db.employees.distinct("emp_code", {"department": department})
        inner = {"aggregate": "employees", "cursor": {},
                 "pipeline": trend_pipeline(department, from_s, to_s, codes, n_days, from_dt)}
        coll = "employees"
    return {"endpoint": endpoint, "collection": coll, "explain": _run_explain(inner)}
