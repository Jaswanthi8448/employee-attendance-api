# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, HR corrections with an audit trail, and analytics built on aggregation
pipelines. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested on 7.0).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB; real env vars take priority
python sample_seed.py             # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```

Interactive docs: http://localhost:8000/docs. Indexes are created at startup and need no manual step.

## Tests

The integration tests run against a real MongoDB, using a throwaway `attendance_pytest` database:

```bash
pip install -r requirements-dev.txt
pytest -q
```

They cover R1-R5 edge cases (09:40:00 vs 09:40:01, millisecond truncation, half-up rounding at 4.495 h, overnight
shifts), races (parallel creates, punch-ins, punch-outs, corrections), ties, zero-log headcount, weekends, mid-month
joiners, the gap-filled trend, and the explain plans (`IXSCAN`, no `COLLSCAN`).

## Notes and interpretations

- **Trend range:** "exceeds 92 days" is read as more than 92 calendar days inclusive (92 rows are allowed, 93 are 422).
- **Moving average:** the mean of the already rounded (4-decimal) `attendance_rate` values in the window. The rounding
  is half-up inside MongoDB, because `$round` rounds half-to-even.
- **Punch-out** closes the latest record by attendance `date` whose `punch_in <= punched_at`. If that record is already
  closed, the result is 409.
- **Corrections** use optimistic concurrency: the update only applies if status, punch times and history length are
  unchanged since the read. Otherwise the loser gets 409, so no history entry is lost.
- MongoDB errors are returned as 503 rather than 500.
