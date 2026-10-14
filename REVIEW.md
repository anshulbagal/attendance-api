# REVIEW.md

Findings on the starter `app/main.py`. Top 10 critical defects identified, reproduced, and resolved:

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` / Datetimes | PyMongo defaults to naive datetimes in the server's local timezone, causing time drift. | A punch-in at 01:00 IST gets saved in local server time with no defined timezone offset. | Configured `tz_aware=True` on the client and standardized all stored instants as UTC datetimes. |
| 2 | `compute_late_minutes` | Floored minutes *before* the 10-minute grace check, so lateness between 10:00 and 10:59 was never marked late. | Shift at 09:30, punch-in at 09:40:30 returned **0** instead of **10**. | Compute the exact time difference in whole seconds first; mark late only if `delta > 600`, then floor to minutes. |
| 3 | `compute_work_hours` | Standard Python `round()` uses round-half-to-even (banker's rounding), violating strict round-half-up. | 450 seconds (0.125 h) rounded to **0.12** instead of **0.13**. | Replaced float rounding with exact integer arithmetic: `(2*secs + 36) // 72` hundredths. |
| 4 | `compute_overtime` | Missing the 30-minute minimum threshold; overnight shift end dates were miscalculated. | 20 minutes past shift end returned **20** instead of **0**. Overnight shift end was calculated a day early. | Added the 30-minute threshold check and added a +1 day offset for overnight shifts. |
| 5 | `attendance_date` | Assigned attendance dates based on local server clock rather than IST shift rules. | Punching in at 1:00 AM on a 22:00–06:00 overnight shift was assigned to the new day instead of yesterday's shift. | Implemented R1 logic in IST: if shift is overnight and punch is before shift end, it maps to the previous date. |
| 6 | `punch_in` concurrency | Used application-level check-then-insert (`find_one` then `insert_one`), causing race conditions. | Multiple simultaneous punch-in requests created duplicate records for the same employee on the same day. | Enforced a unique compound index on `(emp_code, date)` and caught `DuplicateKeyError` to return HTTP 409. |
| 7 | `list_employees` pagination | Used `skip = page * page_size`, which skipped page 1 entirely. | Requesting `page=1&page_size=20` started at record 21 instead of record 1. | Corrected the formula to `skip = (page - 1) * page_size`. |
| 8 | `list_attendance` memory usage | Loaded the entire collection into Python memory to sort and paginate with Python slices. | High memory consumption and multi-second latency on datasets with ~100k records. | Delegated querying, sorting, and pagination to MongoDB using indexed `.find().sort().skip().limit()`. |
| 9 | Attendance responses | Leaked MongoDB's internal `_id` and returned non-standard datetime strings. | Response showed `"id": "6acb..."` and raw ISO strings instead of integer epoch milliseconds. | Created `to_record()` helper to strip `_id` and format all instants into strict integer epoch milliseconds. |
| 10 | `/health` endpoint | Always returned `{"status": "ok"}` without ever verifying the database connection. | Shutting down MongoDB still resulted in `/health` returning HTTP 200. | Added an active `client.admin.command("ping")` check that raises HTTP 503 if MongoDB is down. |

**Looked at and decided it is fine**

- `load_dotenv()` does not override variables that are already set in the environment, so system environment variables always take precedence.
- Synchronous PyMongo with `def` endpoints: FastAPI executes standard `def` routes inside an AnyIO worker thread pool, ensuring non-blocking execution while keeping testing straightforward and thread-safe.
