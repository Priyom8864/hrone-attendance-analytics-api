"""HTTP-level validation tests that do not require a live MongoDB server."""
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_create_employee_invalid_code_returns_422_before_database_access():
    response = client.post(
        "/employees",
        json={
            "emp_code": "E1234",
            "name": "Test Person",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert response.status_code == 422


def test_create_employee_invalid_email_returns_422_before_database_access():
    response = client.post(
        "/employees",
        json={
            "emp_code": "EMP1234",
            "name": "Test Person",
            "email": "not-an-email",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert response.status_code == 422


def test_employee_pagination_rejects_page_size_over_100():
    response = client.get("/employees?page=1&page_size=101")
    assert response.status_code == 422


def test_employee_pagination_rejects_page_zero():
    response = client.get("/employees?page=0&page_size=20")
    assert response.status_code == 422


def test_punch_in_rejects_invalid_status_before_database_access():
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1234", "status": "LEAVE", "punched_at": 1791500000000},
    )
    assert response.status_code == 422
