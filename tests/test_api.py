"""Integration tests: need a real MongoDB 6+ (they are SKIPPED, not failed, when none is reachable).

    pytest tests/test_api.py -v

Expected values are derived by hand from the contract (see the comments next to each dataset),
not by running the implementation. Concurrency tests call the endpoint functions from many threads
at once (released together by a Barrier) so the race happens inside the real database.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import main

pytestmark = pytest.mark.usefixtures("clean")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ms(y, mo, d, h, mi, s=0):
    """IST wall-clock -> epoch milliseconds (computed independently of the app)."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def seed_emp(code, dept="Eng", joined="2026-01-05", start="09:30", end="18:30", name=None):
    main.db.employees.insert_one({"emp_code": code, "name": name or f"Name {code}", "email": f"{code.lower()}@x.com",
                                  "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
                                  "created_at": datetime(2026, 1, 5, tzinfo=UTC)})


def seed_log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False, punches=True):
    d = date.fromisoformat(day)
    pin = datetime(d.year, d.month, d.day, 9, 30, tzinfo=IST).astimezone(UTC) if punches else None
    pout = pin + timedelta(hours=wh) if (pin and wh is not None) else None
    main.db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": pin, "punch_out": pout,
                                        "work_hours": wh, "late_minutes": late, "overtime_minutes": ot,
                                        "half_day": half, "history": []})


def parallel(fn, n):
    """Run fn in n threads released at the same instant; returns [('ok', result) | ('http', status)]."""
    barrier = threading.Barrier(n)

    def task():
        barrier.wait()
        try:
            return ("ok", fn())
        except HTTPException as exc:
            return ("http", exc.status_code)

    with ThreadPoolExecutor(n) as pool:
        return [f.result() for f in [pool.submit(task) for _ in range(n)]]


# =========================================================================== startup & employees
def test_health_and_startup_indexes(api):
    assert api.get("/health").json() == {"status": "ok"}
    logs = main.db.attendance_logs.index_information()
    assert logs["uniq_emp_date"]["unique"] is True and logs["uniq_emp_date"]["key"] == [("emp_code", 1), ("date", 1)]
    assert {"date_emp", "status_date_emp"} <= set(logs)
    assert main.db.employees.index_information()["uniq_emp_code"]["unique"] is True
    main.ensure_indexes()  # idempotent: a second run must not raise


def test_health_is_503_when_mongo_does_not_answer(monkeypatch, api):
    from pymongo import MongoClient
    monkeypatch.setattr(main, "client", MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=200))
    assert api.get("/health").status_code == 503


EMP = dict(emp_code="EMP0001", name="Asha", email="asha@example.com", department="Eng", joined_on="2026-01-05")


def test_employee_create_validate_and_duplicate(api):
    r = api.post("/employees", json=EMP)
    assert r.status_code == 201
    body = r.json()
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30" and "_id" not in body
    assert isinstance(body["created_at"], int) and body["created_at"] > 10**12
    assert api.post("/employees", json={**EMP, "name": "Other"}).status_code == 409
    assert api.post("/employees", json={**EMP, "created_at": 5}).status_code == 409  # created_at is never client supplied
    for bad in ({"emp_code": "E1"}, {"shift_end": "09:30"}, {"joined_on": "2026-02-30"}, {"email": "x"}):
        assert api.post("/employees", json={**EMP, "emp_code": "EMP0002", **bad}).status_code == 422, bad


def test_parallel_duplicate_employee_creation_yields_one_201(api):
    results = parallel(lambda: main.create_employee(main.EmployeeIn(**EMP)), 10)
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert [v for kind, v in results if kind == "http"] == [409] * 9
    assert main.db.employees.count_documents({}) == 1


