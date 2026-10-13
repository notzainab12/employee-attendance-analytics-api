"""
Offline checks for the business rules (R1-R5) and the endpoint flow (status codes, history).
They use an in-memory fake instead of MongoDB, so they do NOT prove that the aggregation
pipelines or indexes work - see README.md. Run:  python tests/test_rules.py   (or pytest)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import main as m  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

IST = m.IST


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST).astimezone(timezone.utc)


def ms(dt):
    return m.dt_to_ms(dt)


EMP = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


# ---------------------------------------------------------------- pure rules
def test_late_rule_examples():
    start = ist(2026, 7, 6, 9, 30)
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), start) == 0
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), start) == 10
    assert m.compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), start) == 45
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 0), start) == 0
    # sub-second part is dropped first: 09:40:00.900 counts as 09:40:00
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900000), start) == 0


def test_overtime_threshold():
    end = ist(2026, 7, 6, 18, 30)
    assert m.compute_overtime(ist(2026, 7, 6, 18, 59, 59), end) == 0   # 29 min
    assert m.compute_overtime(ist(2026, 7, 6, 19, 0, 0), end) == 30
    assert m.compute_overtime(ist(2026, 7, 6, 19, 10), end) == 40
    assert m.compute_overtime(ist(2026, 7, 6, 17, 0), end) == 0


def test_work_hours_half_up_and_half_day():
    a = ist(2026, 7, 6, 9, 0)
    # 4h + 18s = 4.005h -> half-up 4.01 (banker's/float round gives 4.0)
    assert m.compute_work_hours(a, ist(2026, 7, 6, 13, 0, 18)) == 4.01
    assert m.compute_work_hours(a, ist(2026, 7, 6, 12, 30)) == 3.5
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.50) is False
    # 4.495h = 4h29m42s -> rounds to 4.50 (half-up) -> NOT a half day (R5 uses the rounded value)
    wh = m.compute_work_hours(a, ist(2026, 7, 6, 13, 29, 42))
    assert wh == 4.5 and m.compute_half_day(wh) is False


def test_attendance_date_and_overnight():
    assert m.attendance_date(ist(2026, 7, 6, 9, 28), EMP) == "2026-07-06"
    # 23:50 UTC on the 5th is 05:20 IST on the 6th -> IST date, not UTC date
    assert m.attendance_date(datetime(2026, 7, 5, 23, 50, tzinfo=timezone.utc), EMP) == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), NIGHT) == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 5, 59), NIGHT) == "2026-07-06"   # earlier than shift_end -> previous day
    assert m.attendance_date(ist(2026, 7, 7, 6, 0), NIGHT) == "2026-07-07"    # not earlier than shift_end


def test_sample_documents_from_data_model():
    e = dict(EMP)
    d = m.derive(e, "2026-07-06", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
    assert (d["late_minutes"], d["overtime_minutes"], d["work_hours"], d["half_day"]) == (35, 40, 9.08, False)
    d = m.derive(e, "2026-07-06", ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35))
    assert (d["late_minutes"], d["overtime_minutes"], d["work_hours"]) == (0, 0, 9.12)
    d = m.derive(NIGHT, "2026-07-06", ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40))
    assert (d["late_minutes"], d["overtime_minutes"], d["work_hours"]) == (0, 40, 8.75)
    d = m.derive(e, "2026-07-07", ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0))
    assert d["work_hours"] == 3.5 and d["half_day"] is True


def test_conversions_and_working_days():
    assert m.dt_to_ms(m.ms_to_dt(1783312500999)) == 1783312500000
    first, last = m.month_bounds("2026-07")[2:]
    j = m.Date.fromisoformat
    assert m.count_working_days(first, last, j("2026-01-05")) == 23
    assert m.count_working_days(first, last, j("2026-07-13")) == 15   # joined Mon 13th
    assert m.count_working_days(first, last, j("2026-08-01")) == 0
    assert m.round_half_up(0.125, 2) == m.Decimal("0.13")


# ---------------------------------------------------------------- fake database
def _match(doc, flt):
    for k, v in flt.items():
        if k == "$or":
            if not any(_match(doc, s) for s in v):
                return False
            continue
        val = doc.get(k)
        if isinstance(v, dict) and any(x.startswith("$") for x in v):
            for op, arg in v.items():
                if op == "$lte" and not (val is not None and val <= arg): return False
                if op == "$gte" and not (val is not None and val >= arg): return False
                if op == "$exists" and ((k in doc) != arg): return False
                if op == "$size" and not (isinstance(val, list) and len(val) == arg): return False
        elif val != v:
            return False
    return True


class FakeColl:
    def __init__(self, unique):
        self.docs, self.unique, self.n = [], unique, 0

    def find_one(self, flt, sort=None):
        rows = [d for d in self.docs if _match(d, flt)]
        for key, direction in reversed(sort or []):
            rows.sort(key=lambda d: d[key], reverse=direction == -1)
        return rows[0] if rows else None

    def insert_one(self, doc):
        if any(all(d.get(k) == doc.get(k) for k in self.unique) for d in self.docs):
            raise DuplicateKeyError("dup")
        self.n += 1
        doc["_id"] = self.n
        self.docs.append(doc)

    def find_one_and_update(self, flt, upd, return_document=None):
        d = self.find_one(flt)
        if d is None:
            return None
        d.update(upd.get("$set", {}))
        for k, v in upd.get("$push", {}).items():
            d.setdefault(k, []).append(v)
        return d


class FakeDB:
    def __init__(self):
        self.employees = FakeColl(["emp_code"])
        self.attendance_logs = FakeColl(["emp_code", "date"])


def fresh():
    m.db = FakeDB()
    for code, shift in (("EMP0001", EMP), ("EMP0005", NIGHT)):
        m.create_employee(m.EmployeeIn(emp_code=code, name="N", email="a@b.co", department="Eng",
                                       joined_on="2026-01-05", **shift))


def status_of(fn, *a, **k):
    try:
        fn(*a, **k)
        return 200
    except HTTPException as e:
        return e.status_code


# ---------------------------------------------------------------- endpoint flow
def test_duplicate_employee_409():
    fresh()
    assert status_of(m.create_employee, m.EmployeeIn(emp_code="EMP0001", name="N", email="a@b.co",
                                                     department="Eng", joined_on="2026-01-05")) == 409


def test_punch_in_out_flow():
    fresh()
    pi = ms(ist(2026, 7, 6, 10, 5))
    rec = m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=pi))
    assert rec["date"] == "2026-07-06" and rec["late_minutes"] == 35 and rec["work_hours"] is None
    assert rec["punch_in"] == pi and "id" not in rec and rec["history"] == []
    assert status_of(m.punch_in, m.PunchInIn(emp_code="EMP0001", punched_at=pi)) == 409
    assert status_of(m.punch_in, m.PunchInIn(emp_code="NOPE")) == 404
    po = ms(ist(2026, 7, 6, 19, 10))
    out = m.punch_out(m.PunchOutIn(emp_code="EMP0001", punched_at=po))
    assert (out["work_hours"], out["overtime_minutes"], out["half_day"]) == (9.08, 40, False)
    assert status_of(m.punch_out, m.PunchOutIn(emp_code="EMP0001", punched_at=po)) == 409
    assert status_of(m.punch_out, m.PunchOutIn(emp_code="EMP0001", punched_at=pi - 3600_000)) == 404


def test_punch_out_validation_and_overnight():
    fresh()
    pi = ms(ist(2026, 7, 6, 21, 55))
    m.punch_in(m.PunchInIn(emp_code="EMP0005", punched_at=pi))
    assert status_of(m.punch_out, m.PunchOutIn(emp_code="EMP0005", punched_at=pi)) == 422          # not after
    assert status_of(m.punch_out, m.PunchOutIn(emp_code="EMP0005", punched_at=pi + 25 * 3600_000)) == 422
    out = m.punch_out(m.PunchOutIn(emp_code="EMP0005", punched_at=ms(ist(2026, 7, 7, 6, 40))))
    assert out["date"] == "2026-07-06" and out["work_hours"] == 8.75 and out["overtime_minutes"] == 40


def test_regularize_flow():
    fresh()
    pi, po = ms(ist(2026, 7, 7, 10, 5)), ms(ist(2026, 7, 7, 19, 0))
    m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=pi))
    m.punch_out(m.PunchOutIn(emp_code="EMP0001", punched_at=po))
    body = lambda **k: m.RegularizeIn(reason="biometric glitch", regularized_by="hr", **k)  # noqa: E731
    new_in = ms(ist(2026, 7, 7, 9, 28))
    r = m.regularize("EMP0001", "2026-07-07", body(punch_in=new_in))
    assert r["late_minutes"] == 0 and r["punch_in"] == new_in
    assert len(r["history"]) == 1
    ch = r["history"][0]["changes"]
    assert ch["punch_in"] == {"from": pi, "to": new_in}          # epoch ms inside history, not datetimes
    assert ch["late_minutes"] == {"from": 35, "to": 0} and "overtime_minutes" not in ch
    assert status_of(m.regularize, "EMP0001", "2026-07-07", body(punch_in=new_in)) == 422     # changes nothing
    assert status_of(m.regularize, "EMP0001", "2026-07-07", body(punch_in=ms(ist(2026, 7, 8, 9, 0)))) == 422  # other day
    assert status_of(m.regularize, "EMP0001", "2026-07-07", body(punch_out=new_in)) == 422    # out <= in
    assert status_of(m.regularize, "EMP0001", "2026-07-07", body(status="LEAVE", punch_in=new_in)) == 422
    assert status_of(m.regularize, "EMP0001", "2026-07-09", body(status="LEAVE")) == 404
    assert status_of(m.regularize, "NOPE", "2026-07-07", body(status="LEAVE")) == 404
    assert status_of(m.regularize, "EMP0001", "07-07-2026", body(status="LEAVE")) == 422
    r = m.regularize("EMP0001", "2026-07-07", body(status="LEAVE"))
    assert r["punch_in"] is None and r["work_hours"] is None and len(r["history"]) == 2
    assert status_of(m.regularize, "EMP0001", "2026-07-07", body(status="PRESENT")) == 422   # no punch_in to use
    assert r["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}


def test_legacy_record_defaults():
    out = m.record_out({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                        "punch_in": ist(2026, 7, 14, 9, 32), "punch_out": ist(2026, 7, 14, 18, 31),
                        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    assert out["half_day"] is False and out["history"] == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
        print("ok  ", f.__name__)
    print(f"{len(fns)} passed")
