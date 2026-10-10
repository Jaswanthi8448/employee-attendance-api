"""Integration tests against a real MongoDB (MONGO_URI, default localhost). Uses a throwaway database."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

os.environ["MONGO_DB"] = "attendance_pytest"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ms(year, month, day, hour=0, minute=0, second=0, millis=0):
    """IST wall-clock time -> epoch ms."""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + millis


def employee(code, dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
    return {"emp_code": code, "name": code, "email": f"{code.lower()}@x.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined}


def log(code, day, status="PRESENT", late=0, overtime=0, work_hours=9.0, half_day=False):
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    punch = datetime.fromisoformat(day).replace(hour=4, tzinfo=timezone.utc) if present else None
    return {"emp_code": code, "date": day, "status": status, "punch_in": punch, "punch_out": punch,
            "work_hours": work_hours if present else None, "late_minutes": late, "overtime_minutes": overtime,
            "half_day": half_day, "history": []}


@pytest.fixture()
def client():
    main.client.drop_database("attendance_pytest")
    with TestClient(main.app) as test_client:
        yield test_client
    main.client.drop_database("attendance_pytest")


def create(client, *codes, **kwargs):
    for code in codes:
        assert client.post("/employees", json=employee(code, **kwargs)).status_code == 201


# --------------------------------------------------------------------------- employees
def test_create_employee_returns_epoch_ms_and_rejects_duplicates(client):
    res = client.post("/employees", json=employee("EMP0001"))
    assert res.status_code == 201
    body = res.json()
    assert isinstance(body["created_at"], int) and "_id" not in body
    assert client.post("/employees", json=employee("EMP0001")).status_code == 409


@pytest.mark.parametrize("override", [
    {"emp_code": "E1"}, {"email": "nope"}, {"shift_start": "25:00"}, {"joined_on": "2026-02-30"},
    {"shift_start": "09:30", "shift_end": "09:30"}, {"name": ""},
])
def test_create_employee_validation(client, override):
    assert client.post("/employees", json={**employee("EMP0001"), **override}).status_code == 422


def test_concurrent_employee_creates_yield_one_201(client):
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: client.post("/employees", json=employee("EMP0009")).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


def test_list_employees_pagination_filter_and_sort(client):
    for code in ("EMP0003", "EMP0001", "EMP0002"):
        create(client, code, dept="Sales" if code != "EMP0002" else "Ops")
    res = client.get("/employees", params={"department": "Sales", "page": 1, "page_size": 1}).json()
    assert res["total"] == 2 and [e["emp_code"] for e in res["items"]] == ["EMP0001"]
    page2 = client.get("/employees", params={"department": "Sales", "page": 2, "page_size": 1}).json()
    assert [e["emp_code"] for e in page2["items"]] == ["EMP0003"]
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- punch-in (R1, R2)
@pytest.mark.parametrize("hhmmss,millis,expected", [
    ((9, 40, 0), 0, 0), ((9, 40, 0), 900, 0), ((9, 40, 1), 0, 10), ((10, 15, 59), 0, 45), ((9, 28, 0), 0, 0),
])
def test_late_minutes_rule(client, hhmmss, millis, expected):
    create(client, "EMP0001")
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, *hhmmss, millis)})
    assert res.status_code == 201
    body = res.json()
    assert body["late_minutes"] == expected and body["date"] == "2026-07-06"
    assert body["punch_in"] % 1000 == 0  # truncated to whole seconds
    assert body["history"] == [] and body["work_hours"] is None and body["half_day"] is False


def test_punch_in_date_uses_ist_not_utc(client):
    create(client, "EMP0001")
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 1, 0)}).json()
    assert body["date"] == "2026-07-07"  # 2026-07-06 in UTC


def test_overnight_shift_punch_in_after_midnight_belongs_to_previous_day(client):
    create(client, "EMP0005", start="22:00", end="06:00")
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 0, 30)}).json()
    assert body["date"] == "2026-07-06" and body["late_minutes"] == 150


@pytest.mark.parametrize("payload", [
    {"emp_code": "EMP0001", "punched_at": 1783312500},  # seconds
    {"emp_code": "EMP0001", "punched_at": 1783312500000.0},  # float
    {"emp_code": "EMP0001", "punched_at": "1783312500000"},  # string
    {"emp_code": "EMP0001", "punched_at": 9_999_999_999_999},  # out of range
    {"emp_code": "EMP0001", "status": "ABSENT"},
])
def test_punch_in_validation(client, payload):
    create(client, "EMP0001")
    assert client.post("/attendance/punch-in", json=payload).status_code == 422


def test_punch_in_unknown_employee_is_404(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_concurrent_punch_ins_yield_one_201(client):
    create(client, "EMP0001")
    payload = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-in", json=payload).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# --------------------------------------------------------------------------- punch-out (R3 - R5)
def punch(client, code, start, end=None):
    assert client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": start}).status_code == 201
    if end is not None:
        return client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": end})
    return None


def test_punch_out_computes_hours_overtime_half_day(client):
    create(client, "EMP0001", "EMP0002", "EMP0003")
    full = punch(client, "EMP0001", ms(2026, 7, 6, 9, 28), ms(2026, 7, 6, 18, 35)).json()
    assert (full["work_hours"], full["overtime_minutes"], full["half_day"]) == (9.12, 0, False)
    overtime = punch(client, "EMP0002", ms(2026, 7, 6, 10, 5), ms(2026, 7, 6, 19, 0, 59)).json()
    assert overtime["overtime_minutes"] == 30 and overtime["late_minutes"] == 35
    half = punch(client, "EMP0003", ms(2026, 7, 6, 9, 0), ms(2026, 7, 6, 13, 29, 42)).json()
    assert half["work_hours"] == 4.5 and half["half_day"] is False  # 4.495 h rounds half-up to 4.50


def test_overnight_punch_out_across_midnight(client):
    create(client, "EMP0005", start="22:00", end="06:00")
    body = punch(client, "EMP0005", ms(2026, 7, 6, 21, 55), ms(2026, 7, 7, 6, 40)).json()
    assert body["date"] == "2026-07-06"
    assert (body["work_hours"], body["overtime_minutes"]) == (8.75, 40)


def test_punch_out_errors(client):
    create(client, "EMP0001")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    start = ms(2026, 7, 6, 9, 0)
    assert punch(client, "EMP0001", start, start + 25 * 3600 * 1000).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": start}).status_code == 422
    ok = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": start + 3600_000})
    assert ok.status_code == 200
    again = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": start + 7200_000})
    assert again.status_code == 409


def test_concurrent_punch_outs_yield_one_200(client):
    create(client, "EMP0001")
    punch(client, "EMP0001", ms(2026, 7, 6, 9, 0))
    payload = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-out", json=payload).status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


# --------------------------------------------------------------------------- list
def test_list_attendance_sort_filters_and_validation(client):
    main.logs_col.insert_many([log("EMP0002", "2026-07-06"), log("EMP0001", "2026-07-06"),
                               log("EMP0001", "2026-07-07", status="LEAVE"), log("EMP0001", "2026-07-01")])
    items = client.get("/attendance").json()["items"]
    assert [(i["date"], i["emp_code"]) for i in items] == [
        ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"), ("2026-07-01", "EMP0001")]
    filtered = client.get("/attendance", params={"date_from": "2026-07-02", "status": "PRESENT"}).json()
    assert filtered["total"] == 2 and "id" not in filtered["items"][0]
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "07/01/2026"}).status_code == 422
    assert client.get("/attendance", params={"status": "LATE"}).status_code == 422


def test_legacy_record_defaults(client):
    doc = log("EMP0002", "2026-07-14")
    del doc["history"], doc["half_day"]
    main.logs_col.insert_one(doc)
    item = client.get("/attendance").json()["items"][0]
    assert item["history"] == [] and item["half_day"] is False


# --------------------------------------------------------------------------- regularize
def test_regularize_recomputes_and_appends_history(client):
    create(client, "EMP0001")
    punch(client, "EMP0001", ms(2026, 7, 7, 10, 5), ms(2026, 7, 7, 18, 30))
    res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert res.status_code == 200
    body = res.json()
    assert body["late_minutes"] == 0 and body["work_hours"] == 9.03
    (entry,) = body["history"]
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert entry["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert isinstance(entry["at"], int) and entry["by"] == "hr.admin"

    second = client.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "LEAVE", "reason": "was on leave", "regularized_by": "hr.admin"}).json()
    assert len(second["history"]) == 2 and second["punch_in"] is None and second["work_hours"] is None


def test_regularize_rules(client):
    create(client, "EMP0001")
    punch(client, "EMP0001", ms(2026, 7, 7, 9, 0), ms(2026, 7, 7, 18, 0))
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "fixing it", "regularized_by": "hr"}
    assert client.patch(url, json={**base, "status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert client.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422  # other day
    assert client.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 8, 0)}).status_code == 422  # before in
    assert client.patch(url, json={**base, "status": "PRESENT"}).status_code == 422  # changes nothing
    assert client.patch(url, json={**base, "reason": "x"}).status_code == 422
    assert client.patch(url, json={**base, "punch_in": None}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-08", json={**base, "status": "WFH"}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-07-07", json={**base, "status": "WFH"}).status_code == 404
    main.logs_col.insert_one(log("EMP0001", "2026-07-08", status="ABSENT"))
    assert client.patch("/attendance/EMP0001/2026-07-08", json={**base, "status": "WFH"}).status_code == 422


def test_concurrent_regularizations_never_lose_history(client):
    create(client, "EMP0001")
    punch(client, "EMP0001", ms(2026, 7, 7, 9, 0), ms(2026, 7, 7, 18, 0))
    bodies = [{"punch_in": ms(2026, 7, 7, 9, i), "reason": f"fix {i:03d}", "regularized_by": "hr"} for i in range(1, 9)]
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda b: client.patch("/attendance/EMP0001/2026-07-07", json=b).status_code, bodies))
    stored = main.logs_col.find_one({"emp_code": "EMP0001"})
    assert codes.count(200) == len(stored["history"]) >= 1
    assert set(codes) <= {200, 409}


# --------------------------------------------------------------------------- analytics
def seed_july(client):
    """July 2026: 1st is a Wednesday, 23 working days."""
    create(client, "EMP0001", "EMP0003")  # Engineering
    create(client, "EMP0002", joined="2026-07-13")  # mid-month joiner
    create(client, "EMP0004", joined="2026-08-03")  # joins after July
    create(client, "EMP0010", dept="Sales")
    main.logs_col.insert_many([
        log("EMP0001", "2026-07-06", late=30, overtime=40, work_hours=9.12),
        log("EMP0001", "2026-07-07", status="WFH", work_hours=3.5, half_day=True),
        log("EMP0001", "2026-07-11", late=20),  # Saturday: late total only
        log("EMP0001", "2026-07-16", status="LEAVE"),
        log("EMP0001", "2026-07-21", status="ABSENT"),
        log("EMP0001", "2026-06-30", late=99),  # outside the month
        log("EMP0002", "2026-07-14", late=50, work_hours=9.13),
        log("EMP0010", "2026-07-06", status="ON_DUTY", late=50),
        log("EMP0099", "2026-07-06", late=500),  # orphan log, employee does not exist
    ])


def test_employee_monthly(client):
    seed_july(client)
    body = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert body == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 1.5,
                    "leave_days": 1, "late_count": 2, "total_late_minutes": 50, "total_overtime_minutes": 40,
                    "attendance_pct": 6.52}
    joiner = client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert joiner["working_days"] == 15
    future = client.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(client):
    seed_july(client)
    items = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [i["department"] for i in items] == ["Engineering", "Sales"]
    eng = items[0]
    assert eng["headcount"] == 3  # EMP0003 has no logs but counts; EMP0004 joins later
    assert eng["present_days"] == 2.5  # Saturday record excluded from present days
    assert eng["avg_work_hours"] == 7.69  # (9.12 + 3.5 + 9.0 + 9.13) / 4 = 7.6875, over records
    assert (eng["late_count"], eng["total_late_minutes"], eng["leave_count"], eng["on_duty_count"]) == (3, 100, 1, 0)
    sales = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert sales["items"][0]["on_duty_count"] == 1 and sales["items"][0]["headcount"] == 1


def test_leaderboard_ties_and_limit(client):
    create(client, "EMP0001", "EMP0002", "EMP0003", "EMP0004")
    main.logs_col.insert_many([
        log("EMP0003", "2026-07-06", late=120), log("EMP0002", "2026-07-06", late=90),
        log("EMP0001", "2026-07-06", late=60), log("EMP0001", "2026-07-07", late=30),
        log("EMP0004", "2026-07-06", late=60), log("EMP0099", "2026-07-06", late=999)])
    items = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0003"), (2, "EMP0001"), (2, "EMP0002")]
    assert items[1]["late_count"] == 2 and items[1]["name"] == "EMP0001"
    full = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]
    assert [i["rank"] for i in full] == [1, 2, 2, 4]


def test_department_trend(client):
    create(client, "EMP0001", "EMP0002")
    create(client, "EMP0003", joined="2026-07-08")
    main.logs_col.insert_many([
        log("EMP0001", "2026-07-03"), log("EMP0002", "2026-07-03", half_day=True, late=15),
        log("EMP0001", "2026-07-04"),  # Saturday
        log("EMP0001", "2026-07-08"), log("EMP0002", "2026-07-08"), log("EMP0003", "2026-07-08")])
    res = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-02", "to": "2026-07-08"})
    rows = res.json()["items"]
    assert [r["date"] for r in rows] == [f"2026-07-0{d}" for d in range(2, 9)]
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-07-02"]["attendance_rate"] == 0.0  # working day, no records
    assert by_date["2026-07-03"]["present_count"] == 1.5 and by_date["2026-07-03"]["attendance_rate"] == 0.75
    assert by_date["2026-07-04"]["attendance_rate"] is None and by_date["2026-07-04"]["present_count"] == 1
    assert by_date["2026-07-08"]["headcount"] == 3 and by_date["2026-07-07"]["headcount"] == 2
    assert by_date["2026-07-02"]["moving_avg_7d"] == 0.0
    assert by_date["2026-07-03"]["moving_avg_7d"] == 0.375
    assert by_date["2026-07-08"]["moving_avg_7d"] == round((0 + 0.75 + 0 + 0 + 1) / 5, 4)
    assert client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-02", "to": "2026-07-08"}).status_code == 404
    assert client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-07-08", "to": "2026-07-02"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200  # 92 days
    assert client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422  # 93 days
    empty = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-09-01", "to": "2026-09-03"})
    assert [r["attendance_rate"] for r in empty.json()["items"]] == [0.0, 0.0, 0.0]


# --------------------------------------------------------------------------- explain
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"status": "LEAVE"}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(client, endpoint, params):
    seed_july(client)
    res = client.get(f"/admin/explain/{endpoint}", params=params)
    assert res.status_code == 200
    text = str(res.json()["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_requires_params(client):
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422
