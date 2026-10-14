# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
`GET /health` returns 200 once MongoDB answers. All indexes are created at startup and are idempotent.

## Notes
- Instants are epoch milliseconds on the wire and BSON datetimes (UTC) in MongoDB; calendar dates, months and shift times are IST strings.
- Business rules R1-R5 are small pure functions at the top of the file; derived fields are stored and analytics read the stored values.
- Concurrency: punch-in uses the unique `(emp_code, date)` index, punch-out is a compare-and-set update, and a correction is applied only if the record is unchanged since it was read (otherwise 409).
- Analytics are MongoDB aggregation pipelines. Half-up rounding is done in Python or in integer arithmetic, because `$round` is half-even.
- Nothing is missing from the assignment.
