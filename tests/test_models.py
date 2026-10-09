"""Request-model validation tests; no live MongoDB is required."""
import pytest
from pydantic import ValidationError

from app.main import EmployeeIn, PunchInIn, PunchOutIn, RegularizeIn


def employee_payload(**overrides):
    data = {
        "emp_code": "EMP1234",
        "name": "Priyom Kataria",
        "email": "priyom@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    data.update(overrides)
    return data


def test_employee_accepts_valid_payload_and_defaults_shift():
    employee = EmployeeIn(**employee_payload())
    assert employee.emp_code == "EMP1234"
    assert employee.shift_start == "09:30"


def test_employee_rejects_invalid_emp_code():
    with pytest.raises(ValidationError):
        EmployeeIn(**employee_payload(emp_code="E1234"))


def test_employee_rejects_equal_shift_start_and_end():
    with pytest.raises(ValidationError):
        EmployeeIn(**employee_payload(shift_start="09:30", shift_end="09:30"))


def test_employee_rejects_invalid_joined_on_date():
    with pytest.raises(ValidationError):
        EmployeeIn(**employee_payload(joined_on="2026-02-30"))


def test_punch_in_rejects_boolean_timestamp():
    # bool is a subclass of int in Python, but is not a valid epoch timestamp.
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP1234", punched_at=True)


def test_punch_out_rejects_timestamp_outside_epoch_ms_range():
    with pytest.raises(ValidationError):
        PunchOutIn(emp_code="EMP1234", punched_at=12345)


def test_punch_in_rejects_invalid_status():
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP1234", status="LEAVE")


def test_regularization_requires_reason_and_actor():
    with pytest.raises(ValidationError):
        RegularizeIn(reason="ok", regularized_by="")
