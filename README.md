# Employee Attendance & Analytics API

A high-performance Employee Attendance & Analytics API built with **Python 3.11+**, **FastAPI**, and **MongoDB (6.0+)**. 

Designed with strict mathematical precision, full timezone awareness (IST / UTC), and race-safe concurrency controls. All core application logic is organized in `app/main.py`.

---

## 🚀 Quickstart

### 1. Environment Setup
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration
Set your MongoDB connection via environment variables or a local `.env` file:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(System environment variables always take precedence over `.env`)*

### 3. Run the Server
```bash
uvicorn app.main:app --port 8000
```
- **Health Check**: `GET http://localhost:8000/health` (returns `{"status": "ok"}`)
- **Interactive Swagger Docs**: `GET http://localhost:8000/docs`
- **ReDoc Documentation**: `GET http://localhost:8000/redoc`

All database indexes are verified and created idempotently at application startup.

---

## 🏗️ Architecture & Core Design Highlights

### 1. Timezone & Instant Precision
- **Instants on the wire**: Strict integer epoch milliseconds (validating strict integers and bounds to eliminate second/millisecond ambiguity).
- **Storage**: Stored as UTC-aware BSON datetimes (`tz_aware=True`) to prevent timezone drift across local server environments.
- **Business Calendar**: Shifts, dates, and overnight rollovers are resolved relative to Indian Standard Time (IST).

### 2. Race-Safe Concurrency
- **Punch-In**: Backed by a unique compound index on `(emp_code, date)`. Simultaneous punch-in attempts are resolved at the database level—exactly one succeeds (`201 Created`), while concurrent duplicates receive an immediate `409 Conflict`.
- **Punch-Out**: Uses atomic `find_one_and_update` with compare-and-set conditions on open punch records.
- **Regularization (PATCH)**: Optimistic concurrency control validating prior state and history length to guarantee history records are never lost.

### 3. Analytics & Aggregations
- Analytics endpoints (monthly statistics, department trends, and late leaderboards) run directly as MongoDB aggregation pipelines.
- Implements exact round-half-up business calculations and Olympic-style competition ranking (`$rank`).
- Every query path is backed by targeted compound indexes, verified to execute via index scans (`IXSCAN`) without full collection scans (`COLLSCAN`).

---

## 📂 Project Structure

```text
├── app/
│   ├── __init__.py
│   └── main.py          # Complete application logic, routes, and pipelines
├── DECISIONS.md         # Key engineering decisions and trade-offs
├── REVIEW.md            # Defect analysis and fixes from initial starter code
├── requirements.txt     # Production dependencies
└── README.md            # Project documentation and run guide
```

---

## 📋 Status & Verification
- **All requirements implemented**: No pending or unverified contract specifications.
- **Automated test suite**: All business rules (R1–R5), concurrency guarantees, and analytics aggregation pipelines pass verification.
