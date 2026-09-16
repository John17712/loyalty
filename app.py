from flask import Flask, render_template, request, redirect, flash, url_for, session, abort, Response
import calendar as pycalendar
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, date, time, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

COMPANY_NAME = os.getenv("COMPANY_NAME", "Loyalty Group Homes")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "12640 SE Madison St, Portland, Oregon 97233")
COMPANY_TIMEZONE = os.getenv("COMPANY_TIMEZONE", "America/Los_Angeles")
TZ = ZoneInfo(COMPANY_TIMEZONE)

CALENDAR_BACKEND = os.getenv("CALENDAR_BACKEND", "local").lower().strip()
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
DB_PATH = Path(os.getenv("CALENDAR_DB_PATH", str(BASE_DIR / "appointments.db")))

try:
    CALENDAR_ADMIN_IDLE_MINUTES = max(1, int(os.getenv("CALENDAR_ADMIN_IDLE_MINUTES", "5")))
except ValueError:
    CALENDAR_ADMIN_IDLE_MINUTES = 5
CALENDAR_ADMIN_IDLE_SECONDS = CALENDAR_ADMIN_IDLE_MINUTES * 60

CATEGORIES = ["Medical", "Dental", "Staff Meeting", "Client Activity", "Transportation", "Home Visit", "Other"]
EDITABLE_STATUSES = ["Upcoming", "Confirmed", "Completed", "Cancelled"]
STATUSES = EDITABLE_STATUSES + ["Past"]


def now_local():
    return datetime.now(TZ)


def parse_local_datetime(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)


def serialize_dt(value):
    return value.astimezone(TZ).isoformat()


def month_bounds(year, month):
    start = datetime(year, month, 1, tzinfo=TZ)
    end = datetime(year + 1, 1, 1, tzinfo=TZ) if month == 12 else datetime(year, month + 1, 1, tzinfo=TZ)
    return start, end


def init_db():
    if CALENDAR_BACKEND != "local":
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_name TEXT,
                title TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                location TEXT,
                description TEXT,
                category TEXT NOT NULL DEFAULT 'Other',
                other_category_detail TEXT,
                assigned_staff TEXT,
                status TEXT NOT NULL DEFAULT 'Upcoming',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(appointments)").fetchall()}
        if "resident_name" not in columns:
            conn.execute("ALTER TABLE appointments ADD COLUMN resident_name TEXT")
        if "other_category_detail" not in columns:
            conn.execute("ALTER TABLE appointments ADD COLUMN other_category_detail TEXT")
        conn.commit()


def row_to_event(row):
    return {
        "id": str(row["id"]),
        "resident_name": row["resident_name"] or "",
        "title": row["title"],
        "start": datetime.fromisoformat(row["start_at"]).astimezone(TZ),
        "end": datetime.fromisoformat(row["end_at"]).astimezone(TZ),
        "location": row["location"] or "",
        "description": row["description"] or "",
        "category": row["category"] or "Other",
        "other_category_detail": row["other_category_detail"] or "",
        "assigned_staff": row["assigned_staff"] or "",
        "status": row["status"] or "Upcoming",
        "provider": "local",
    }


def google_service():
    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("GOOGLE_CALENDAR_ID is not configured.")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google Calendar packages are not installed.") from exc

    scopes = ["https://www.googleapis.com/auth/calendar"]
    json_blob = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    json_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if json_blob:
        creds = service_account.Credentials.from_service_account_info(json.loads(json_blob), scopes=scopes)
    elif json_file:
        creds = service_account.Credentials.from_service_account_file(json_file, scopes=scopes)
    else:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def google_to_event(item):
    start_data = item.get("start", {})
    end_data = item.get("end", {})
    if "dateTime" in start_data:
        start = datetime.fromisoformat(start_data["dateTime"].replace("Z", "+00:00")).astimezone(TZ)
    else:
        start = datetime.combine(date.fromisoformat(start_data["date"]), time.min, tzinfo=TZ)
    if "dateTime" in end_data:
        end = datetime.fromisoformat(end_data["dateTime"].replace("Z", "+00:00")).astimezone(TZ)
    else:
        end = datetime.combine(date.fromisoformat(end_data.get("date", start.date().isoformat())), time.min, tzinfo=TZ)
    props = item.get("extendedProperties", {}).get("shared", {})
    return {
        "id": item["id"],
        "resident_name": props.get("resident_name", ""),
        "title": item.get("summary", "Untitled appointment"),
        "start": start,
        "end": end,
        "location": item.get("location", ""),
        "description": item.get("description", ""),
        "category": props.get("category", "Other"),
        "other_category_detail": props.get("other_category_detail", ""),
        "assigned_staff": props.get("assigned_staff", ""),
        "status": props.get("appointment_status", "Upcoming"),
        "provider": "google",
    }


