"""Adds sample documents from sample_data/ to your database without replacing existing records.
Usage:  python sample_seed.py            (reads MONGO_URI / MONGO_DB from the environment or .env)

Existing documents are left unchanged; matching employee codes or attendance dates are skipped.
The sample records demonstrate useful API states. Run pytest for the full edge-case suite."""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

try:
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)

    employee_docs = json_util.loads((here / "employees.json").read_text(), json_options=opts)
    attendance_docs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)

    def add_missing(name, docs, key_fields):
        added = 0
        for doc in docs:
            identity = {field: doc[field] for field in key_fields}
            result = db[name].update_one(identity, {"$setOnInsert": doc}, upsert=True)
            if result.upserted_id is not None:
                added += 1
        print(f"added {added:>2} new documents to '{db.name}.{name}' ({len(docs) - added} already existed)")

    add_missing("employees", employee_docs, ("emp_code",))

    employee_codes = set(db.employees.distinct("emp_code"))
    missing_codes = sorted({doc["emp_code"] for doc in attendance_docs} - employee_codes)
    if missing_codes:
        raise ValueError(f"attendance samples reference missing employees: {', '.join(missing_codes)}")

    add_missing("attendance_logs", attendance_docs, ("emp_code", "date"))
finally:
    db.client.close()
