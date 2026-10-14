# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance, punch-in/out business rules, HR corrections with an audit trail,
and analytics built with MongoDB aggregation pipelines. A Streamlit UI provides a demo interface for non-technical
users.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (tested with MongoDB 7.0), running locally or available through MongoDB Atlas
- Git

## 1. Clone the repository

Replace `<your-repository-url>` with the repository URL:

```powershell
git clone <your-repository-url>
cd employee-attendance-api
```

Run the following commands from the `employee-attendance-api` project directory, where `requirements.txt` and
`streamlit_app.py` are located.

## 2. Create and activate a virtual environment

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

If PowerShell blocks environment activation, either activate for the current terminal and retry:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Or use the virtual environment's Python executable directly without activation:

```powershell
.\.venv\Scripts\python.exe --version
```

### macOS / Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
```

## 3. Install dependencies

With the virtual environment active:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This installs the API, MongoDB driver, Streamlit interface, and related dependencies.

## 4. Configure MongoDB

Make sure MongoDB is running and reachable. By default, the application connects to a local MongoDB server at
`mongodb://localhost:27017` and uses the `attendance_db` database.

To configure another MongoDB server, create a `.env` file from the example.

### Windows PowerShell

```powershell
Copy-Item .env.example .env
notepad .env
```

### macOS / Linux

```bash
cp .env.example .env
```

Set the connection details in `.env`:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas, set `MONGO_URI` to your Atlas connection string and ensure your database user and IP access
settings are configured in Atlas. Do not commit `.env` or share a connection string containing credentials.
Environment variables set outside `.env` take precedence.

The API creates database indexes at startup. Attendance records refer to employees using `emp_code`. MongoDB does
not enforce foreign keys across collections; the API checks employee existence for attendance operations, and the
sample loader rejects attendance samples whose employee does not exist.

## 5. (Optional) Load sample data

From the project directory, with the virtual environment active:

```powershell
python sample_seed.py
```

This adds sample employees and attendance records to the database configured by `MONGO_URI` and `MONGO_DB`. It
does not delete or overwrite existing records. Existing employee codes and employee/date attendance pairs are
skipped. To use a fresh database, set a new database name in `.env`.

## 6. Start the FastAPI server

Open a terminal in the project directory with the virtual environment active:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Keep this terminal running. Check that the API and database are available:

- Health check: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

The API uses the database configured in `.env`, or the local defaults if no configuration is provided.

## 7. Start the Streamlit demo UI

Open a **second terminal** in the same project directory and activate the same virtual environment.

### Windows PowerShell

```powershell
.\.venv\Scripts\Activate.ps1
python -m streamlit run streamlit_app.py
```

### macOS / Linux

```bash
source .venv/bin/activate
python -m streamlit run streamlit_app.py
```

Open the URL printed by Streamlit, normally <http://localhost:8501>. The sidebar's **Attendance service address**
should be `http://127.0.0.1:8000` when the API is running locally.

The UI includes:

- Overview dashboard and recent attendance
- Employee creation and directory
- Punch-in and punch-out
- Attendance filtering and CSV download
- Attendance corrections with audit history
- Department analytics, late-arrival leaderboard, and individual monthly summaries

Users can select a display time zone, date format, and 12- or 24-hour clock. The UI formats dates and times for
people, then converts punch times to the API's epoch-millisecond format when sending requests. Attendance workday
assignment continues to follow the API's IST business-day rules.

## 8. Run the tests

The integration tests require a running MongoDB. They use and clean up a separate `attendance_pytest` database;
they do not use the configured `attendance_db` database.

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run all tests:

```powershell
python -m pytest -q
```

The suite covers request validation, attendance business-rule edge cases, concurrent requests, analytics, and
database query plans.

## Data collections

- `employees`: employee profiles, department, work shift, joining date, and profile creation time.
- `attendance_logs`: one attendance record per employee and attendance date, with status, punch times, calculated
  work values, and any correction history.

The collections are associated by `emp_code`. To fetch records with employee details in MongoDB, use `$lookup`
between `attendance_logs.emp_code` and `employees.emp_code`.

## Design notes

- **Trend range:** "exceeds 92 days" is interpreted as more than 92 calendar days inclusive (92 rows are allowed,
  93 are rejected).
- **Moving average:** the mean of the already rounded (4-decimal) `attendance_rate` values in the window. The
  pipeline uses an explicit half-up rounding expression because MongoDB `$round` uses half-to-even.
- **Punch-out:** closes the latest record by attendance date whose `punch_in <= punched_at`. If that record is
  already closed, the request returns 409.
- **Corrections:** optimistic concurrency ensures a correction applies only if the record has not changed since it
  was read. This prevents concurrent edits from losing audit history.
- **Database errors:** MongoDB errors are returned as 503 rather than 500.

See [DECISIONS.md](./DECISIONS.md) for implementation choices and [REVIEW.md](./REVIEW.md) for the starter-code
review.
