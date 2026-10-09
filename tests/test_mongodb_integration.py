"""Opt-in integration tests against a disposable MongoDB database.

Run with MONGO_TEST_URI pointing to a reachable MongoDB instance:
    MONGO_TEST_URI='mongodb://localhost:27017' pytest -q tests/test_mongodb_integration.py

The suite creates and drops only a uniquely named database ending in `_test`.
It deliberately skips when the real PyMongo package or MONGO_TEST_URI is absent.
"""
import importlib.util
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

# conftest.py installs a tiny import-only stub when PyMongo is absent. Never
# mistake that stub for the real driver or count these as integration tests.
try:
    pymongo_spec = importlib.util.find_spec("pymongo")
except (ImportError, ValueError):
    pymongo_spec = None
if pymongo_spec is None or getattr(sys.modules.get("pymongo"), "__spec__", pymongo_spec) is None:
    pytest.skip("real PyMongo is not installed", allow_module_level=True)

MONGO_TEST_URI = os.getenv("MONGO_TEST_URI")
if not MONGO_TEST_URI:
    pytest.skip("set MONGO_TEST_URI to run MongoDB integration tests", allow_module_level=True)

from pymongo import MongoClient
from fastapi.testclient import TestClient
import app.main as api


@pytest.fixture()
def live_api():
    mongo_client = MongoClient(MONGO_TEST_URI, serverSelectionTimeoutMS=3000)
    # Fail clearly rather than running a false-positive suite against a stub or
    # silently proceeding without a reachable database.
    mongo_client.admin.command("ping")
    db_name = "hrone_test_" + uuid.uuid4().hex[:10]
    test_db = mongo_client[db_name]
    old_db, old_client = api.db, api.client
    api.db, api.client = test_db, mongo_client
    try:
        with TestClient(api.app) as client:
            yield client, test_db
    finally:
        mongo_client.drop_database(db_name)
        api.db, api.client = old_db, old_client
        mongo_client.close()


def epoch_ms(local_iso):
    # Test instants are provided as ISO timestamps with explicit UTC offset.
    return int(datetime.fromisoformat(local_iso).astimezone(timezone.utc).timestamp() * 1000)


def employee_payload(emp_code="EMP81234"):
    return {
        "emp_code": emp_code,
        "name": " Integration User ",
        "email": f"{emp_code.lower()}@example.test",
        "department": " Engineering ",
        "joined_on": "2026-01-01",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }


def test_employee_create_persists_trimmed_fields_and_lists(live_api):
    client, db = live_api
    response = client.post("/employees", json=employee_payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Integration User"
    assert body["department"] == "Engineering"
    assert isinstance(body["created_at"], int)
    assert db.employees.count_documents({"emp_code": "EMP81234"}) == 1

    listed = client.get("/employees?department=Engineering&page=1&page_size=20")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["emp_code"] == "EMP81234"


def test_duplicate_employee_returns_conflict(live_api):
    client, db = live_api
    payload = employee_payload()
    assert client.post("/employees", json=payload).status_code == 201
    duplicate = client.post("/employees", json=payload)
    assert duplicate.status_code == 409
    assert db.employees.count_documents({"emp_code": payload["emp_code"]}) == 1


def test_duplicate_punch_in_returns_conflict(live_api):
    client, db = live_api
    payload = employee_payload()
    assert client.post("/employees", json=payload).status_code == 201
    timestamp = epoch_ms("2026-10-12T09:30:00+05:30")
    request = {"emp_code": payload["emp_code"], "punched_at": timestamp, "status": "PRESENT"}
    first = client.post("/attendance/punch-in", json=request)
    second = client.post("/attendance/punch-in", json=request)
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert db.attendance_logs.count_documents({"emp_code": payload["emp_code"], "date": "2026-10-12"}) == 1


def test_punch_out_calculates_work_hours_and_half_day(live_api):
    client, db = live_api
    payload = employee_payload()
    assert client.post("/employees", json=payload).status_code == 201
    punch_in = epoch_ms("2026-10-12T09:30:00+05:30")
    punch_out = epoch_ms("2026-10-12T13:59:00+05:30")
    created = client.post("/attendance/punch-in", json={"emp_code": payload["emp_code"], "punched_at": punch_in})
    assert created.status_code == 201, created.text
    closed = client.post("/attendance/punch-out", json={"emp_code": payload["emp_code"], "punched_at": punch_out})
    assert closed.status_code == 200, closed.text
    assert closed.json()["work_hours"] == 4.48
    assert closed.json()["half_day"] is True
    stored = db.attendance_logs.find_one({"emp_code": payload["emp_code"], "date": "2026-10-12"})
    assert stored["punch_out"] is not None
