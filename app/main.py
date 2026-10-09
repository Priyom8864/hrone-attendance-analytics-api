import os
import re
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from calendar import monthrange

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Time, rounding, validation and serialization helpers
# ---------------------------------------------------------------------------
def truncate_seconds(value: datetime) -> datetime:
    """Drop fractional seconds before storing or doing attendance calculations."""
    return value.replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    # Pydantic validates the input type/range before this helper is called.
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        # PyMongo commonly returns naive datetimes representing UTC.
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_utc(value: datetime) -> datetime:
    """Treat naive PyMongo datetimes as UTC; normalize aware values to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def shift_instant(day: Date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    # An overnight shift's early-morning punches belong to the prior shift day.
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, attendance_day: Date) -> int:
    instant = as_utc(truncate_seconds(punch_in))
    start = shift_instant(attendance_day, shift_start)
    elapsed_seconds = int((instant - start).total_seconds())
    # Grace is strictly more than 10 minutes; once exceeded, count from shift start.
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(truncate_seconds(punch_out)) - as_utc(truncate_seconds(punch_in))).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def is_half_day(work_hours: Optional[float]) -> bool:
    """Half-day is based on rounded work hours and is strictly below 4.50."""
    return work_hours is not None and Decimal(str(work_hours)) < Decimal("4.50")


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: Date) -> int:
    out_utc = as_utc(truncate_seconds(punch_out))
    end_day = attendance_day
    # Shift end is on the next day for overnight shifts.
    # The caller passes the attendance day, which is the shift-start day.
    # A shift ending at or before its start is overnight.
    # Shift start is not needed here: the caller supplies the employee's shift_start below.
    end = shift_instant(end_day, shift_end)
    elapsed = int((out_utc - end).total_seconds()) // 60
    return elapsed if elapsed >= 30 else 0


def compute_overtime_for_employee(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: Date) -> int:
    end_day = attendance_day + timedelta(days=1) if shift_end <= shift_start else attendance_day
    end = shift_instant(end_day, shift_end)
    elapsed = int((as_utc(truncate_seconds(punch_out)) - end).total_seconds()) // 60
    return elapsed if elapsed >= 30 else 0


def serialize_history(history):
    result = []
    for entry in history or []:
        item = dict(entry)
        item["at"] = datetime_to_epoch_ms(item.get("at"))
        changes = item.get("changes") or {}
        for field, pair in changes.items():
            pair = dict(pair)
            if field in {"punch_in", "punch_out"}:
                pair["from"] = datetime_to_epoch_ms(pair.get("from"))
                pair["to"] = datetime_to_epoch_ms(pair.get("to"))
            changes[field] = pair
        item["changes"] = changes
        result.append(item)
    return result


def serialize_employee(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    if "created_at" in doc:
        doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


def serialize_attendance(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)
    doc["punch_in"] = datetime_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = datetime_to_epoch_ms(doc.get("punch_out"))
    doc["history"] = serialize_history(doc.get("history", []))
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("work_hours", None)
    return doc


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_date_desc")


@app.on_event("startup")
def startup():
    create_indexes()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            Date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if len(value) != 10:
            raise ValueError("joined_on must be in YYYY-MM-DD format")
        return value

    @field_validator("name", "department")
    @classmethod
    def nonblank_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


def validate_epoch_ms(value):
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch-millisecond value")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise ValueError("epoch milliseconds are outside the allowed range")
    return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        return validate_epoch_ms(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        return validate_epoch_ms(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_time(cls, value):
        return validate_epoch_ms(value)


# ---------------------------------------------------------------------------
# Existing endpoints, corrected as part of the first implementation pass
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    items = [serialize_employee(doc) for doc in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    day_str = day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": day_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            Date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from must be YYYY-MM-DD")
    if date_to:
        try:
            Date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    items = [serialize_attendance(doc) for doc in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    # A punch-out may happen after midnight; locate the latest still-open attendance
    # record for this employee and validate it against that record's shift day.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$ne": None}},
        sort=[("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_at = as_utc(record["punch_in"])
    elapsed = int((as_utc(ts) - punch_in_at).total_seconds())
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    day = Date.fromisoformat(record["date"])
    work_hours = compute_work_hours(punch_in_at, ts)
    overtime = compute_overtime_for_employee(ts, emp["shift_start"], emp["shift_end"], day)
    half_day = is_half_day(work_hours)

    # Atomic compare-and-set: exactly one concurrent request can fill punch_out.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": as_utc(ts),
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    date_str = date
    try:
        target_day = Date.fromisoformat(date_str)
        if target_day.isoformat() != date_str:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    requested = body.model_dump(exclude_unset=True)
    status = requested.get("status", current.get("status", "PRESENT"))
    if status in {"ABSENT", "LEAVE"} and ("punch_in" in requested or "punch_out" in requested):
        raise HTTPException(status_code=422, detail="ABSENT or LEAVE cannot be supplied with punch times")

    new_in = epoch_ms_to_datetime(requested["punch_in"]) if requested.get("punch_in") is not None else current.get("punch_in")
    new_out = epoch_ms_to_datetime(requested["punch_out"]) if requested.get("punch_out") is not None else current.get("punch_out")

    if status in {"ABSENT", "LEAVE"}:
        new_in, new_out = None, None
    elif new_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if new_in is not None and attendance_date_for(
        new_in, emp["shift_start"], emp["shift_end"]
    ).isoformat() != date_str:
        # For overnight shifts, an early-morning IST punch belongs to the
        # previous calendar date (the shift-start date), not its local date.
        raise HTTPException(status_code=422, detail="punch_in does not belong to the attendance date")
    if new_out is not None:
        if new_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (as_utc(new_out) - as_utc(new_in)).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    new_late = compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"], target_day) if new_in else 0
    new_work = compute_work_hours(new_in, new_out) if new_in and new_out else None
    new_overtime = compute_overtime_for_employee(new_out, emp["shift_start"], emp["shift_end"], target_day) if new_in and new_out else 0
    new_half_day = is_half_day(new_work)

    final_values = {
        "status": status,
        "punch_in": as_utc(new_in) if new_in else None,
        "punch_out": as_utc(new_out) if new_out else None,
        "late_minutes": new_late,
        "work_hours": new_work,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    old_values = {
        "status": current.get("status"),
        "punch_in": as_utc(current["punch_in"]) if current.get("punch_in") else None,
        "punch_out": as_utc(current["punch_out"]) if current.get("punch_out") else None,
        "late_minutes": current.get("late_minutes", 0),
        "work_hours": current.get("work_hours"),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }

    changes = {}
    for field, new_value in final_values.items():
        old_value = old_values.get(field)
        if field in {"punch_in", "punch_out"}:
            if old_value != new_value:
                changes[field] = {
                    "from": datetime_to_epoch_ms(old_value),
                    "to": datetime_to_epoch_ms(new_value),
                }
        elif old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="request does not change the record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Include the previous history length in the predicate so concurrent edits do
    # not overwrite one another's audit entries.
    history = current.get("history", [])
    update_filter = {"_id": current["_id"], "history": history}
    update_doc = {"$set": final_values, "$push": {"history": history_entry}}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; reload and retry")
    return serialize_attendance(updated)



def parse_month(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    start = Date(year, mon, 1)
    end = Date(year, mon, monthrange(year, mon)[1])
    return start, end


def month_working_days(start: Date, end: Date, joined_on: str) -> int:
    first = max(start, Date.fromisoformat(joined_on))
    if first > end:
        return 0
    return sum(1 for offset in range((end - first).days + 1)
               if (first + timedelta(days=offset)).weekday() < 5)


def half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_weekdays": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0.0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    row = rows[0] if rows else {}
    working_days = month_working_days(start, end, emp["joined_on"])
    present_days = half_up(row.get("present_weekdays", 0), 2)
    attendance_pct = half_up(present_days / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    last_day = end.isoformat()
    employee_match = {"joined_on": {"$lte": last_day}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start.isoformat()]},
                    {"$lte": ["$date", end.isoformat()]},
                ]}}},
                {"$project": {
                    "status": 1, "date": 1, "work_hours": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, 6]},
                ]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]}, 0.0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]}, "$logs.work_hours", 0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]}, 1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount_codes"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = half_up(item.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}




def parse_iso_date(value: str, field_name: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return parsed


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_iso_date(from_date, "from")
    end = parse_iso_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.min)
    # Seed the pipeline with the first day so $densify can emit the complete range,
    # including when attendance_logs has no matching documents at all.
    pipeline = [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
        }},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_str": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_str"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$project": {
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {
                "input": "$logs",
                "as": "log",
                "in": {"$cond": [
                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                    0.0,
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs",
                "as": "log",
                "cond": {"$gt": ["$$log.late_minutes", 0]},
            }}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_str": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day_str"]},
                ]}}},
                {"$count": "headcount"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}}},
        {"$set": {
            # Keep the full precision through the moving-average calculation.
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]
    # $documents is a database-level aggregation stage, so use db.aggregate rather
    # than collection.aggregate. Decimal half-up rounding applies only to output values.
    items = list(db.aggregate(pipeline))
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = half_up(item["moving_avg_7d"], 4)
        item["present_count"] = half_up(item.get("present_count", 0), 2)
    return {"department": department, "from": from_date, "to": to_date, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    department: Optional[str] = None,
    emp_code: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        match = {}
        if emp_code:
            match["emp_code"] = emp_code
        if from_date:
            parse_iso_date(from_date, "from")
            match.setdefault("date", {})["$gte"] = from_date
        if to_date:
            parse_iso_date(to_date, "to")
            match.setdefault("date", {})["$lte"] = to_date
        aggregate_command = {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": match},
                {"$sort": {"date": -1, "emp_code": 1}},
                {"$skip": (page - 1) * page_size},
                {"$limit": page_size},
            ],
            "cursor": {},
        }
        return db.command({"explain": aggregate_command, "verbosity": "executionStats"})

    if endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(status_code=422, detail="month and emp_code are required")
        start, end = parse_month(month)
        match = {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        aggregate_command = {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": match}, {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}],
            "cursor": {},
        }
        return db.command({"explain": aggregate_command, "verbosity": "executionStats"})

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        match = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        aggregate_command = {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": match}, {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}],
            "cursor": {},
        }
        return db.command({"explain": aggregate_command, "verbosity": "executionStats"})

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        match = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}
        aggregate_command = {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": match}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}],
            "cursor": {},
        }
        return db.command({"explain": aggregate_command, "verbosity": "executionStats"})

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start = parse_iso_date(from_date, "from")
        end = parse_iso_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid date range")
        match = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        aggregate_command = {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": match}, {"$group": {"_id": "$date", "records": {"$sum": 1}}}],
            "cursor": {},
        }
        return db.command({"explain": aggregate_command, "verbosity": "executionStats"})

    raise HTTPException(status_code=422, detail="unsupported endpoint name")
