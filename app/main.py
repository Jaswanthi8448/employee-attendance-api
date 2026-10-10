"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout (all code lives in this one file by assignment rule):
  1. Configuration, database, indexes
  2. Time and business-rule helpers (R1 - R5, R8)
  3. Request models and validation
  4. Serialization (BSON <-> API)
  5. Query and pipeline builders, shared by the endpoints and /admin/explain
  6. Endpoints
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority over .env

logger = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # BSON dates come back as aware UTC datetimes, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees_col = db.employees
logs_col = db.attendance_logs

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000  # anything smaller looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.5  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE_STATUSES = ["ABSENT", "LEAVE"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    employees_col.create_index([("emp_code", ASCENDING)], unique=True)  # identity, race-safe create
    employees_col.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])  # list/filter by dept
    employees_col.create_index([("joined_on", ASCENDING)])  # department summary without a dept filter
    # One record per (emp_code, date): race-safe punch-in, PATCH lookup, per-employee month/range queries.
    logs_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Date-range scans across all employees (attendance list sort, leaderboard, summary).
    logs_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    logger.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def validation_error(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's default body shape, for checks that span several parameters."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 2. Time and business-rule helpers
# --------------------------------------------------------------------------- #
def ms_to_datetime(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(UTC))


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: BSON dates are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST date of the punch-in; early-morning punches of an overnight shift belong to the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_at(day: date, emp: dict) -> datetime:
    return datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)


def shift_end_at(day: date, emp: dict) -> datetime:
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; minutes counted from shift start."""
    seconds = (truncate_seconds(punch_in) - shift_start_at(day, emp)).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    minutes = int((truncate_seconds(punch_out) - shift_end_at(day, emp)).total_seconds() // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours rounded to 2 decimals half-up (Decimal, not float round())."""
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return float(round_half_up(Decimal(seconds) / 3600, 2))


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   day: date, emp: dict) -> dict:
    """The four API-computed fields for a record in its final state (DATA_MODEL section 3)."""
    if status in ABSENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, emp),
        "half_day": work_hours < HALF_DAY_HOURS,  # R5 uses the rounded value
    }


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] inclusive (R7)."""
    if end < start:
        return 0
    full_weeks, remainder = divmod((end - start).days + 1, 7)
    extra = sum(1 for i in range(remainder) if (start.weekday() + i) % 7 < 5)
    return full_weeks * 5 + extra


def as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value or 0))


def rounded(value: Any, places: int) -> float:
    return float(round_half_up(as_decimal(value), places))


# --------------------------------------------------------------------------- #
# 3. Request models and validation
# --------------------------------------------------------------------------- #
def _valid_calendar_date(value: str) -> str:
    date.fromisoformat(value)  # rejects 2026-02-30 and friends with a ValueError -> 422
    return value


def _valid_month(value: str) -> str:
    date(int(value[:4]), int(value[5:]), 1)  # rejects year 0000
    return value


EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
IsoDate = Annotated[str, StringConstraints(pattern=DATE_PATTERN), AfterValidator(_valid_calendar_date)]
MonthStr = Annotated[str, StringConstraints(pattern=MONTH_PATTERN), AfterValidator(_valid_month)]
HhMm = Annotated[str, StringConstraints(pattern=HHMM_PATTERN)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HhMm = "09:30"
    shift_end: HhMm = "18:30"
    joined_on: IsoDate

    @model_validator(mode="after")
    def _distinct_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _check_combination(self) -> "RegularizeIn":
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may be omitted but not null")
        if self.status in ABSENCE_STATUSES and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("punch times cannot be supplied with status ABSENT or LEAVE")
        return self


def check_date_order(start: Optional[str], end: Optional[str], loc: tuple) -> None:
    if start and end and start > end:
        raise validation_error(loc, "start date must not be after end date")


def require_params(**params: Any) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError([
            {"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing
        ])


# --------------------------------------------------------------------------- #
# 4. Serialization
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def to_api(value: Any) -> Any:
    """Recursively turn BSON datetimes into epoch ms (history entries nest them inside `changes`)."""
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {key: to_api(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_api(item) for item in value]
    return value


def serialize_employee(doc: dict) -> dict:
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out


def serialize_record(doc: dict) -> dict:
    """Fills the documented defaults for legacy records (missing history / half_day etc.)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": to_api(doc.get("history") or []),
    }


