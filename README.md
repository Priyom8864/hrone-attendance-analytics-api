# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Requirements

- Python 3.10+
- MongoDB 6.0+ (MongoDB 7.0 recommended)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000 --reload
```

The app reads `MONGO_URI` and `MONGO_DB` from environment variables. Defaults are `mongodb://localhost:27017` and `attendance_db`. On startup it creates the indexes used for uniqueness and common query paths. The database must be reachable when the app starts.

Open `http://localhost:8000/docs` for the interactive API documentation. The service's health endpoint is `GET /health`.

## Notes

- API timestamps are epoch milliseconds; MongoDB datetimes represent UTC instants.
- Attendance dates use the Asia/Kolkata (IST) calendar and overnight shifts are assigned to their shift-start date.
- `REVIEW.md` records issues identified in the supplied starter. `DECISIONS.md` explains key implementation choices.
- `PART_B5_TEST_PLAN.md` is a suggested manual/integration test checklist; it is not a claim that all tests have passed.

## Before submission

Run the API against MongoDB 7.0 and execute the required edge-case tests, especially overnight shifts, simultaneous duplicate requests, regularization history, monthly analytics, department trend gap filling, and explain plans. Do not commit credentials, `.env` files, or other secrets.

## Test coverage and limitations

Run `python validate_submission.py` for offline structure checks and `pytest -q` for helper calculation and request-model validation tests. These tests do not emulate MongoDB or establish endpoint integration correctness. See `PART_J_TEST_RESULTS.md` for the latest validation scope. Full endpoint and MongoDB integration testing remains required.