def list_events(start, end):
    if CALENDAR_BACKEND == "google":
        result = google_service().events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            showDeleted=False,
        ).execute()
        return [google_to_event(item) for item in result.get("items", [])]

    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM appointments WHERE start_at < ? AND end_at > ? ORDER BY start_at ASC",
            (serialize_dt(end), serialize_dt(start)),
        ).fetchall()
    return [row_to_event(row) for row in rows]


def get_event(event_id):
    if CALENDAR_BACKEND == "google":
        item = google_service().events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return google_to_event(item)
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (event_id,)).fetchone()
    return row_to_event(row) if row else None


def google_body(data):
    return {
        "summary": data["title"],
        "location": data["location"],
        "description": data["description"],
        "start": {"dateTime": data["start"].isoformat(), "timeZone": COMPANY_TIMEZONE},
        "end": {"dateTime": data["end"].isoformat(), "timeZone": COMPANY_TIMEZONE},
        "extendedProperties": {"shared": {
            "resident_name": data["resident_name"],
            "category": data["category"],
            "other_category_detail": data["other_category_detail"],
            "assigned_staff": data["assigned_staff"],
            "appointment_status": data["status"],
        }},
    }


def create_event(data):
    if CALENDAR_BACKEND == "google":
        return google_service().events().insert(calendarId=GOOGLE_CALENDAR_ID, body=google_body(data)).execute()["id"]

    init_db()
    stamp = now_local().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO appointments
            (resident_name,title,start_at,end_at,location,description,category,other_category_detail,assigned_staff,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data["resident_name"], data["title"], serialize_dt(data["start"]), serialize_dt(data["end"]),
            data["location"], data["description"], data["category"], data["other_category_detail"],
            data["assigned_staff"], data["status"], stamp, stamp,
        ))
        conn.commit()
        return str(cur.lastrowid)