def record_state(doc: dict) -> dict:
    """The fields a correction may change, with documented defaults for missing values."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
    }


# --------------------------------------------------------------------------- #
# 5. Query and pipeline builders (shared by endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}  # R5, R6
WEEKDAY_PRESENT_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}  # R7
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding inside MongoDB ($round is half-to-even). Inputs are non-negative Decimal128."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """Starts from employees so that people with zero logs still count towards headcount (R9)."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    has_work_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    per_employee_stats = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "work_hours_sum": {"$sum": {"$cond": [has_work_hours, {"$toDecimal": "$work_hours"}, 0]}},
            "work_hours_count": count_if(has_work_hours),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        }},
    ]
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee_stats, "as": "stats"}},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
            "work_hours_count": {"$sum": "$stats.work_hours_count"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    """Standard competition ranking via $rank; limit is applied to the rank, not the row count."""
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(emp_codes: list[str], joined_dates: list[str], start: date, end: date) -> list:
    """One row per calendar day: group the logs by day, gap-fill with $densify, then a 7-row window."""
    start_dt = datetime.combine(start, time(), UTC)
    end_dt = datetime.combine(end, time(), UTC)
    headcount = {"$size": {"$filter": {"input": {"$literal": joined_dates}, "as": "joined",
                                       "cond": {"$lte": ["$$joined", "$date"]}}}}
    rate = {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                      half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": count_if(IS_LATE)}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Anchor both ends so $densify covers the whole range even when there are no logs at all.
        {"$unionWith": {"pipeline": [{"$documents": [{"day": start_dt}, {"day": end_dt}]}]}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                        "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]}}},
        {"$addFields": {"headcount": headcount}},
        {"$addFields": {"attendance_rate": rate}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg", None]}, None,
                                                  half_up_expr("$moving_avg", 4)]}}},
    ]


