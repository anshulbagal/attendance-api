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