def update_event(event_id, data):
    if CALENDAR_BACKEND == "google":
        svc = google_service()
        item = svc.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        item.update(google_body(data))
        svc.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=item).execute()
        return

    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE appointments SET resident_name=?,title=?,start_at=?,end_at=?,location=?,description=?,category=?,
            other_category_detail=?,assigned_staff=?,status=?,updated_at=? WHERE id=?
        """, (
            data["resident_name"], data["title"], serialize_dt(data["start"]), serialize_dt(data["end"]),
            data["location"], data["description"], data["category"], data["other_category_detail"],
            data["assigned_staff"], data["status"], now_local().isoformat(), event_id,
        ))
        conn.commit()


def delete_event(event_id):
    if CALENDAR_BACKEND == "google":
        google_service().events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM appointments WHERE id = ?", (event_id,))
        conn.commit()


def form_event_data():
    values = {
        "resident_name": request.form.get("resident_name", "").strip(),
        "title": request.form.get("title", "").strip(),
        "start_raw": request.form.get("start", "").strip(),
        "end_raw": request.form.get("end", "").strip(),
        "category": request.form.get("category", "").strip(),
        "status": request.form.get("status", "").strip(),
        "location": request.form.get("location", "").strip(),
        "assigned_staff": request.form.get("assigned_staff", "").strip(),
        "other_category_detail": request.form.get("other_category_detail", "").strip(),
        "description": request.form.get("description", "").strip(),
    }
    required = {
        "Resident name": values["resident_name"], "Appointment / event title": values["title"],
        "Start time": values["start_raw"], "End time": values["end_raw"], "Category": values["category"],
        "Status": values["status"], "Location": values["location"], "Assigned staff": values["assigned_staff"],
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Required field(s): " + ", ".join(missing) + ".")
    if values["category"] not in CATEGORIES:
        raise ValueError("Please select a valid appointment category.")
    if values["status"] not in EDITABLE_STATUSES:
        raise ValueError("Please select a valid appointment status.")
    if values["category"] == "Other" and not values["other_category_detail"]:
        raise ValueError("Please describe the appointment type when Category is Other.")
    if values["category"] != "Other":
        values["other_category_detail"] = ""
    start = parse_local_datetime(values.pop("start_raw"))
    end = parse_local_datetime(values.pop("end_raw"))
    if end <= start:
        raise ValueError("End time must be after start time.")
    values["start"] = start
    values["end"] = end
    return values


def decorate_event(event, reference=None):
    reference = reference or now_local()
    result = dict(event)
    stored = event.get("status", "Upcoming")
    is_past = event["end"] <= reference and stored not in {"Completed", "Cancelled"}
    display_status = "Past" if is_past else stored
    seconds_to_start = (event["start"] - reference).total_seconds()
    is_urgent = display_status in {"Upcoming", "Confirmed"} and 0 <= seconds_to_start <= 3600
    result.update(
        display_status=display_status,
        is_past=is_past,
        is_urgent=is_urgent,
        start_iso=event["start"].isoformat(),
        end_iso=event["end"].isoformat(),
    )
    return result


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def validate_csrf():
    sent = request.form.get("csrf_token", "")
    stored = session.get("csrf_token", "")
    if not sent or not stored or not hmac.compare_digest(sent, stored):
        abort(400, "Invalid form token.")


def clear_admin():
    session.pop("calendar_admin", None)
    session.pop("calendar_admin_last_activity", None)


def admin_session_valid(refresh=True):
    if not session.get("calendar_admin"):
        return False, False
    now_ts = int(datetime.now().timestamp())
    try:
        idle = now_ts - int(session.get("calendar_admin_last_activity"))
    except (TypeError, ValueError):
        clear_admin()
        return False, True
    if idle >= CALENDAR_ADMIN_IDLE_SECONDS:
        clear_admin()
        return False, True
    if refresh:
        session["calendar_admin_last_activity"] = now_ts
    return True, False


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        valid, expired = admin_session_valid(True)
        if not valid:
            if expired:
                flash(f"Manager session expired after {CALENDAR_ADMIN_IDLE_MINUTES} minutes of inactivity. Please sign in again.", "warning")
            return redirect(url_for("calendar_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def display_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("calendar_display"):
            return view(*args, **kwargs)
        valid, _ = admin_session_valid(False)
        if valid:
            return view(*args, **kwargs)
        return redirect(url_for("office_calendar_login"))
    return wrapped


def calendar_context(year=None, month=None):
    reference = now_local()
    today = reference.date()
    year = year or today.year
    month = month or today.month
    start, end = month_bounds(year, month)

    month_events = [decorate_event(e, reference) for e in list_events(start, end)]
    month_map = {}
    for event in month_events:
        key = event["start"].astimezone(TZ).date().isoformat()
        month_map.setdefault(key, []).append(event)
    for events in month_map.values():
        events.sort(key=lambda e: e["start"])
    month_days = [{"date": date.fromisoformat(k), "events": v} for k, v in sorted(month_map.items())]

    day_start = datetime.combine(today, time.min, tzinfo=TZ)
    day_end = day_start + timedelta(days=1)
    today_events = [decorate_event(e, reference) for e in list_events(day_start, day_end)]
    today_events.sort(key=lambda e: e["start"])

    upcoming_end = datetime.combine(today + timedelta(days=14), time.max, tzinfo=TZ)
    upcoming_events = [decorate_event(e, reference) for e in list_events(reference, upcoming_end)]
    upcoming_events = [
        e for e in upcoming_events
        if e["display_status"] not in {"Cancelled", "Completed", "Past"} and e["end"] > reference
    ]
    upcoming_events.sort(key=lambda e: e["start"])

    summary = {status: 0 for status in STATUSES}
    for event in month_events:
        summary[event["display_status"]] = summary.get(event["display_status"], 0) + 1
    summary["Total"] = len(month_events)

    return {
        "company_name": COMPANY_NAME,
        "company_address": COMPANY_ADDRESS,
        "company_timezone": COMPANY_TIMEZONE,
        "backend": CALENDAR_BACKEND,
        "today": today,
        "now": reference,
        "year": year,
        "month": month,
        "month_name": pycalendar.month_name[month],
        "month_map": month_map,
        "month_days": month_days,
        "today_events": today_events,
        "upcoming_events": upcoming_events,
        "month_events": month_events,
        "summary": summary,
        "categories": CATEGORIES,
        "statuses": STATUSES,
    }


@app.after_request
def protect_private_pages(response):
    if request.path.startswith("/staff/calendar") or request.path.startswith("/office-calendar"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow: /staff/calendar\nDisallow: /office-calendar\n", mimetype="text/plain")


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/send_message", methods=["POST"])
def send_message():
    name = request.form["name"]
    email = request.form["email"]
    phone = request.form["phone"]
    subject = request.form["subject"]
    message = request.form["message"]
    content = f"New Contact Form Submission:\n\nName: {name}\nEmail: {email}\nPhone: {phone}\nSubject: {subject}\nMessage:\n{message}\n"
    msg = EmailMessage()
    msg.set_content(content)
    msg["Subject"] = f"Loyalty Contact Form - {subject}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER
    try:
        if not EMAIL_USER or not EMAIL_PASS:
            raise RuntimeError("Email credentials are not configured.")
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_USER, EMAIL_PASS)
            smtp.send_message(msg)
        flash("Message sent successfully!", "success")
    except Exception as exc:
        app.logger.error("Contact email failed: %s", exc)
        flash("Failed to send message. Please try again later.", "danger")
    return redirect(url_for("contact"))


@app.route("/staff/calendar/login", methods=["GET", "POST"])
def calendar_login():
    configured_user = os.getenv("CALENDAR_ADMIN_USERNAME", "")
    password_hash = os.getenv("CALENDAR_ADMIN_PASSWORD_HASH", "")
    plain_password = os.getenv("CALENDAR_ADMIN_PASSWORD", "")
    setup_ready = bool(configured_user and (password_hash or plain_password))
    if request.method == "POST":
        validate_csrf()
        if not setup_ready:
            flash("Calendar admin login is not configured yet.", "danger")
            return render_template("calendar_login.html", setup_ready=False)
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user_ok = hmac.compare_digest(username, configured_user)
        password_ok = check_password_hash(password_hash, password) if password_hash else hmac.compare_digest(password, plain_password)
        if user_ok and password_ok:
            session.clear()
            session["calendar_admin"] = True
            session["calendar_admin_last_activity"] = int(datetime.now().timestamp())
            csrf_token()
            return redirect(url_for("calendar_admin"))
        flash("Incorrect username or password.", "danger")
    return render_template("calendar_login.html", setup_ready=setup_ready)


@app.route("/staff/calendar/logout", methods=["POST"])
@admin_required
def calendar_logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("calendar_login"))


@app.route("/staff/calendar")
@admin_required
def calendar_admin():
    try:
        year = int(request.args.get("year", now_local().year))
        month = int(request.args.get("month", now_local().month))
        if not 1 <= month <= 12:
            raise ValueError("Invalid month.")
        return render_template("calendar_admin.html", **calendar_context(year, month))
    except Exception as exc:
        app.logger.exception("Calendar dashboard error")
        return render_template(
            "calendar_error.html",
            company_name=COMPANY_NAME,
            company_address=COMPANY_ADDRESS,
            backend=CALENDAR_BACKEND,
            error=str(exc),
        ), 500


@app.route("/staff/calendar/new", methods=["GET", "POST"])
@admin_required
def calendar_new():
    if request.method == "POST":
        validate_csrf()
        try:
            create_event(form_event_data())
            flash("Appointment created.", "success")
            return redirect(url_for("calendar_admin"))
        except Exception as exc:
            flash(str(exc), "danger")
    default_start = now_local().replace(second=0, microsecond=0) + timedelta(hours=1)
    return render_template(
        "calendar_form.html",
        mode="new",
        event=None,
        categories=CATEGORIES,
        statuses=EDITABLE_STATUSES,
        default_start=default_start,
        default_end=default_start + timedelta(hours=1),
        company_name=COMPANY_NAME,
    )


@app.route("/staff/calendar/<event_id>/edit", methods=["GET", "POST"])
@admin_required
def calendar_edit(event_id):
    event = get_event(event_id)
    if not event:
        abort(404)
    if request.method == "POST":
        validate_csrf()
        try:
            update_event(event_id, form_event_data())
            flash("Appointment updated.", "success")
            return redirect(url_for("calendar_admin"))
        except Exception as exc:
            flash(str(exc), "danger")
    return render_template(
        "calendar_form.html",
        mode="edit",
        event=event,
        categories=CATEGORIES,
        statuses=EDITABLE_STATUSES,
        company_name=COMPANY_NAME,
    )


@app.route("/staff/calendar/<event_id>/delete", methods=["POST"])
@admin_required
def calendar_delete(event_id):
    validate_csrf()
    delete_event(event_id)
    flash("Appointment deleted.", "success")
    return redirect(url_for("calendar_admin"))


@app.route("/office-calendar/login", methods=["GET", "POST"])
def office_calendar_login():
    display_pin = os.getenv("CALENDAR_DISPLAY_PIN", "")
    if request.method == "POST":
        validate_csrf()
        if not display_pin:
            flash("Office display PIN is not configured yet.", "danger")
        elif hmac.compare_digest(request.form.get("pin", ""), display_pin):
            session.clear()
            session["calendar_display"] = True
            csrf_token()
            return redirect(url_for("office_calendar"))
        else:
            flash("Incorrect display PIN.", "danger")
    return render_template("office_calendar_login.html", company_name=COMPANY_NAME, setup_ready=bool(display_pin))


@app.route("/office-calendar/logout", methods=["POST"])
@display_required
def office_calendar_logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("office_calendar_login"))


@app.route("/office-calendar")
@display_required
def office_calendar():
    try:
        return render_template("office_calendar.html", **calendar_context())
    except Exception as exc:
        app.logger.exception("Office display error")
        return render_template(
            "calendar_error.html",
            company_name=COMPANY_NAME,
            company_address=COMPANY_ADDRESS,
            backend=CALENDAR_BACKEND,
            error=str(exc),
        ), 500


if __name__ == "__main__":
    init_db()
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
