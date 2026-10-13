# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code` (identity, 409 on duplicates), `(department, emp_code)`, `(department, joined_on)`, `joined_on`. Attendance: unique `(emp_code, date)` (race safety, per-employee queries), `(date desc, emp_code)` (list sort, leaderboard), `(status, date desc, emp_code)`, `(emp_code, punch_in desc)` (punch-out lookup). Rejected: an index on `late_minutes` (the date range already narrows it) and copying department into logs (it would go stale).

2. **Punch-in race.** Both requests compute the same date and insert. The unique index admits one; the other gets a duplicate-key error and returns 409. There is no check-first step.

3. **Ties.** Employees are ranked with `$rank`, then rows with `rank <= limit` are kept. Tied employees at the cutoff share a rank, so all are returned (more than `limit` rows), ordered by `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs, so someone with zero logs still counts once.

5. **100x data.** Store per-employee monthly totals, updated on punch and correction, instead of scanning and joining logs on every request.
