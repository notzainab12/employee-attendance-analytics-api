# REVIEW.md

Found by reading the starter `app/main.py` (line numbers refer to the original starter) and checking each rule against `openapi.yaml`.
The rules/flow fixes are covered by `tests/test_rules.py` (in-memory fake, not a real MongoDB). The items that need a real database
(concurrency, index behaviour, scale) I could **not** run; they are reasoned from the code and marked "(not run)".

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` L33-37 | Builds shift start on the punch's own calendar day and in whatever zone the datetime has (not IST). Wrong day for overnight shifts. | Punch-in 09:40:01 IST on a 09:30 shift, or any punch-in whose UTC date differs from its IST date. | Shift start is built for the *attendance date* in IST (`shift_bounds`). |
| 2 | `compute_late_minutes` L36-37 | Floors to minutes *then* tests `> 10`, so 09:40:01 gives 10 -> 0 (R2 says 10). Sub-seconds not truncated. | 09:40:01 expected 10, got 0. | Compare seconds `> 600`, then floor to minutes; truncate to whole seconds first. |
| 3 | `compute_work_hours` L41 | `round()` is not half-up (binary float / banker's); sub-seconds not truncated. | Exactly 4h00m18s = 4.005h should be 4.01. | `Decimal` division + `ROUND_HALF_UP`. |
| 4 | `compute_overtime` L44-47 | No 30-minute minimum; end is built from `date_str` so overnight shifts end on the wrong day; naive vs aware datetimes would raise `TypeError`. | Punch-out 10 min after shift end returned 10, expected 0. 22:00-06:00 shift gave huge or zero overtime. | Uses `shift_bounds` (adds a day for overnight), threshold 30, floor to minutes. |
| 5 | `MongoClient` L22 | Not `tz_aware`: datetimes come back naive, so UTC/IST meaning is a guess. | Wrong dates / `TypeError` when mixing naive and aware. | `tz_aware=True`, UTC; conversion only at the API edge. Also a 5 s server-selection timeout. |
| 6 | Models L53-66 | No validation: emp_code format, email, shift format, `shift_start == shift_end`, `joined_on`, lengths; `status` is any string (ABSENT accepted by punch-in); `punched_at` accepts seconds, floats, numeric strings. | POST an employee with `"shift_start": "25:99"`, or punch-in with `status: "ABSENT"` -> 201 instead of 422. | Pydantic constraints per the contract; `StrictInt` + 1e11..4.1e12 range for epoch ms; `Literal` statuses. |
| 7 | `/health` L72-74 | Always 200, even when MongoDB is down. | Stop MongoDB, call `/health`. (not run) | Pings MongoDB; 503 on failure. |
| 8 | `create_employee` L77-85 | Check-then-insert race and no unique index, so parallel duplicates produce several 201s and duplicate rows. `created_at` is local naive time and is returned as a datetime/ISO string, not epoch ms. | Fire 20 parallel POSTs. (not run) | Unique index at startup; catch `DuplicateKeyError` -> 409; UTC `created_at`, returned as epoch ms. |
| 9 | `list_employees` L89-96 | `skip = page * page_size` skips the whole first page; `total` ignores the department filter; no sort; no page/page_size bounds (page_size=0 or page=0 gives errors/500). | `GET /employees?page=1` misses the first 20 rows; total wrong with `department`. | `skip = (page-1)*size`, `count_documents(filter)`, sort by `emp_code`, `ge/le` bounds. |
| 10 | `punch_in` L101 | Unknown employee -> `emp` is `None` -> `TypeError` -> 500. | Punch in `emp_code: "NOPE"`. | 404. |
| 11 | `punch_in` L102-103 | `datetime.fromtimestamp(ms/1000)` uses the server's local zone, so `date` is not the IST date (R1); no overnight rule; sub-seconds kept; `if body.punched_at` treats a falsy value as missing. | Punch-in at 23:30 UTC gets the wrong date on a UTC server. | `ms_to_dt` (UTC, truncated) + `attendance_date` (R1 incl. overnight). |
| 12 | `punch_in` L104-118 | Check-then-insert with no unique index: two simultaneous requests both pass the check. | 2 parallel identical punch-ins -> two 201s. (not run) | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 13 | `punch_in` L119-121 | Returns an `id` (ObjectId string), which the contract forbids; returns raw datetimes instead of epoch ms. | Response has `id`; `punch_in` is an ISO string. | `record_out()` serializer; `_id` never leaves the app. |
| 14 | `list_attendance` L144-150 | `find(q)` loads the whole result into memory, sorts and pages in Python (slow/unbounded at 100k); sort is by `date` only (no `emp_code` tie-break); exposes `id`; datetimes not ms; legacy docs missing `half_day`/`history` come back incomplete. | 100k records: seconds and big memory. Same-date rows in unstable order. (not run at scale) | Sort/skip/limit in MongoDB with `date desc, emp_code asc`, backed by indexes; `record_out` fills defaults. |
| 15 | `list_attendance` L125-143 | No validation of `date_from/date_to` or `status`; `date_from > date_to` is not rejected. | `?date_from=2026-07-10&date_to=2026-07-01` -> 200 empty instead of 422. | Strict date parsing, 422 on reversed range, `Literal` status. |
| 16 | Whole file | No indexes are created at startup. | `explain` shows COLLSCAN. (not run) | `ensure_indexes()` in the app lifespan, idempotent. |

## Looked at and judged NOT a defect
- `load_dotenv()` without `override=True`: real environment variables already win, as the contract requires.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct (India has no DST).
- `status_code=201` on the two POST endpoints: matches the contract.
- Comparing `date` strings with `$gte/$lte`: fine because `YYYY-MM-DD` sorts chronologically as text.
- `doc.pop("_id")` after `insert_one`: needed because PyMongo adds `_id` to the dict; I kept the idea but now serialize explicitly.

## Starter helpers: reused or not?
Kept the *names and purpose* (`compute_late_minutes`, `compute_work_hours`, `compute_overtime`) but rewrote them: none was correct as written (items 1-4), so reusing them unchanged would have copied the bugs.