def test_employee_list_sorted_paginated_with_filtered_total(api):
    for i in range(1, 8):
        seed_emp(f"EMP{i:04d}", dept="Eng" if i % 2 else "Ops")
    page = api.get("/employees", params={"department": "Eng", "page_size": 2, "page": 2}).json()
    assert page["total"] == 4                                   # total reflects the filter (4 Eng of 7)
    assert [e["emp_code"] for e in page["items"]] == ["EMP0005", "EMP0007"]  # sorted, page 2 (not page 3)
    assert api.get("/employees", params={"page_size": 100}).status_code == 200
    assert api.get("/employees", params={"page_size": 101}).status_code == 422
    assert api.get("/employees", params={"page": 0}).status_code == 422


# =========================================================================== punch-in
def test_punch_in_unknown_employee_and_invalid_timestamps(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0404"}).status_code == 404
    seed_emp("EMP0001")
    for bad in (1783312500, 1783312500000.0, "1783312500000", True, 9_999_999_999_999):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"}).status_code == 422


def test_punch_in_grace_period_boundaries_and_truncation(api):
    for code, (h, mi, s, extra_ms, expected) in {
        "EMP0001": (9, 40, 0, 0, 0), "EMP0002": (9, 40, 1, 0, 10), "EMP0003": (10, 15, 59, 900, 45),
    }.items():
        seed_emp(code)
        r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, 6, h, mi, s) + extra_ms})
        assert r.status_code == 201
        body = r.json()
        assert body["late_minutes"] == expected and body["date"] == "2026-07-06"
        assert body["punch_in"] == ms(2026, 7, 6, h, mi, s)  # truncated to whole seconds and stored that way
        assert body["punch_out"] is None and body["work_hours"] is None and body["history"] == []
        assert body["overtime_minutes"] == 0 and body["half_day"] is False


def test_punch_in_ignores_client_supplied_derived_fields_and_returns_no_internal_ids(api):
    seed_emp("EMP0001")
    body = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30),
                                                  "late_minutes": 99, "work_hours": 12, "half_day": True}).json()
    assert body["late_minutes"] == 0 and body["work_hours"] is None and body["half_day"] is False
    assert "_id" not in body and "id" not in body


def test_punch_in_date_is_the_ist_date_near_midnight(api):
    seed_emp("EMP0001")  # day shift; 00:10 IST on the 7th is still the 6th in UTC
    body = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 10)}).json()
    assert body["date"] == "2026-07-07"


def test_overnight_punch_in_after_midnight_belongs_to_previous_day(api):
    seed_emp("EMP0005", start="22:00", end="06:00")
    body = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 1, 0)}).json()
    assert body["date"] == "2026-07-06" and body["late_minutes"] == 180  # 3h after the 22:00 start


def test_duplicate_punch_in_is_409(api):
    seed_emp("EMP0001")
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 201
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 5)}).status_code == 409


def test_parallel_punch_in_exactly_one_201_rest_409(api):
    seed_emp("EMP0001")
    results = parallel(lambda: main.punch_in(main.PunchInIn(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 30))), 12)
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert [v for kind, v in results if kind == "http"] == [409] * 11
    assert main.db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


# =========================================================================== punch-out
def punch_in_at(api, code, y, mo, d, h, mi, s=0):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(y, mo, d, h, mi, s)})
    assert r.status_code == 201, r.text
    return r.json()


def punch_out_at(api, code, y, mo, d, h, mi, s=0):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(y, mo, d, h, mi, s)})


def test_punch_out_computes_hours_overtime_and_half_day(api):
    seed_emp("EMP0001")
    punch_in_at(api, "EMP0001", 2026, 7, 6, 9, 28)
    r = punch_out_at(api, "EMP0001", 2026, 7, 6, 19, 10, 59)
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.72 and b["overtime_minutes"] == 40 and b["half_day"] is False  # 9h42m59s = 9.7164h
    assert b["punch_out"] == ms(2026, 7, 6, 19, 10, 59) and b["history"] == []  # punch-out never writes history

    seed_emp("EMP0002")
    punch_in_at(api, "EMP0002", 2026, 7, 7, 9, 30)
    b = punch_out_at(api, "EMP0002", 2026, 7, 7, 13, 59, 59).json()  # 4h29m59s = 4.4997h -> 4.50 -> NOT half day
    assert b["work_hours"] == 4.5 and b["half_day"] is False and b["overtime_minutes"] == 0
    seed_emp("EMP0003")
    punch_in_at(api, "EMP0003", 2026, 7, 7, 9, 30)
    b = punch_out_at(api, "EMP0003", 2026, 7, 7, 13, 29, 59).json()  # 3h59m59s -> 4.00 hours -> half day
    assert b["work_hours"] == 4.0 and b["half_day"] is True


