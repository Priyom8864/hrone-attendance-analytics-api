import os
import re
from calendar import monthrange
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
DAY_SECONDS = 24 * 3600
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


# --- time helpers -----------------------------------------------------------

def utc(dt):
    # pymongo returns naive datetimes that are really UTC
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def now():
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=UTC).replace(microsecond=0)


def to_ms(dt):
    return None if dt is None else int(utc(dt).timestamp() * 1000)


def half_up(value, places=2):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_date(value, name):
    try:
        parsed = Date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    # fromisoformat also takes things like 20240101, we don't want those
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    return parsed


def parse_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    return Date(year, mon, 1), Date(year, mon, monthrange(year, mon)[1])


def working_days(start, end, joined_on):
    first = max(start, Date.fromisoformat(joined_on))
    if first > end:
        return 0
    return sum(1 for i in range((end - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


# --- shift rules ------------------------------------------------------------

def shift_time(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST).astimezone(UTC)


def shift_day(punch_in, start, end):
    local = utc(punch_in).astimezone(IST)
    day = local.date()
    # overnight shift: a punch after midnight belongs to the day the shift started
    if end <= start and local.strftime("%H:%M") < end:
        day -= timedelta(days=1)
    return day


def late_minutes(punch_in, start, day):
    secs = int((utc(punch_in.replace(microsecond=0)) - shift_time(day, start)).total_seconds())
    # 10 minute grace, but once you're past it the lateness counts from shift start
    return secs // 60 if secs > 600 else 0


def work_hours(punch_in, punch_out):
    secs = int((utc(punch_out.replace(microsecond=0)) - utc(punch_in.replace(microsecond=0))).total_seconds())
    return half_up(Decimal(secs) / 3600)


def is_half_day(hours):
    return hours is not None and hours < 4.5


def overtime_minutes(punch_out, start, end, day):
    end_day = day + timedelta(days=1) if end <= start else day
    mins = int((utc(punch_out.replace(microsecond=0)) - shift_time(end_day, end)).total_seconds()) // 60
    return mins if mins >= 30 else 0


def check_gap(punch_in, punch_out, msg):
    gap = (utc(punch_out) - utc(punch_in)).total_seconds()
    if gap <= 0 or gap > DAY_SECONDS:
        raise HTTPException(422, msg)


# --- serializers ------------------------------------------------------------

def serialize_employee(doc):
    doc = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in doc:
        doc["created_at"] = to_ms(doc["created_at"])
    return doc


def serialize_attendance(doc):
    doc = {k: v for k, v in doc.items() if k not in ("_id", "id")}
    doc["punch_in"] = to_ms(doc.get("punch_in"))
    doc["punch_out"] = to_ms(doc.get("punch_out"))
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("work_hours", None)

    history = []
    for entry in doc.get("history", []):
        entry = dict(entry, at=to_ms(entry.get("at")))
        changes = {}
        for field, pair in (entry.get("changes") or {}).items():
            pair = dict(pair)
            if field in ("punch_in", "punch_out"):
                pair["from"], pair["to"] = to_ms(pair.get("from")), to_ms(pair.get("to"))
            changes[field] = pair
        entry["changes"] = changes
        history.append(entry)
    doc["history"] = history
    return doc


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_date_desc")


# --- request models ---------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v):
        try:
            Date.fromisoformat(v)
        except (TypeError, ValueError):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if len(v) != 10:
            raise ValueError("joined_on must be in YYYY-MM-DD format")
        return v

    @field_validator("name", "department")
    @classmethod
    def strip_text(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("value must not be blank")
        return v

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


def check_epoch_ms(v):
    if v is None:
        return v
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("must be an integer epoch-millisecond value")
    if not 100_000_000_000 <= v <= 4_102_444_800_000:
        raise ValueError("epoch milliseconds are outside the allowed range")
    return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        return check_epoch_ms(v)


class PunchInIn(PunchOutIn):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ts(cls, v):
        return check_epoch_ms(v)


# --- employees --------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    rows = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_employee(r) for r in rows],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


# --- attendance -------------------------------------------------------------

def find_employee(emp_code):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now()
    day = shift_day(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now()

    # may be after midnight, so grab the latest open record rather than today's
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$ne": None}},
        sort=[("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")

    check_gap(record["punch_in"], ts, "punched_at must be after punch_in and within 24 hours")
    day = Date.fromisoformat(record["date"])
    hours = work_hours(record["punch_in"], ts)

    # punch_out: None in the filter means only one concurrent request can win
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": utc(ts),
            "work_hours": hours,
            "overtime_minutes": overtime_minutes(ts, emp["shift_start"], emp["shift_end"], day),
            "half_day": is_half_day(hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


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
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    rows = (db.attendance_logs.find(query, {"_id": 0})
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [serialize_attendance(r) for r in rows],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    emp = find_employee(emp_code)
    start, end = emp["shift_start"], emp["shift_end"]

    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    sent = body.model_dump(exclude_unset=True)
    status = sent.get("status", current.get("status", "PRESENT"))
    absent = status in ("ABSENT", "LEAVE")

    if absent and ("punch_in" in sent or "punch_out" in sent):
        raise HTTPException(422, "ABSENT or LEAVE cannot be supplied with punch times")

    new_in = from_ms(sent["punch_in"]) if sent.get("punch_in") is not None else current.get("punch_in")
    new_out = from_ms(sent["punch_out"]) if sent.get("punch_out") is not None else current.get("punch_out")

    if absent:
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if new_in is not None and shift_day(new_in, start, end).isoformat() != date:
        raise HTTPException(422, "punch_in does not belong to the attendance date")
    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        check_gap(new_in, new_out, "punch_out must be after punch_in and within 24 hours")

    closed = new_in is not None and new_out is not None
    hours = work_hours(new_in, new_out) if closed else None
    new = {
        "status": status,
        "punch_in": new_in and utc(new_in),
        "punch_out": new_out and utc(new_out),
        "late_minutes": late_minutes(new_in, start, day) if new_in else 0,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(new_out, start, end, day) if closed else 0,
        "half_day": is_half_day(hours),
    }
    old = {
        "status": current.get("status"),
        "punch_in": current.get("punch_in") and utc(current["punch_in"]),
        "punch_out": current.get("punch_out") and utc(current["punch_out"]),
        "late_minutes": current.get("late_minutes", 0),
        "work_hours": current.get("work_hours"),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }

    changes = {}
    for field, value in new.items():
        if old[field] == value:
            continue
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(old[field]), "to": to_ms(value)}
        else:
            changes[field] = {"from": old[field], "to": value}
    if not changes:
        raise HTTPException(422, "request does not change the record")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # matching on the old history makes a parallel edit miss instead of
    # clobbering the other person's audit entry
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "history": current.get("history", [])},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed concurrently; reload and retry")
    return serialize_attendance(updated)


# --- analytics --------------------------------------------------------------

def present_credit(status, half_day, date):
    """1 for a weekday presence, 0.5 for a half day, 0 otherwise (Mongo expr)."""
    weekday = {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date}}}, 6]}
    return {"$cond": [
        {"$and": [{"$in": [status, PRESENCE]}, weekday]},
        {"$cond": [{"$eq": [half_day, True]}, 0.5, 1.0]},
        0.0,
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_weekdays": {"$sum": present_credit("$status", "$half_day", "$date")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    row = next(db.attendance_logs.aggregate(pipeline), {})

    days = working_days(start, end, emp["joined_on"])
    present = half_up(row.get("present_weekdays", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / days * 100) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    emp_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        emp_match["department"] = department

    # only count hours for people who were present and actually punched out
    has_hours = {"$and": [
        {"$in": ["$logs.status", PRESENCE]},
        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
    ]}

    pipeline = [
        {"$match": emp_match},
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
            "present_days": {"$sum": present_credit(
                "$logs.status", "$logs.half_day", {"$ifNull": ["$logs.date", "1970-01-01"]})},
            "work_hours_sum": {"$sum": {"$cond": [has_hours, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
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
        item["present_days"] = half_up(item.get("present_days", 0))
        item["avg_work_hours"] = half_up(item.get("avg_work_hours"))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
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
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.min)

    # $documents seeds one row so $densify can still fill the whole range
    # even when there are no attendance logs at all
    pipeline = [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_str": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_str"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
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
                    {"$in": ["$$log.status", PRESENCE]},
                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                    0.0,
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "log", "cond": {"$gt": ["$$log.late_minutes", 0]},
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
        # not rounded yet on purpose, the moving average should use the raw rate
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]

    # $documents is a database-level stage, so db.aggregate, not a collection's
    items = list(db.aggregate(pipeline))
    for item in items:
        item["attendance_rate"] = half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = half_up(item.get("moving_avg_7d"), 4)
        item["present_count"] = half_up(item.get("present_count", 0))
    return {"department": department, "from": from_date, "to": to_date, "items": items}


# --- admin ------------------------------------------------------------------

def explain(pipeline):
    cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
    return db.command({"explain": cmd, "verbosity": "executionStats"})


def month_filter(month):
    if not month:
        raise HTTPException(422, "month is required")
    start, end = parse_month(month)
    return {"$gte": start.isoformat(), "$lte": end.isoformat()}


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
            parse_date(from_date, "from")
            match.setdefault("date", {})["$gte"] = from_date
        if to_date:
            parse_date(to_date, "to")
            match.setdefault("date", {})["$lte"] = to_date
        return explain([
            {"$match": match},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ])

    if endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(422, "month and emp_code are required")
        return explain([
            {"$match": {"emp_code": emp_code, "date": month_filter(month)}},
            {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}},
        ])

    if endpoint == "department_summary":
        return explain([
            {"$match": {"date": month_filter(month)}},
            {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}},
        ])

    if endpoint == "late_leaderboard":
        return explain([
            {"$match": {"date": month_filter(month), "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ])

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid date range")
        return explain([
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$group": {"_id": "$date", "records": {"$sum": 1}}},
        ])

    raise HTTPException(422, "unsupported endpoint name")
