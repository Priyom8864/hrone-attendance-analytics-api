"""Fast, database-independent tests for attendance calculation helpers.

Run after installing requirements with: pytest -q
These tests import the app module, but do not require a running MongoDB server.
"""
from datetime import date, datetime, timedelta, timezone

from app.main import (
    IST,
    attendance_date_for,
    compute_late_minutes,
    compute_overtime_for_employee,
    compute_work_hours,
    datetime_to_epoch_ms,
    epoch_ms_to_datetime,
    is_half_day,
    truncate_seconds,
)


def test_truncate_seconds_drops_microseconds():
    value = datetime(2026, 10, 9, 10, 11, 12, 987654, tzinfo=timezone.utc)
    assert truncate_seconds(value).microsecond == 0


def test_epoch_milliseconds_round_trip_at_whole_second():
    value = datetime(2026, 10, 9, 10, 11, 12, tzinfo=timezone.utc)
    assert epoch_ms_to_datetime(datetime_to_epoch_ms(value)) == value


def test_late_grace_is_strictly_more_than_ten_minutes():
    day = date(2026, 10, 9)
    on_time_edge = datetime(2026, 10, 9, 9, 10, tzinfo=IST)
    over_edge = datetime(2026, 10, 9, 9, 10, 1, tzinfo=IST)
    assert compute_late_minutes(on_time_edge, "09:00", "17:00", day) == 0
    assert compute_late_minutes(over_edge, "09:00", "17:00", day) == 10


def test_work_hours_round_half_up_to_two_decimals():
    start = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    # 4 hours 30 minutes is exactly 4.50 hours.
    assert compute_work_hours(start, start + timedelta(minutes=270)) == 4.5


def test_overnight_punch_before_shift_end_belongs_to_previous_day():
    punch = datetime(2026, 10, 10, 5, 30, tzinfo=IST)
    assert attendance_date_for(punch, "22:00", "06:00") == date(2026, 10, 9)


def test_overnight_overtime_uses_next_calendar_day_for_shift_end():
    day = date(2026, 10, 9)
    punch_out = datetime(2026, 10, 10, 6, 35, tzinfo=IST)
    assert compute_overtime_for_employee(punch_out, "22:00", "06:00", day) == 35


def test_overtime_requires_at_least_thirty_minutes():
    day = date(2026, 10, 9)
    shift_end = datetime(2026, 10, 9, 17, 0, tzinfo=IST)
    assert compute_overtime_for_employee(shift_end + timedelta(minutes=29), "09:00", "17:00", day) == 0
    assert compute_overtime_for_employee(shift_end + timedelta(minutes=30), "09:00", "17:00", day) == 30


def test_half_day_threshold_uses_rounded_hours_strictly_below_450():
    assert is_half_day(4.49) is True
    assert is_half_day(4.50) is False
    assert is_half_day(4.51) is False
    assert is_half_day(None) is False


def test_overnight_punch_in_date_validation_uses_shift_day():
    # A 05:30 IST punch for a 22:00-06:00 shift belongs to the previous day.
    punch = datetime(2026, 10, 10, 5, 30, tzinfo=IST)
    assert attendance_date_for(punch, "22:00", "06:00").isoformat() == "2026-10-09"


def test_employee_text_fields_are_trimmed_and_blank_is_rejected():
    from pydantic import ValidationError
    from app.main import EmployeeIn

    employee = EmployeeIn(
        emp_code="EMP1234", name="  Priyom  ", email="priyom@example.com",
        department="  Engineering ", joined_on="2026-01-01",
    )
    assert employee.name == "Priyom"
    assert employee.department == "Engineering"
    try:
        EmployeeIn(
            emp_code="EMP1234", name="   ", email="priyom@example.com",
            department="Engineering", joined_on="2026-01-01",
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("whitespace-only employee name should be rejected")