def test_punch_out_overtime_threshold_boundaries(api):
    for code, (h, mi, s, expected) in {"EMP0001": (18, 59, 59, 0), "EMP0002": (19, 0, 0, 30)}.items():
        seed_emp(code)
        punch_in_at(api, code, 2026, 7, 6, 9, 30)
        assert punch_out_at(api, code, 2026, 7, 6, h, mi, s).json()["overtime_minutes"] == expected


def test_overnight_punch_out_across_midnight(api):
    seed_emp("EMP0005", start="22:00", end="06:00")
    punch_in_at(api, "EMP0005", 2026, 7, 6, 21, 55)
    b = punch_out_at(api, "EMP0005", 2026, 7, 7, 6, 40).json()   # DATA_MODEL sample 7
    assert b["date"] == "2026-07-06" and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40


def test_punch_out_errors(api):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0404"}).status_code == 404
    seed_emp("EMP0001")
    assert punch_out_at(api, "EMP0001", 2026, 7, 6, 18, 0).status_code == 404            # no punch-in at all
    punch_in_at(api, "EMP0001", 2026, 7, 6, 9, 30)
    assert punch_out_at(api, "EMP0001", 2026, 7, 6, 9, 30).status_code == 422             # == punch_in
    assert punch_out_at(api, "EMP0001", 2026, 7, 6, 8, 0).status_code == 404              # before any punch-in
    assert punch_out_at(api, "EMP0001", 2026, 7, 7, 9, 30, 1).status_code == 422          # 24h + 1s
    assert punch_out_at(api, "EMP0001", 2026, 7, 7, 9, 30, 0).status_code == 200          # exactly 24h is allowed
    assert punch_out_at(api, "EMP0001", 2026, 7, 7, 10, 0).status_code == 409             # already punched out
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code == 422


def test_parallel_punch_out_exactly_one_200(api):
    seed_emp("EMP0001")
    main.punch_in(main.PunchInIn(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 9, 30)))
    results = parallel(lambda: main.punch_out(main.PunchOutIn(emp_code="EMP0001", punched_at=ms(2026, 7, 6, 18, 30))), 12)
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert [v for kind, v in results if kind == "http"] == [409] * 11


# =========================================================================== regularize (PATCH)
BASE = {"reason": "biometric glitch", "regularized_by": "hr.admin"}


def open_day(api, code="EMP0001"):
    seed_emp(code)
    punch_in_at(api, code, 2026, 7, 6, 10, 5)               # 35 min late
    punch_out_at(api, code, 2026, 7, 6, 19, 10)             # 40 min overtime
    return f"/attendance/{code}/2026-07-06"


def test_regularize_recomputes_and_records_only_actual_changes(api):
    path = open_day(api)
    r = api.patch(path, json={**BASE, "punch_in": ms(2026, 7, 6, 9, 28)})
    assert r.status_code == 200
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.7 and b["overtime_minutes"] == 40  # 9h42m
    entry = b["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}      # overtime did not change -> absent
    assert entry["changes"]["punch_in"] == {"from": ms(2026, 7, 6, 10, 5), "to": ms(2026, 7, 6, 9, 28)}  # ms ints, not dates
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)     # BSON dates in the database


def test_regularize_appends_never_overwrites_history(api):
    path = open_day(api)
    api.patch(path, json={**BASE, "punch_in": ms(2026, 7, 6, 9, 28)})
    r = api.patch(path, json={**BASE, "reason": "second fix", "punch_out": ms(2026, 7, 6, 18, 0)})
    hist = r.json()["history"]
    assert [h["reason"] for h in hist] == ["biometric glitch", "second fix"]
    assert hist[1]["changes"]["punch_out"]["from"] == ms(2026, 7, 6, 19, 10)
    assert api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"] == hist  # same shape in list


