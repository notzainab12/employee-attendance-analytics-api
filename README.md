# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0+ (uses `$setWindowFields`, `$dateAdd`, `$lookup` with `localField` + `pipeline`). Indexes are created at startup, idempotently.
Optional: `python sample_seed.py` loads the nine sample documents.

## What was and was not verified
**Verified** (`python tests/test_rules.py`, 11 checks): rules R1-R5 (late/overtime/work-hours rounding/half day, IST date, overnight
shift), the data-model sample values, and the status codes and history output of punch-in, punch-out and PATCH, run against a small
in-memory fake of the two collections.

**NOT verified: I had no MongoDB in my environment, so none of this has run against a real database:**
- the four aggregation pipelines (monthly, department summary, leaderboard, trend); I only checked that they build, and checked the
  integer half-up rounding formulas separately;
- `GET /admin/explain/...` and whether every plan shows `IXSCAN` and no `COLLSCAN`;
- real concurrency (parallel punch-in / punch-out / PATCH). The logic relies on the unique index and conditional updates, but I did not fire parallel requests;
- startup time and speed on ~100k records.
Please run the sample data, then a larger generated dataset, before submitting. If a pipeline errors, the message names the stage.

## Interpretation choices (the spec is silent or ambiguous)
- **Moving average** uses the *reported* (4-decimal rounded) daily rates, then rounds the mean half-up.
- **`working_days`** (employee monthly) is plain calendar arithmetic in Python (Mon-Fri from `max(month start, joined_on)`); the data itself is aggregated in MongoDB.
- **Trend range limit**: more than 92 days (rows) -> 422. Pipeline starts from one `employees` document, so `explain` for `department_trend` reports collection `employees`.
- **Punch-out order of checks**: 404 (no record) -> 409 (already out) -> 422 (time rules).
- **PATCH "changes nothing"** means none of `status`, `punch_in`, `punch_out` changed; an explicit `null` for those is 422.
- **PATCH concurrency**: optimistic check (record must be unchanged since read), up to 5 retries, then 409.
- **Unfiltered `GET /attendance` total** uses `estimated_document_count()` (instant); filtered totals use `count_documents`.
- Records dated before an employee's `joined_on` are not excluded from `present_days`; the contract does not say to.
- `.env.example` is mentioned in the kit's README but was not in the kit; set the two variables directly.
