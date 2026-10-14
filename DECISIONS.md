# DECISIONS.md

1. **Indexes.** On attendance, unique `(emp_code, date)` guarantees one daily log and safe punch-ins; `(date desc, emp_code)` handles pagination; `(status, date desc, emp_code)` filters by status; and `(emp_code, punch_in desc)` finds open shifts. On employees, I indexed unique `emp_code` and `(department, joined_on)` for headcount. I rejected copying `department` into logs because transfers would invalidate past records.

2. **Punch-in race.** When two requests hit simultaneously, both compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets the first succeed and rejects the second with `DuplicateKeyError`. My handler catches this and returns 409 Conflict, avoiding fragile check-then-insert code.

3. **Ties.** I used `$rank` (1, 2, 2, 4) and filtered `rank <= limit` afterwards. If employees tie at the cutoff, all are returned. It's fairer than arbitrarily dropping someone based on database ordering.

4. **Headcount.** My pipeline starts from `employees` (joined on or before month end) and `$lookup`s logs. Grouping uses `{$sum: 1}` per employee, ensuring zero-log employees are still counted.

5. **At 100x scale.** I would store daily summaries when punches close instead of querying raw logs, and switch from `skip`/`limit` to keyset (cursor) pagination.
