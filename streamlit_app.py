"""Streamlit demonstration UI for the Employee Attendance API."""
import csv
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import requests
import streamlit as st


IST = ZoneInfo("Asia/Kolkata")
STATUSES = ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
STATUS_LABELS = {
    "PRESENT": "Present · worked on site",
    "WFH": "Work from home",
    "ON_DUTY": "On duty · working off site",
    "ABSENT": "Absent",
    "LEAVE": "On leave",
}
DATE_FORMATS = {
    "Day / month / year (14 Oct 2026)": "%d %b %Y",
    "Day / month / year (14/10/2026)": "%d/%m/%Y",
    "Month / day / year (10/14/2026)": "%m/%d/%Y",
    "Year / month / day (2026-10-14)": "%Y-%m-%d",
}
TIMEZONES = sorted(available_timezones())

st.set_page_config(page_title="HRone | Attendance", page_icon="🗓️", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.8rem; max-width: 1440px; }
      [data-testid="stMetric"] {
        background: linear-gradient(135deg, #f5f8ff, #ffffff);
        border: 1px solid #e7ebf3; padding: 16px 18px; border-radius: 14px;
      }
      div[data-testid="stForm"] {
        border: 1px solid #e7ebf3; border-radius: 14px; padding: 18px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


class ApiError(Exception):
    """An API request failed with a useful detail for the UI."""


def api_request(method: str, path: str, *, params=None, payload=None):
    base_url = st.session_state.api_url.rstrip("/")
    try:
        response = requests.request(
            method, f"{base_url}{path}", params=params, json=payload, timeout=8
        )
    except requests.RequestException as exc:
        raise ApiError(
            f"Cannot reach the API at {base_url}. Start it with "
            "`python -m uvicorn app.main:app --reload --port 8000`."
        ) from exc

    if not response.ok:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if isinstance(detail, list):
            detail = "; ".join(item.get("msg", str(item)) for item in detail)
        raise ApiError(f"API returned {response.status_code}: {detail}")

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def fetch_employees():
    return api_request("GET", "/employees", params={"page": 1, "page_size": 100})


def format_date(value, date_format):
    if not value:
        return "—"
    parsed = value if hasattr(value, "strftime") else datetime.strptime(value, "%Y-%m-%d")
    return parsed.strftime(date_format)


def format_datetime(epoch_ms, display_timezone, date_format, clock_format):
    if epoch_ms is None:
        return "—"
    local_dt = datetime.fromtimestamp(epoch_ms / 1000, display_timezone)
    clock = "%I:%M %p" if clock_format == "12-hour" else "%H:%M"
    return f"{local_dt.strftime(date_format)}, {local_dt.strftime(clock)} {local_dt.tzname()}"


def to_epoch_ms(local_dt: datetime, display_timezone: ZoneInfo) -> int:
    """Convert a time selected in the user's display timezone to API epoch milliseconds."""
    return int(local_dt.replace(tzinfo=display_timezone).timestamp() * 1000)


def local_input_time(epoch_ms, display_timezone):
    if epoch_ms is None:
        return datetime.now(display_timezone).replace(tzinfo=None)
    return datetime.fromtimestamp(epoch_ms / 1000, display_timezone).replace(tzinfo=None)


def display_logs(logs, display_timezone, date_format, clock_format):
    rows = []
    for log in logs:
        rows.append(
            {
                "Attendance date": format_date(log["date"], date_format),
                "Employee code": log["emp_code"],
                "Attendance status": STATUS_LABELS[log["status"]],
                "Punch-in time": format_datetime(
                    log.get("punch_in"), display_timezone, date_format, clock_format
                ),
                "Punch-out time": format_datetime(
                    log.get("punch_out"), display_timezone, date_format, clock_format
                ),
                "Hours worked": "—" if log.get("work_hours") is None else f"{log['work_hours']:.2f}",
                "Minutes late": log.get("late_minutes", 0),
                "Overtime minutes": log.get("overtime_minutes", 0),
                "Half day": "Yes" if log.get("half_day", False) else "No",
                "Correction entries": len(log.get("history") or []),
            }
        )
    return rows


def display_employee(employee, date_format, display_timezone, clock_format):
    return {
        "Employee code": employee["emp_code"],
        "Name": employee["name"],
        "Work email": employee["email"],
        "Department": employee["department"],
        "Shift hours": f"{employee['shift_start']}–{employee['shift_end']}",
        "Joining date": format_date(employee["joined_on"], date_format),
        "Profile created": format_datetime(
            employee.get("created_at"), display_timezone, date_format, clock_format
        ),
    }


def display_change_value(field, value, display_timezone, date_format, clock_format):
    if field in ("punch_in", "punch_out", "at", "created_at"):
        return format_datetime(value, display_timezone, date_format, clock_format)
    if field == "date":
        return format_date(value, date_format)
    if field == "status" and value in STATUS_LABELS:
        return STATUS_LABELS[value]
    if value is None:
        return "—"
    return value


def show_corrected_record(record, display_timezone, date_format, clock_format):
    st.success("Attendance corrected and audit history recorded.")
    st.write(
        f"**Employee:** {record['emp_code']} · **Attendance date:** "
        f"{format_date(record['date'], date_format)} · "
        f"**Status:** {STATUS_LABELS[record['status']]}"
    )
    metrics = st.columns(4)
    metrics[0].metric(
        "Hours worked", "—" if record.get("work_hours") is None else f"{record['work_hours']:.2f}"
    )
    metrics[1].metric("Minutes late", record.get("late_minutes", 0))
    metrics[2].metric("Overtime minutes", record.get("overtime_minutes", 0))
    metrics[3].metric("Half day", "Yes" if record.get("half_day") else "No")
    for index, entry in enumerate(record.get("history") or [], start=1):
        changed_at = format_datetime(entry.get("at"), display_timezone, date_format, clock_format)
        with st.expander(f"Correction {index} · {changed_at} · {entry.get('by', 'Unknown editor')}"):
            st.write(f"**Reason:** {entry.get('reason', '—')}")
            changes = [
                {
                    "Changed field": field.replace("_", " ").title(),
                    "Before": display_change_value(field, values.get("from"), display_timezone, date_format, clock_format),
                    "After": display_change_value(field, values.get("to"), display_timezone, date_format, clock_format),
                }
                for field, values in entry.get("changes", {}).items()
            ]
            if changes:
                st.dataframe(changes, use_container_width=True, hide_index=True)


def show_api_error(exc: ApiError):
    st.error(str(exc))


if "api_url" not in st.session_state:
    st.session_state.api_url = "http://127.0.0.1:8000"

with st.sidebar:
    st.title("HRone")
    st.caption("Employee attendance & analytics")
    st.text_input("Attendance service address", key="api_url", help="The address where the attendance service is running.")
    st.subheader("Your display preferences")
    display_timezone_name = st.selectbox(
        "Time zone",
        TIMEZONES,
        index=TIMEZONES.index("Asia/Kolkata") if "Asia/Kolkata" in TIMEZONES else 0,
        help="All displayed punch times use this time zone. API timestamps stay unchanged.",
    )
    display_timezone = ZoneInfo(display_timezone_name)
    date_style = st.selectbox("Date format", list(DATE_FORMATS))
    date_format = DATE_FORMATS[date_style]
    clock_format = st.selectbox("Clock", ["12-hour", "24-hour"])
    page = st.radio(
        "Workspace",
        ["Overview", "Attendance", "Employees", "Regularize", "Analytics"],
        label_visibility="collapsed",
    )
    st.divider()
    try:
        api_request("GET", "/health")
        st.success("Attendance service connected · Data store available")
    except ApiError as exc:
        st.error("Attendance service unavailable")
        st.caption(str(exc))

st.title(page)
page_descriptions = {
    "Overview": "A quick snapshot of your people and recent attendance activity.",
    "Attendance": "Record work starts and finishes, then review or download attendance.",
    "Employees": "Add employee profiles and review the employee directory.",
    "Regularize": "Correct a recorded attendance entry and keep a visible audit trail.",
    "Analytics": "Review monthly attendance patterns by department and employee.",
}
st.caption(page_descriptions[page])

try:
    if page == "Overview":
        today = datetime.now(IST).date()
        employees = fetch_employees()
        recent = api_request(
            "GET",
            "/attendance",
            params={
                "date_from": (today - timedelta(days=6)).isoformat(),
                "date_to": today.isoformat(),
                "page": 1,
                "page_size": 100,
            },
        )
        logs = recent["items"]
        present_count = sum(log["status"] in ("PRESENT", "WFH", "ON_DUTY") for log in logs)
        open_shifts = sum(
            log["status"] in ("PRESENT", "WFH", "ON_DUTY") and log.get("punch_out") is None
            for log in logs
        )
        metrics = st.columns(4)
        metrics[0].metric("Employees", employees["total"])
        metrics[1].metric("Attendance records · 7 days", recent["total"])
        metrics[2].metric("Working attendance entries", present_count)
        metrics[3].metric("Open shifts", open_shifts)
        st.caption("Working entries include on-site, work-from-home, and off-site duty records.")

        left, right = st.columns([1, 2])
        with left:
            st.subheader("Recent status mix")
            counts = {
                STATUS_LABELS[status]: sum(item["status"] == status for item in logs)
                for status in STATUSES
            }
            counts = {status: count for status, count in counts.items() if count}
            if counts:
                st.bar_chart(
                    {"Attendance type": list(counts), "Number of records": list(counts.values())},
                    x="Attendance type",
                    y="Number of records",
                    color="#4263eb",
                )
            else:
                st.info("No attendance records in the last seven days.")
        with right:
            st.subheader("Latest attendance")
            st.caption("Attendance day follows the system's India Standard Time business-day rule.")
            if logs:
                latest = sorted(logs, key=lambda item: (item["date"], item["emp_code"]), reverse=True)
                st.dataframe(
                    display_logs(latest[:10], display_timezone, date_format, clock_format),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Attendance activity will appear here.")
        st.caption(
            f"Current attendance day: {format_date(today, date_format)} · "
            f"Displayed times: {display_timezone_name}."
        )

    elif page == "Employees":
        st.subheader("Add an employee")
        st.caption("Employee codes identify each person; shift hours use the team's local wall-clock time.")
        with st.form("create_employee", clear_on_submit=True):
            col1, col2 = st.columns(2)
            emp_code = col1.text_input("Employee code", placeholder="EMP1234")
            name = col2.text_input("Full name")
            email = col1.text_input("Work email")
            department = col2.text_input("Department")
            joined_on = col1.date_input("Joining date", value=datetime.now(IST).date())
            shift_start = col2.time_input("Shift starts", value=datetime.strptime("09:30", "%H:%M").time())
            shift_end = col1.time_input("Shift ends", value=datetime.strptime("18:30", "%H:%M").time())
            submitted = st.form_submit_button("Create employee", type="primary")
        if submitted:
            try:
                employee = api_request(
                    "POST",
                    "/employees",
                    payload={
                        "emp_code": emp_code.strip(),
                        "name": name.strip(),
                        "email": email.strip(),
                        "department": department.strip(),
                        "joined_on": joined_on.isoformat(),
                        "shift_start": shift_start.strftime("%H:%M"),
                        "shift_end": shift_end.strftime("%H:%M"),
                    },
                )
                st.success(f"Created {employee['name']} ({employee['emp_code']}).")
            except ApiError as exc:
                show_api_error(exc)

        st.divider()
        st.subheader("Employee directory")
        st.caption("Profile details are stored separately from the person's daily attendance records.")
        directory = fetch_employees()
        if directory["items"]:
            st.caption(f"{directory['total']} employee(s) · showing up to 100")
            st.dataframe(
                [
                    display_employee(person, date_format, display_timezone, clock_format)
                    for person in directory["items"]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No employees yet. Add one above to get started.")

    elif page == "Attendance":
        people = fetch_employees()["items"]
        employee_options = {f"{person['emp_code']} · {person['name']}": person["emp_code"] for person in people}
        st.subheader("Record a workday")
        st.caption(
            "Choose an employee to record a start or finish. Punch times you enter are interpreted in "
            f"{display_timezone_name}."
        )
        if not employee_options:
            st.info("Create an employee before recording attendance.")
        else:
            employee_label = st.selectbox("Employee", list(employee_options))
            emp_code = employee_options[employee_label]
            punch_col, out_col = st.columns(2)
            with punch_col:
                st.subheader("Record punch-in")
                with st.form("punch_in"):
                    status = st.selectbox(
                        "How did the employee work?",
                        ["PRESENT", "WFH", "ON_DUTY"],
                        format_func=STATUS_LABELS.__getitem__,
                    )
                    use_custom_in = st.checkbox("Enter a different start time", key="custom_punch_in")
                    punch_in_dt = st.datetime_input(
                        f"Start time ({display_timezone_name})",
                        value=datetime.now(display_timezone).replace(tzinfo=None),
                        disabled=not use_custom_in,
                    )
                    punch_in_submit = st.form_submit_button("Punch in", type="primary")
                if punch_in_submit:
                    payload = {"emp_code": emp_code, "status": status}
                    if use_custom_in:
                        payload["punched_at"] = to_epoch_ms(punch_in_dt, display_timezone)
                    try:
                        result = api_request("POST", "/attendance/punch-in", payload=payload)
                        st.success(
                            f"Start recorded for {format_date(result['date'], date_format)} · "
                            f"{format_datetime(result['punch_in'], display_timezone, date_format, clock_format)}"
                        )
                    except ApiError as exc:
                        show_api_error(exc)
            with out_col:
                st.subheader("Record punch-out")
                st.write("Closes this employee’s latest open attendance shift.")
                with st.form("punch_out"):
                    use_custom_out = st.checkbox("Enter a different finish time", key="custom_punch_out")
                    punch_out_dt = st.datetime_input(
                        f"Finish time ({display_timezone_name})",
                        value=datetime.now(display_timezone).replace(tzinfo=None),
                        disabled=not use_custom_out,
                    )
                    punch_out_submit = st.form_submit_button("Punch out")
                if punch_out_submit:
                    payload = {"emp_code": emp_code}
                    if use_custom_out:
                        payload["punched_at"] = to_epoch_ms(punch_out_dt, display_timezone)
                    try:
                        result = api_request("POST", "/attendance/punch-out", payload=payload)
                        st.success(
                            f"Shift closed · {result.get('work_hours'):.2f} hours worked · "
                            f"finished {format_datetime(result.get('punch_out'), display_timezone, date_format, clock_format)}."
                        )
                    except ApiError as exc:
                        show_api_error(exc)

        st.divider()
        st.subheader("Attendance records")
        st.caption("Attendance day is assigned using the API's IST business-day rules; punch times below use your selected time zone.")
        st.caption("A dash (—) means the value is not recorded or does not apply, such as punch-out for an open shift.")
        filter_cols = st.columns(3)
        filter_employee_labels = {
            "All employees": None,
            **{
                f"{person['emp_code']} · {person['name']}": person["emp_code"]
                for person in people
            },
        }
        selected_employee_label = filter_cols[0].selectbox(
            "Filter employee",
            list(filter_employee_labels),
        )
        filter_employee = filter_employee_labels[selected_employee_label]
        status_filter_options = ["All attendance types"] + STATUSES
        filter_status = filter_cols[1].selectbox(
            "Attendance type",
            status_filter_options,
            format_func=lambda value: "All attendance types" if value == "All attendance types" else STATUS_LABELS[value],
        )
        filter_days = filter_cols[2].selectbox(
            "Show records from the last",
            [7, 30, 90],
            index=1,
            format_func=lambda days: f"{days} days",
        )
        today = datetime.now(IST).date()
        params = {
            "date_from": (today - timedelta(days=filter_days - 1)).isoformat(),
            "date_to": today.isoformat(),
            "page": 1,
            "page_size": 100,
        }
        if filter_employee != "All employees":
            params["emp_code"] = filter_employee
        if filter_status != "All attendance types":
            params["status"] = filter_status
        attendance = api_request("GET", "/attendance", params=params)
        st.caption(f"{attendance['total']} record(s) · showing up to 100")
        table_rows = display_logs(attendance["items"], display_timezone, date_format, clock_format)
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
        if attendance["items"]:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=table_rows[0].keys())
            writer.writeheader()
            writer.writerows(table_rows)
            st.download_button("Download these records as CSV", output.getvalue(), "attendance.csv", "text/csv")

    elif page == "Regularize":
        st.subheader("Correct an attendance record")
        st.caption(
            "Choose an existing record, make a meaningful correction, and explain why. "
            "The API keeps an audit history of who changed what and when."
        )
        people = fetch_employees()["items"]
        employee_options = {f"{person['emp_code']} · {person['name']}": person["emp_code"] for person in people}
        if not employee_options:
            st.info("Create an employee before regularizing attendance.")
        else:
            employee_label = st.selectbox("Employee", list(employee_options))
            emp_code = employee_options[employee_label]
            records = api_request(
                "GET",
                "/attendance",
                params={"emp_code": emp_code, "page": 1, "page_size": 100},
            )["items"]
            if not records:
                st.info("No attendance records exist for this employee yet.")
            else:
                record_by_date = {record["date"]: record for record in records}
                ordered_dates = sorted(record_by_date, reverse=True)
                selected_date = st.selectbox(
                    "Choose the attendance day to correct",
                    ordered_dates,
                    format_func=lambda day: format_date(day, date_format),
                )
                current = record_by_date[selected_date]
                st.info(
                    f"Current status: **{STATUS_LABELS[current['status']]}** · "
                    f"corrections so far: {len(current.get('history') or [])}"
                )
                st.caption(
                    "Changing an absent or leave day to a working status requires a punch-in time. "
                    "Punch times cannot be added to an absent or leave status."
                )
                st.caption(
                    f"Current punch-in: {format_datetime(current.get('punch_in'), display_timezone, date_format, clock_format)} · "
                    f"Current punch-out: {format_datetime(current.get('punch_out'), display_timezone, date_format, clock_format)}"
                )
                with st.form("regularize"):
                    new_status = st.selectbox(
                        "Correct attendance type",
                        ["Keep current"] + STATUSES,
                        format_func=lambda value: "Keep current status" if value == "Keep current" else STATUS_LABELS[value],
                    )
                    change_times = st.checkbox("Correct the recorded start or finish time")
                    punch_in_dt = st.datetime_input(
                        f"Correct start time ({display_timezone_name})",
                        value=local_input_time(current.get("punch_in"), display_timezone),
                        disabled=not change_times,
                    )
                    punch_out_dt = st.datetime_input(
                        f"Correct finish time ({display_timezone_name})",
                        value=local_input_time(current.get("punch_out"), display_timezone),
                        disabled=not change_times,
                    )
                    reason = st.text_input("Reason (required)", placeholder="Explain why this correction is needed")
                    regularized_by = st.text_input("Corrected by", value="hr.admin")
                    submit_correction = st.form_submit_button("Save correction", type="primary")
                if submit_correction:
                    payload = {
                        "reason": reason.strip(),
                        "regularized_by": regularized_by.strip(),
                    }
                    if new_status != "Keep current":
                        payload["status"] = new_status
                    if change_times:
                        if new_status not in ("ABSENT", "LEAVE"):
                            payload["punch_in"] = to_epoch_ms(punch_in_dt, display_timezone)
                            payload["punch_out"] = to_epoch_ms(punch_out_dt, display_timezone)
                        else:
                            st.error("Punch times cannot be set for ABSENT or LEAVE.")
                            st.stop()
                    try:
                        result = api_request(
                            "PATCH",
                            f"/attendance/{emp_code}/{selected_date}",
                            payload=payload,
                        )
                        show_corrected_record(result, display_timezone, date_format, clock_format)
                    except ApiError as exc:
                        show_api_error(exc)

    elif page == "Analytics":
        st.subheader("Understand attendance patterns")
        st.caption(
            "Monthly totals and rates are calculated by the API from saved attendance. "
            "Choose a month and department to explore the results."
        )
        people = fetch_employees()["items"]
        departments = sorted({person["department"] for person in people})
        if not departments:
            st.info("Add employees to see analytics.")
        else:
            month = st.date_input("Choose a month to analyse", value=datetime.now(IST).date())
            month_key = month.strftime("%Y-%m")
            month_label = month.strftime("%B %Y")
            department = st.selectbox("Department", departments)
            summary = api_request(
                "GET",
                "/analytics/departments/summary",
                params={"month": month_key, "department": department},
            )["items"]
            leaderboard = api_request(
                "GET",
                "/analytics/leaderboard/late",
                params={"month": month_key, "department": department, "limit": 10},
            )["items"]
            if summary:
                metrics = st.columns(4)
                metrics[0].metric("Department headcount", summary[0]["headcount"])
                metrics[1].metric("Present-equivalent days", summary[0]["present_days"])
                metrics[2].metric("Late attendance entries", summary[0]["late_count"])
                metrics[3].metric("Leave entries", summary[0]["leave_count"])
                st.subheader(f"{department} · {month_label}")
                st.caption("Present-equivalent days include full and half-day attendance.")
                summary_rows = [
                    {
                        "Department": row["department"],
                        "Employees": row["headcount"],
                        "Present-equivalent days": row["present_days"],
                        "Average hours worked": "—" if row["avg_work_hours"] is None else f"{row['avg_work_hours']:.2f}",
                        "Late attendance entries": row["late_count"],
                        "Minutes late": row["total_late_minutes"],
                        "Leave entries": row["leave_count"],
                        "On-duty entries": row["on_duty_count"],
                    }
                    for row in summary
                ]
                st.dataframe(summary_rows, use_container_width=True, hide_index=True)
            else:
                st.info("There are no summary rows for this department and month.")
            st.subheader("Late-arrival leaderboard")
            st.caption("Ranks employees by total late minutes in the selected month. Tied employees share a rank.")
            if leaderboard:
                st.dataframe(
                    [
                        {
                            "Rank": row["rank"],
                            "Employee code": row["emp_code"],
                            "Name": row["name"],
                            "Department": row["department"],
                            "Minutes late": row["total_late_minutes"],
                            "Late attendance entries": row["late_count"],
                        }
                        for row in leaderboard
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No late arrivals recorded for this month.")

            st.subheader("Daily department attendance trend")
            trend_end = datetime.now(IST).date()
            trend_start = trend_end - timedelta(days=13)
            trend_range = st.date_input(
                "Choose the date range (up to 92 days)",
                value=(trend_start, trend_end),
                max_value=trend_end,
            )
            if isinstance(trend_range, tuple) and len(trend_range) == 2:
                trend_start, trend_end = trend_range
                trend = api_request(
                    "GET",
                    f"/analytics/departments/{department}/trend",
                    params={"from": trend_start.isoformat(), "to": trend_end.isoformat()},
                )["items"]
                if trend:
                    st.line_chart(
                        {
                            "Date": [format_date(row["date"], date_format) for row in trend],
                            "Attendance rate (%)": [
                                None if row["attendance_rate"] is None else row["attendance_rate"] * 100
                                for row in trend
                            ],
                            "7-day average (%)": [
                                None if row["moving_avg_7d"] is None else row["moving_avg_7d"] * 100
                                for row in trend
                            ],
                        },
                        x="Date",
                        y=["Attendance rate (%)", "7-day average (%)"],
                    )
                    st.caption(
                        "Attendance rate is present-equivalent days divided by eligible employees. "
                        "Weekends have no rate."
                    )
                else:
                    st.info("No trend data is available for this date range.")

            person_labels = {
                f"{person['emp_code']} · {person['name']}": person["emp_code"] for person in people
            }
            st.subheader("Individual monthly summary")
            st.caption("Attendance percentage uses working days since the employee's joining date.")
            selected_person = st.selectbox("Choose an employee", list(person_labels))
            monthly = api_request(
                "GET",
                f"/analytics/employees/{person_labels[selected_person]}/monthly",
                params={"month": month_key},
            )
            monthly_cols = st.columns(4)
            monthly_cols[0].metric("Attendance", "—" if monthly["attendance_pct"] is None else f"{monthly['attendance_pct']}%")
            monthly_cols[1].metric("Present-equivalent days", monthly["present_days"])
            monthly_cols[2].metric("Late minutes", monthly["total_late_minutes"])
            monthly_cols[3].metric("Overtime minutes", monthly["total_overtime_minutes"])

except ApiError as exc:
    show_api_error(exc)