def test_regularize_status_changes_and_validation(api):
    path = open_day(api)
    assert api.patch(path, json={**BASE}).status_code == 422                                          # nothing changes
    assert api.patch(path, json={**BASE, "punch_in": ms(2026, 7, 6, 10, 5)}).status_code == 422       # same value
    assert api.patch(path, json={**BASE, "status": "ABSENT", "punch_in": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert api.patch(path, json={**BASE, "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422        # leaves the attendance date
    assert api.patch(path, json={**BASE, "punch_out": ms(2026, 7, 6, 9, 0)}).status_code == 422       # before punch_in
    assert api.patch(path, json={**BASE, "punch_out": ms(2026, 7, 7, 10, 5, 1)}).status_code == 422   # > 24h
    assert api.patch(path, json={"reason": "x", "regularized_by": "hr", "status": "LEAVE"}).status_code == 422
    assert api.patch(path, json={**BASE, "late_minutes": 0, "status": "LEAVE"}).status_code == 200    # derived field ignored
    b = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert b["status"] == "LEAVE" and b["punch_in"] is None and b["punch_out"] is None and b["work_hours"] is None
    assert b["late_minutes"] == 0 and b["overtime_minutes"] == 0 and b["half_day"] is False
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    assert api.patch(path, json={**BASE, "status": "WFH"}).status_code == 422                         # presence needs punch_in
    assert api.patch(path, json={**BASE, "status": "WFH", "punch_in": ms(2026, 7, 6, 9, 0)}).status_code == 200


def test_regularize_404s(api):
    path = open_day(api)
    assert api.patch("/attendance/EMP0404/2026-07-06", json={**BASE, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**BASE, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-09", json={**BASE, "status": "LEAVE"}).status_code == 422


def test_concurrent_corrections_never_lose_a_history_entry(api):
    path = open_day(api)
    n = 8

    def correct(i):
        def go():
            body = main.RegularizeIn(reason=f"correction {i}", regularized_by="hr", punch_out=ms(2026, 7, 6, 19, 11 + i))
            return main.regularize_attendance("EMP0001", "2026-07-06", body)
        return go

    barrier = threading.Barrier(n)

    def task(i):
        barrier.wait()
        try:
            return ("ok", correct(i)())
        except HTTPException as exc:
            return ("http", exc.status_code)

    with ThreadPoolExecutor(n) as pool:
        results = [f.result() for f in [pool.submit(task, i) for i in range(n)]]
    ok = [r for kind, r in results if kind == "ok"]
    assert ok and all(v == 409 for kind, v in results if kind == "http")      # loser may be 409, never 500
    history = main.db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]
    reasons = [h["reason"] for h in history]
    assert len(history) == len(ok) and len(set(reasons)) == len(reasons)        # one entry per success, none lost/duplicated
    chain = [h["changes"]["punch_out"] for h in history]                        # a consistent linear history:
    assert chain[0]["from"] == datetime(2026, 7, 6, 13, 40, tzinfo=UTC)         # starts at the original value (19:10 IST)
    for earlier, later in zip(chain, chain[1:]):
        assert earlier["to"] == later["from"]


# =========================================================================== attendance list / pagination
def test_attendance_list_filters_sort_pagination_and_total(api):
    seed_emp("EMP0001"); seed_emp("EMP0002")
    for i in range(30):                                  # 30 consecutive days for EMP0001
        d = (date(2026, 6, 1) + timedelta(days=i)).isoformat()
        seed_log("EMP0001", d, status="WFH" if i % 3 == 0 else "PRESENT", wh=8.0)
    seed_log("EMP0002", "2026-06-30", wh=8.0)            # same date as EMP0001's last day: emp_code tie-break

    r = api.get("/attendance", params={"page_size": 100}).json()
    assert r["total"] == 31 and len(r["items"]) == 31
    assert [(x["date"], x["emp_code"]) for x in r["items"][:3]] == [("2026-06-30", "EMP0001"), ("2026-06-30", "EMP0002"), ("2026-06-29", "EMP0001")]

    p = api.get("/attendance", params={"emp_code": "EMP0001", "page": 2, "page_size": 20}).json()
    assert p["total"] == 30 and len(p["items"]) == 10 and p["page"] == 2 and p["page_size"] == 20
    f = api.get("/attendance", params={"date_from": "2026-06-10", "date_to": "2026-06-12", "status": "WFH"}).json()
    assert all(x["status"] == "WFH" for x in f["items"])
    assert [x["date"] for x in f["items"]] == ["2026-06-10"] and f["total"] == 1   # only i=9 (Jun 10) is WFH in that range
    assert api.get("/attendance", params={"date_from": "2026-06-30", "date_to": "2026-06-01"}).status_code == 422
    assert api.get("/attendance", params={"page_size": 101}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "06/10/2026"}).status_code == 422


def test_legacy_documents_without_history_or_half_day_are_listed(api):
    main.db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC), "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
                                        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = api.get("/attendance").json()["items"][0]
    assert item["history"] == [] and item["half_day"] is False
    assert item["punch_in"] == int(datetime(2026, 7, 14, 4, 2, tzinfo=UTC).timestamp()) * 1000
    assert "_id" not in item


# =========================================================================== analytics
def seed_analytics():
    """July 2026: Wed 1 .. Fri 31, so there are 23 working days; 6 Jul is a Monday, 11 Jul a Saturday."""
    seed_emp("EMP1001"); seed_emp("EMP1002", joined="2026-07-15"); seed_emp("EMP1003")      # EMP1003: no logs at all
    seed_emp("EMP1004", joined="2026-08-01")                                                  # joins after July
    seed_emp("EMP2001", dept="Ops"); seed_emp("EMP2002", dept="Ops")
    seed_emp("EMP3001", dept="Future", joined="2026-09-01")
    seed_log("EMP1001", "2026-07-06", wh=9.08, late=35, ot=40)
    seed_log("EMP1001", "2026-07-07", status="WFH", wh=3.5, half=True)
    seed_log("EMP1001", "2026-07-08", status="LEAVE", punches=False)
    seed_log("EMP1001", "2026-07-09", status="ABSENT", punches=False)
    seed_log("EMP1001", "2026-07-11", wh=8.0, late=15)                    # Saturday: counts late, NOT present
    seed_log("EMP1001", "2026-06-30", wh=9.0, late=20)                    # other months must not leak in
    seed_log("EMP1001", "2026-08-03", wh=9.0, late=20)
    seed_log("EMP1002", "2026-07-15", wh=9.0)
    seed_log("EMP1002", "2026-07-16", status="ON_DUTY", wh=9.0, late=12)
    seed_log("EMP2001", "2026-07-06", wh=9.0, late=30)
    seed_log("EMP2002", "2026-07-06", wh=9.0, late=30)
    seed_log("EMP9999", "2026-07-06", wh=9.0, late=100)                   # employee does not exist: ignored by the leaderboard


def test_employee_monthly(api):
    seed_analytics()
    get = lambda code, month="2026-07": api.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    b = get("EMP1001").json()
    assert b == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23, "present_days": 1.5, "leave_days": 1,
                 "late_count": 2, "total_late_minutes": 50, "total_overtime_minutes": 40, "attendance_pct": 6.52}  # 1.5/23*100
    j = get("EMP1002").json()                                  # joined Wed 15 Jul: 13 working days
    assert (j["working_days"], j["present_days"], j["late_count"], j["total_late_minutes"], j["attendance_pct"]) == (13, 2.0, 1, 12, 15.38)
    n = get("EMP1003").json()                                  # no logs: zeros, not an error
    assert (n["working_days"], n["present_days"], n["late_count"], n["attendance_pct"]) == (23, 0, 0, 0.0)
    assert get("EMP1004").json()["attendance_pct"] is None and get("EMP1004").json()["working_days"] == 0   # not joined yet
    assert get("EMP0404").status_code == 404
    assert get("EMP1001", "2026-13").status_code == 422
    assert api.get("/analytics/employees/EMP1001/monthly").status_code == 422


def test_department_summary_headcount_and_averages(api):
    seed_analytics()
    items = {i["department"]: i for i in api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    assert list(items) == ["Eng", "Ops"]                       # sorted; 'Future' (headcount 0 in July) is omitted
    assert items["Eng"] == {"department": "Eng", "headcount": 3,           # 1001, 1002, 1003 (zero logs still counts); 1004 not joined
                            "present_days": 3.5,                           # 1 + 0.5 + 1 + 1 (Saturday record excluded)
                            "avg_work_hours": 7.72,                        # (9.08+3.5+8.0+9.0+9.0)/5 = 7.716 over RECORDS
                            "late_count": 3, "total_late_minutes": 62, "leave_count": 1, "on_duty_count": 1}
    assert items["Ops"] == {"department": "Ops", "headcount": 2, "present_days": 2.0, "avg_work_hours": 9.0,
                            "late_count": 2, "total_late_minutes": 60, "leave_count": 0, "on_duty_count": 0}
    only = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()["items"]
    assert [i["department"] for i in only] == ["Ops"]
    aug = {i["department"]: i for i in api.get("/analytics/departments/summary", params={"month": "2026-08"}).json()["items"]}
    assert aug["Eng"]["headcount"] == 4 and aug["Eng"]["present_days"] == 1.0 and aug["Eng"]["late_count"] == 1
    assert aug["Ops"]["avg_work_hours"] is None and aug["Ops"]["headcount"] == 2   # no records -> null, not 0
    assert api.get("/analytics/departments/summary").status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": "2026-7"}).status_code == 422


def test_late_leaderboard_competition_ranking_and_limit_after_ranking(api):
    seed_analytics()
    get = lambda **kw: api.get("/analytics/leaderboard/late", params={"month": "2026-07", **kw}).json()["items"]
    shape = lambda rows: [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in rows]
    full = [(1, "EMP1001", 50, 2), (2, "EMP2001", 30, 1), (2, "EMP2002", 30, 1), (4, "EMP1002", 12, 1)]   # ranks 1,2,2,4
    assert shape(get(limit=10)) == full                       # unknown employee EMP9999 (100 min) is ignored
    assert shape(get(limit=4)) == full
    assert shape(get(limit=3)) == full[:3]                    # rank 4 excluded, rank 3 does not exist
    assert shape(get(limit=2)) == full[:3]                    # tie at the cutoff: 3 rows although limit=2
    assert shape(get(limit=1)) == full[:1]
    assert get(limit=1)[0]["name"] == "Name EMP1001" and get(limit=1)[0]["department"] == "Eng"
    assert shape(get(department="Eng")) == [(1, "EMP1001", 50, 2), (2, "EMP1002", 12, 1)]   # ranked inside the department
    assert get(department="Nope") == []
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-02"}).json()["items"] == []


def trend(api, department, a, b):
    r = api.get(f"/analytics/departments/{department}/trend", params={"from": a, "to": b})
    return r


def test_department_trend_every_day_gap_filling_and_moving_average(api):
    seed_analytics()
    r = trend(api, "Eng", "2026-07-05", "2026-07-11")          # Sun .. Sat; headcount 2 throughout (1001 and 1003)
    assert r.status_code == 200
    got = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"])
           for i in r.json()["items"]]
    assert got == [
        ("2026-07-05", False, 2, 0, 0, None, None),            # Sunday: no rate, window holds no rate
        ("2026-07-06", True, 2, 1, 1, 0.5, 0.5),
        ("2026-07-07", True, 2, 0.5, 0, 0.25, 0.375),          # half day = 0.5
        ("2026-07-08", True, 2, 0, 0, 0.0, 0.25),              # LEAVE record: 0 present
        ("2026-07-09", True, 2, 0, 0, 0.0, 0.1875),            # ABSENT record
        ("2026-07-10", True, 2, 0, 0, 0.0, 0.15),              # weekday with NO record: rate 0.0, still a row
        ("2026-07-11", False, 2, 1, 1, None, 0.15),            # Saturday record counts as present_count; no rate
    ]
    assert r.json()["department"] == "Eng"


def test_department_trend_mid_range_joiner_and_short_first_window(api):
    seed_analytics()
    items = trend(api, "Eng", "2026-07-13", "2026-07-17").json()["items"]       # EMP1002 joins Wed 15th
    assert [(i["headcount"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [
        (2, 0, 0, 0.0, 0.0), (2, 0, 0, 0.0, 0.0), (3, 1, 0, 0.3333, 0.1111), (3, 1, 1, 0.3333, 0.1667), (3, 0, 0, 0.0, 0.1333)]


def test_department_trend_returns_every_day_when_no_event_falls_inside_the_range(api):
    """Regression: a range with no joiner and no log must still yield exactly one row per day."""
    seed_analytics()
    # 'Future' has one employee who joins on 2026-09-01: before that the department has headcount 0.
    items = trend(api, "Future", "2026-08-01", "2026-08-05").json()["items"]
    assert [i["date"] for i in items] == ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
    assert all(i["headcount"] == 0 and i["present_count"] == 0 and i["late_count"] == 0 for i in items)
    assert all(i["attendance_rate"] is None and i["moving_avg_7d"] is None for i in items)   # headcount 0 -> no rate
    assert [i["is_working_day"] for i in items] == [False, False, True, True, True]          # Sat, Sun, Mon-Wed


def test_department_trend_quiet_week_before_any_activity(api):
    """Regression: employees joined long before the range, but there is no log in it (Mon 2 Feb .. Sun 8 Feb 2026)."""
    seed_analytics()
    items = trend(api, "Eng", "2026-02-02", "2026-02-08").json()["items"]
    assert [i["date"] for i in items] == [f"2026-02-0{d}" for d in range(2, 9)]
    assert [i["headcount"] for i in items] == [2] * 7                           # EMP1001 and EMP1003 (joined January)
    assert [i["attendance_rate"] for i in items] == [0.0] * 5 + [None, None]    # working days with no record: 0.0
    assert [i["moving_avg_7d"] for i in items] == [0.0] * 7                     # window holds only the zero rates
    assert all(i["present_count"] == 0 and i["late_count"] == 0 for i in items)


def test_department_trend_range_rules(api):
    seed_analytics()
    one = trend(api, "Eng", "2026-07-06", "2026-07-06").json()["items"]
    assert len(one) == 1 and one[0]["moving_avg_7d"] == 0.5
    assert len(trend(api, "Eng", "2026-04-01", "2026-07-01").json()["items"]) == 92          # the maximum
    assert trend(api, "Eng", "2026-04-01", "2026-07-02").status_code == 422                   # 93 days
    assert trend(api, "Eng", "2026-07-02", "2026-07-01").status_code == 422
    assert trend(api, "Nope", "2026-07-01", "2026-07-02").status_code == 404
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422
    quiet = trend(api, "Ops", "2026-07-20", "2026-07-26").json()["items"]                    # whole week without records
    assert len(quiet) == 7 and [i["attendance_rate"] for i in quiet] == [0.0] * 5 + [None, None]
    future = trend(api, "Future", "2026-08-30", "2026-09-02").json()["items"]                # headcount 0 -> rate stays null
    assert [(i["headcount"], i["attendance_rate"]) for i in future] == [(0, None), (0, None), (1, 0.0), (1, 0.0)]


# =========================================================================== explain
EXPLAIN_CASES = {
    "attendance_list": {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31", "page_size": 5},
    "employee_monthly": {"emp_code": "EMP1001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "late_leaderboard": {"month": "2026-07", "limit": 3},
    "department_trend": {"department": "Eng", "from": "2026-07-05", "to": "2026-07-11"},
}


@pytest.mark.parametrize("endpoint", list(EXPLAIN_CASES))
def test_explain_uses_index_and_never_collection_scan(api, endpoint):
    seed_analytics()
    body = api.get(f"/admin/explain/{endpoint}", params=EXPLAIN_CASES[endpoint])
    assert body.status_code == 200, body.text
    out = body.json()
    assert out["endpoint"] == endpoint and out["collection"] in ("employees", "attendance_logs")
    text = body.text
    assert "IXSCAN" in text and "COLLSCAN" not in text
    assert "executionStats" in text or "executionStages" in text


def test_explain_attendance_list_has_same_filter_sort_and_page_as_the_endpoint(api):
    seed_analytics()
    params = {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2, "page_size": 2}
    real = api.get("/attendance", params=params).json()
    plan = api.get("/admin/explain/attendance_list", params=params).json()["explain"]
    assert plan["executionStats"]["nReturned"] == len(real["items"])             # same skip + limit applied
    text = str(plan["queryPlanner"]["parsedQuery"])
    assert "PRESENT" in text and "2026-07-01" in text and "2026-07-31" in text


def test_explain_every_variant_has_no_collscan(api):
    seed_analytics()
    for params in ({}, {"status": "WFH"}, {"emp_code": "EMP1001"}, {"date_from": "2026-07-01"}, {"status": "LEAVE", "date_to": "2026-07-31"}):
        text = api.get("/admin/explain/attendance_list", params=params).text
        assert "COLLSCAN" not in text and "IXSCAN" in text, params
    for dept in ({}, {"department": "Eng"}):
        text = api.get("/admin/explain/department_summary", params={"month": "2026-07", **dept}).text
        assert "COLLSCAN" not in text and "IXSCAN" in text, dept
    text = api.get("/admin/explain/late_leaderboard", params={"month": "2026-07", "department": "Eng"}).text
    assert "COLLSCAN" not in text and "IXSCAN" in text


def test_explain_validation(api):
    assert api.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/late_leaderboard").status_code == 422
    assert api.get("/admin/explain/not_an_endpoint").status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-07-02", "to": "2026-07-01"}).status_code == 422


@pytest.mark.skipif(not __import__("os").getenv("HRONE_LARGE"), reason="set HRONE_LARGE=1 to seed a large dataset")
def test_large_dataset_plans_and_speed(api):
    """HRONE_LARGE_EMP (default 500) employees x 200 weekdays: 500 -> 100,000 records, 100 -> 20,000.
    HRONE_MAX_SECONDS (default 3) is the per-request time limit; use a local MongoDB for meaningful timings."""
    import os
    import time
    from tests.seed_large import seed
    employees = int(os.getenv("HRONE_LARGE_EMP", "500"))
    limit = float(os.getenv("HRONE_MAX_SECONDS", "3"))
    assert seed(main.db, employees=employees, weekdays=200) == employees * 200
    main.ensure_indexes()
    cases = {**EXPLAIN_CASES, "department_trend": {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"},
             "department_summary": {"month": "2026-03"}, "late_leaderboard": {"month": "2026-03", "limit": 10}}
    for endpoint, params in cases.items():
        text = api.get(f"/admin/explain/{endpoint}", params=params).text
        assert "IXSCAN" in text and "COLLSCAN" not in text, endpoint
    urls = [("/attendance", {"page_size": 100}), ("/attendance", {"status": "WFH"}), ("/analytics/departments/summary", {"month": "2026-03"}),
            ("/analytics/leaderboard/late", {"month": "2026-03"}), ("/analytics/employees/EMP0001/monthly", {"month": "2026-03"}),
            ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"})]
    for path, params in urls:
        t0 = time.perf_counter()
        assert api.get(path, params=params).status_code == 200
        elapsed = time.perf_counter() - t0
        print(f"{path} {params} -> {elapsed:.2f}s")
        assert elapsed < limit, path