def find_employee_or_404(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def department_members_or_404(department: str) -> tuple[list[str], list[str]]:
    members = list(employees_col.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not members:
        raise HTTPException(404, "department not found")
    return [m["emp_code"] for m in members], [m["joined_on"] for m in members]


def validate_trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise validation_error(("query", "to"), "to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise validation_error(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created_at = datetime.now(UTC)
    doc = {**body.model_dump(), "created_at": created_at.replace(microsecond=created_at.microsecond // 1000 * 1000)}
    try:
        employees_col.insert_one(doc)  # the unique index decides races, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    total = employees_col.count_documents(query)
    cursor = employees_col.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee_or_404(body.emp_code)
    ts = ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs_col.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee_or_404(body.emp_code)
    ts = ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    record = logs_col.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = truncate_seconds(record["punch_in"])
    if ts <= started or ts - started > MAX_SHIFT:
        raise validation_error(("body", "punched_at"), "punched_at must be after punch_in and within 24 hours")
    update = {"punch_out": ts, **derived_fields(record["status"], started, ts, date.fromisoformat(record["date"]), emp)}
    updated = logs_col.find_one_and_update(
        # Matching punch_out=None makes the close atomic; matching punch_in guards against a concurrent correction.
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[IsoDate], Query()] = None,
    date_to: Annotated[Optional[IsoDate], Query()] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    check_date_order(date_from, date_to, ("query", "date_from"))
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = logs_col.count_documents(query) if query else logs_col.estimated_document_count()
    cursor = logs_col.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def corrected_state(record: dict, body: RegularizeIn, emp: dict, day: date) -> dict:
    """Apply a correction to a record and return its full final state, or raise 422."""
    status = body.status or record["status"]
    if status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(("body",), "punch times cannot be set on an ABSENT or LEAVE record")
        return {"status": status, "punch_in": None, "punch_out": None, **derived_fields(status, None, None, day, emp)}

    punch_in_at = ms_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    if punch_in_at is None:
        raise validation_error(("body", "punch_in"), "a presence status requires a punch_in")
    if body.punch_in is not None and attendance_date(punch_in_at, emp) != day:
        raise validation_error(("body", "punch_in"), "punch_in must fall on the record's attendance date")
    punch_out_at = ms_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")
    if punch_out_at is not None and not (punch_in_at < punch_out_at <= punch_in_at + MAX_SHIFT):
        raise validation_error(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
    return {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at,
            **derived_fields(status, punch_in_at, punch_out_at, day, emp)}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: IsoDate, body: RegularizeIn):  # noqa: A002 - contract name
    emp = find_employee_or_404(emp_code)
    record = logs_col.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    before = record_state(record)
    after = corrected_state(record, body, emp, datetime.strptime(date, "%Y-%m-%d").date())
    changes = {key: {"from": before[key], "to": after[key]} for key in after if before[key] != after[key]}
    if not changes:
        raise validation_error(("body",), "the request changes nothing")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: only apply if nobody changed the record (or its history) since we read it.
    unchanged = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
    }
    updated = logs_col.find_one_and_update(
        unchanged, {"$set": after, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):
    emp = find_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(logs_col.aggregate(monthly_pipeline(emp_code, first, last)))
    stats = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)
    present_days = as_decimal(stats.get("present_days"))
    pct = None if working_days == 0 else rounded(present_days * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": rounded(present_days, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in employees_col.aggregate(department_summary_pipeline(first, last, department)):
        count = int(row["work_hours_count"])
        avg = None if count == 0 else rounded(as_decimal(row["work_hours_sum"]) / count, 2)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": rounded(row["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[MonthStr, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = logs_col.aggregate(leaderboard_pipeline(first, last, limit, department))
    items = [{"rank": int(row["rank"]), "emp_code": row["emp_code"], "name": row["name"],
              "department": row["department"], "total_late_minutes": int(row["total_late_minutes"]),
              "late_count": int(row["late_count"])} for row in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[IsoDate, Query(alias="from")],
    to: Annotated[IsoDate, Query()],
):
    start, end = validate_trend_range(from_, to)
    emp_codes, joined_dates = department_members_or_404(department)
    items = [
        {
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": int(row["headcount"]),
            "present_count": rounded(row["present_count"], 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": None if row["attendance_rate"] is None else rounded(row["attendance_rate"], 4),
            "moving_avg_7d": None if row["moving_avg_7d"] is None else rounded(row["moving_avg_7d"], 4),
        }
        for row in logs_col.aggregate(trend_pipeline(emp_codes, joined_dates, start, end))
    ]
    return {"department": department, "items": items}


ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def explain_command(command: dict) -> dict:
    raw = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(raw))  # BSON (ObjectId, Timestamp, ...) -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[MonthStr], Query()] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[Optional[IsoDate], Query()] = None,
    date_to: Annotated[Optional[IsoDate], Query()] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[IsoDate], Query(alias="from")] = None,
    to: Annotated[Optional[IsoDate], Query()] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to, ("query", "date_from"))
        collection = "attendance_logs"
        command = {"find": collection, "filter": attendance_filter(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        require_params(emp_code=emp_code, month=month)
        find_employee_or_404(emp_code)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": monthly_pipeline(emp_code, *month_bounds(month)), "cursor": {}}
    elif endpoint == "department_summary":
        require_params(month=month)
        collection = "employees"
        pipeline = department_summary_pipeline(*month_bounds(month), department)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        require_params(month=month)
        collection = "attendance_logs"
        pipeline = leaderboard_pipeline(*month_bounds(month), limit, department)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        require_params(department=department, **{"from": from_, "to": to})
        start, end = validate_trend_range(from_, to)
        emp_codes, joined_dates = department_members_or_404(department)
        collection = "attendance_logs"
        pipeline = trend_pipeline(emp_codes, joined_dates, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": explain_command(command)}
