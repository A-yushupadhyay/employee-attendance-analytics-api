"""Pure unit tests of the business rules (R1-R5, R8). NO MongoDB needed:  pytest tests/test_rules.py

Expected values come from the contract text (openapi.yaml R1-R10) and from the nine sample
documents in DATA_MODEL.md, not from running the implementation and copying its output.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import main as m

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_SHIFT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


def late(punch_in, shift=DAY_SHIFT):
    day = m.attendance_date(punch_in, shift["shift_start"], shift["shift_end"])
    return m.compute_late_minutes(punch_in, day, shift["shift_start"], shift["shift_end"])


# ---- R2: lateness (examples are quoted from the contract) -------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after start: not late
    (9, 40, 1, 10),     # one second over: late, minutes counted from shift start
    (10, 15, 59, 45),   # floor of 45.98
    (9, 30, 0, 0),
    (9, 0, 0, 0),       # early
])
def test_late_minutes_contract_examples(h, mi, s, expected):
    assert late(ist(2026, 7, 6, h, mi, s)) == expected


def test_late_uses_ist_not_utc_date():
    # 00:10 IST on the 7th is 18:40 UTC on the 6th. The attendance date must be the IST date.
    p = ist(2026, 7, 7, 0, 10)
    assert m.attendance_date(p, "09:30", "18:30") == "2026-07-07"


# ---- R1: overnight shifts ----------------------------------------------------------------
def test_overnight_punch_in_before_shift_end_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 7, 1, 0), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"


def test_overnight_punch_in_at_or_after_shift_end_is_same_day():
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_overnight_late_is_measured_from_previous_days_shift_start():
    # 00:30 IST on the 7th belongs to the shift that started 22:00 on the 6th -> 150 minutes late.
    assert late(ist(2026, 7, 7, 0, 30), NIGHT_SHIFT) == 150


def test_overnight_early_arrival_is_not_late():
    assert late(ist(2026, 7, 6, 21, 55), NIGHT_SHIFT) == 0


def test_non_overnight_shift_never_moves_the_date():
    assert m.attendance_date(ist(2026, 7, 7, 1, 0), "09:30", "18:30") == "2026-07-07"


# ---- R3: overtime ------------------------------------------------------------------------
def ot(punch_out, day="2026-07-06", shift=DAY_SHIFT):
    return m.compute_overtime(punch_out, day, shift["shift_start"], shift["shift_end"])


def test_overtime_threshold_boundaries():
    assert ot(ist(2026, 7, 6, 18, 59, 59)) == 0    # 29:59 -> below the 30 minute minimum
    assert ot(ist(2026, 7, 6, 19, 0, 0)) == 30     # exactly 30
    assert ot(ist(2026, 7, 6, 19, 10, 59)) == 40   # floor of 40.98
    assert ot(ist(2026, 7, 6, 17, 0)) == 0         # leaving early


def test_overnight_overtime_is_measured_against_next_day_shift_end():
    # DATA_MODEL sample 7: out 06:40 IST next day, shift ends 06:00 -> 40
    assert ot(ist(2026, 7, 7, 6, 40), "2026-07-06", NIGHT_SHIFT) == 40


# ---- R4 / R5: work hours and half day ----------------------------------------------------
def hours(seconds):
    t0 = ist(2026, 7, 6, 9, 30)
    return m.compute_work_hours(t0, t0 + timedelta(seconds=seconds))


def test_work_hours_half_up_not_bankers():
    assert hours(18) == 0.01       # 0.005 h: banker's/float rounding gives 0.0
    assert hours(54) == 0.02       # 0.015 h
    assert hours(16182) == 4.5     # 4.495 h
    assert hours(16181) == 4.49


def test_half_day_uses_rounded_value():
    t0 = ist(2026, 7, 6, 9, 30)
    emp = {**DAY_SHIFT}

    def half(seconds):
        return m.derive_fields(emp, "PRESENT", "2026-07-06", t0, t0 + timedelta(seconds=seconds))["half_day"]

    assert half(16181) is True     # 4.4947 -> 4.49 < 4.50
    assert half(16182) is False    # 4.495  -> 4.50, NOT a half day
    assert half(16200) is False    # exactly 4.50


# ---- the nine samples from DATA_MODEL.md --------------------------------------------------
def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize("name,shift,pin,pout,exp", [
    ("sample 1 normal", DAY_SHIFT, utc(2026, 7, 6, 3, 58), utc(2026, 7, 6, 13, 5),
     dict(late_minutes=0, work_hours=9.12, overtime_minutes=0, half_day=False)),
    ("sample 2 late+overtime", DAY_SHIFT, utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40),
     dict(late_minutes=35, work_hours=9.08, overtime_minutes=40, half_day=False)),
    ("sample 4 half day", DAY_SHIFT, utc(2026, 7, 7, 4, 0), utc(2026, 7, 7, 7, 30),
     dict(late_minutes=0, work_hours=3.5, overtime_minutes=0, half_day=True)),
    ("sample 7 overnight", NIGHT_SHIFT, utc(2026, 7, 6, 16, 25), utc(2026, 7, 7, 1, 10),
     dict(late_minutes=0, work_hours=8.75, overtime_minutes=40, half_day=False)),
    ("sample 8 regularized", DAY_SHIFT, utc(2026, 7, 7, 3, 58), utc(2026, 7, 7, 13, 0),
     dict(late_minutes=0, work_hours=9.03, overtime_minutes=0, half_day=False)),
    ("sample 9 legacy", DAY_SHIFT, utc(2026, 7, 14, 4, 2), utc(2026, 7, 14, 13, 1),
     dict(late_minutes=0, work_hours=8.98, overtime_minutes=0, half_day=False)),
])
def test_derived_fields_match_data_model_samples(name, shift, pin, pout, exp):
    day = m.attendance_date(pin, shift["shift_start"], shift["shift_end"])
    assert m.derive_fields(shift, "PRESENT", day, pin, pout) == exp, name


def test_open_record_and_absent_defaults():
    t = ist(2026, 7, 7, 9, 35)
    assert m.derive_fields(DAY_SHIFT, "PRESENT", "2026-07-07", t, None) == dict(
        late_minutes=0, work_hours=None, overtime_minutes=0, half_day=False)
    assert m.derive_fields(DAY_SHIFT, "ABSENT", "2026-07-08", None, None) == dict(
        late_minutes=0, work_hours=None, overtime_minutes=0, half_day=False)


# ---- instants: epoch ms <-> datetime -----------------------------------------------------
def test_ms_conversion_truncates_to_whole_seconds_and_round_trips():
    assert m.dt_to_ms(m.ms_to_dt(1783312500999)) == 1783312500000  # 09:40:00.900 counts as 09:40:00
    assert m.ms_to_dt(1783312500000).tzinfo is not None
    naive = datetime(2026, 7, 6, 4, 35)  # a naive datetime is treated as UTC
    assert m.dt_to_ms(naive) == m.dt_to_ms(naive.replace(tzinfo=UTC))


def test_history_entry_dates_are_converted_inside_nested_changes():
    entry = {"at": utc(2026, 7, 8, 5, 0), "by": "hr", "reason": "glitch", "changes": {
        "punch_in": {"from": utc(2026, 7, 7, 4, 35), "to": utc(2026, 7, 7, 3, 58)},
        "late_minutes": {"from": 35, "to": 0},
        "punch_out": {"from": None, "to": utc(2026, 7, 7, 13, 0)}}}
    out = m.serialize_history_entry(entry)
    assert out["at"] == m.dt_to_ms(entry["at"])
    assert out["changes"]["punch_in"] == {"from": m.dt_to_ms(utc(2026, 7, 7, 4, 35)), "to": m.dt_to_ms(utc(2026, 7, 7, 3, 58))}
    assert out["changes"]["punch_out"]["from"] is None
    assert out["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_legacy_record_without_history_or_half_day_serializes():
    doc = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT", "punch_in": utc(2026, 7, 14, 4, 2),
           "punch_out": utc(2026, 7, 14, 13, 1), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}
    out = m.serialize_attendance(doc)
    assert out["history"] == [] and out["half_day"] is False and "_id" not in out


# ---- R8 helpers and working days (R7) ----------------------------------------------------
def test_round_half_up_helpers():
    assert m.round_half_up(1, 32, 4) == 0.0313   # 0.03125 exactly: half-up, banker's would give 0.0312
    assert m.round_half_up(2, 3, 4) == 0.6667
    assert m.round_half_up(15, 100, 1) == 0.2
    assert m.half_up_int(5, 2) == 3 and m.half_up_int(7, 3) == 2


def test_working_days_july_2026_and_mid_month_joiner():
    first, last = m.month_bounds("2026-07")
    assert m.count_working_days(first, last, "2026-01-05") == 23   # Wed 1 .. Fri 31
    assert m.count_working_days(first, last, "2026-07-15") == 13   # from Wed 15
    assert m.count_working_days(first, last, "2026-08-01") == 0    # not yet joined
    assert m.count_working_days(first, last, "2026-07-11") == 15   # joined on a Saturday -> from Mon 13


def test_month_bounds_and_invalid_inputs():
    assert m.month_bounds("2024-02")[1].day == 29
    with pytest.raises(Exception):
        m.month_bounds("0000-01")
    with pytest.raises(Exception):
        m.parse_date_param("2026-02-30", "date")
    with pytest.raises(Exception):
        m.parse_date_param("20260706", "date")


# ---- request validation ------------------------------------------------------------------
@pytest.mark.parametrize("bad", [1783312500, 1783312500.0, "1783312500000", True, 5_000_000_000_000, -5])
def test_punched_at_rejects_seconds_floats_strings_bools_and_out_of_range(bad):
    with pytest.raises(ValidationError):
        m.PunchInIn(emp_code="EMP0001", punched_at=bad)


def test_punch_in_status_only_accepts_presence_values():
    with pytest.raises(ValidationError):
        m.PunchInIn(emp_code="EMP0001", status="ABSENT")
    assert m.PunchInIn(emp_code="EMP0001").status == "PRESENT"


def test_client_sent_derived_fields_are_ignored():
    body = m.PunchInIn(emp_code="EMP0001", late_minutes=99, work_hours=8)
    assert not hasattr(body, "late_minutes")


def test_employee_validation():
    ok = dict(emp_code="EMP0001", name="A", email="a@b.co", department="Eng", joined_on="2026-01-05")
    assert m.EmployeeIn(**ok).shift_start == "09:30"
    for patch in ({"emp_code": "E1"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"joined_on": "2026-13-01"},
                  {"shift_start": "9:30"}, {"shift_start": "09:30", "shift_end": "09:30"}, {"name": ""}):
        with pytest.raises(ValidationError):
            m.EmployeeIn(**{**ok, **patch})


def test_regularize_rejects_explicit_null_and_short_reason():
    with pytest.raises(ValidationError):
        m.RegularizeIn(reason="valid reason", regularized_by="hr", punch_in=None)
    with pytest.raises(ValidationError):
        m.RegularizeIn(reason="no", regularized_by="hr")


# ---- regression: trend must not depend on an employee-join or log event existing inside the range ----
def test_trend_pipeline_always_contains_a_seed_event_for_the_first_day():
    pipe = m.trend_pipeline("Future", date(2026, 8, 1), date(2026, 8, 5))
    concat = next(st["$project"]["events"]["$concatArrays"] for st in pipe if "$project" in st and "events" in st["$project"])
    assert concat[0] == [{"date": "2026-08-01", "hc": 0, "p2": 0, "late": 0}]