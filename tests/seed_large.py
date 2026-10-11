"""Seeds ~100,000 attendance records straight into MongoDB (like the grader does) to check query plans/speed.

    python tests/seed_large.py            # uses MONGO_URI / MONGO_DB (default database: attendance_db)
Creates NO indexes: start the API afterwards and it builds them at startup.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

IST = timezone(timedelta(hours=5, minutes=30))
DEPTS = ["Engineering", "Sales", "Support", "Finance", "HR"]


def seed(db, employees=500, weekdays=200, seed_value=7):
    rnd = random.Random(seed_value)
    db.employees.drop()
    db.attendance_logs.drop()
    emps = []
    for i in range(employees):
        emps.append({"emp_code": f"EMP{i + 1:04d}", "name": f"Employee {i + 1}", "email": f"e{i + 1}@example.com",
                     "department": DEPTS[i % len(DEPTS)], "shift_start": "09:30", "shift_end": "18:30",
                     "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)})
    db.employees.insert_many(emps)
    days, d = [], date(2026, 1, 5)
    while len(days) < weekdays:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    batch, total = [], 0
    for e in emps:
        for day in days:
            late = rnd.choice([0, 0, 0, 0, 12, 25, 40])
            start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST) + timedelta(minutes=late)
            hours = round(rnd.uniform(4, 10), 2)
            batch.append({"emp_code": e["emp_code"], "date": day.isoformat(), "status": rnd.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY"]),
                          "punch_in": start.astimezone(timezone.utc), "punch_out": (start + timedelta(hours=hours)).astimezone(timezone.utc),
                          "work_hours": hours, "late_minutes": late if late > 10 else 0, "overtime_minutes": 0,
                          "half_day": hours < 4.5, "history": []})
            if len(batch) == 5000:
                db.attendance_logs.insert_many(batch)
                total += len(batch)
                batch = []
    if batch:
        db.attendance_logs.insert_many(batch)
        total += len(batch)
    return total


if __name__ == "__main__":
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
    database = client[os.getenv("MONGO_DB", "attendance_db")]
    print("seeded", seed(database), "attendance records into", database.name)
