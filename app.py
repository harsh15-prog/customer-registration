import hmac
import os
import re
import secrets
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pymysql
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request
from itsdangerous import BadData, URLSafeTimedSerializer

import database
from validation import MIN_DOB, validate_customer


load_dotenv()
app = Flask(__name__, static_folder="public/static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8192
KEY = re.compile(r"[A-Za-z0-9._:-]{8,255}")


def today():
    return datetime.now(ZoneInfo(os.environ.get("CUSTOMER_TIME_ZONE") or "Asia/Kolkata")).date()


def problem(message, status, fields=None):
    body = {"error": message}
    if fields:
        body["fields"] = fields
    return jsonify(body), status


@app.after_request
def response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; "
        "object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/form")
def form():
    rate_secret = os.environ.get("RATE_LIMIT_SECRET", "")
    fingerprint_secret = os.environ.get("SUBMISSION_FINGERPRINT_SECRET", "")
    ready = (len(rate_secret) >= 32 and len(fingerprint_secret) >= 32
             and all(os.environ.get(key) for key in database.MYSQL_REQUIRED))
    ticket = form_signer(rate_secret).dumps(secrets.token_urlsafe(18)) if ready else ""
    return render_template("form.html", today=today(), earliest=MIN_DOB,
                           configured=ready, form_ticket=ticket)


@app.get("/google-form")
def google_form():
    url = os.environ.get("GOOGLE_FORM_URL", "")
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc == "docs.google.com" and re.fullmatch(
        r"/forms/(?:u/[0-9]+/)?d/(?:e/)?[A-Za-z0-9_-]+/viewform/?", parts.path
    ):
        return redirect(url, code=302)
    return render_template(
        "message.html", heading="Google Form unavailable",
        message="The Google registration form has not been connected yet.",
    ), 503


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.errorhandler(413)
def too_large(error):
    return problem("The request is too large.", 413)


def form_signer(secret):
    return URLSafeTimedSerializer(secret, salt="customer-registration-form")


def verify_form_ticket(ticket, secret):
    if not isinstance(ticket, str) or not 1 <= len(ticket) <= 256:
        return False
    try:
        form_signer(secret).loads(ticket, max_age=7200)
    except BadData:
        return False
    return True


def client_address():
    if os.environ.get("VERCEL"):
        return request.headers.get("x-vercel-forwarded-for", "").split(",")[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def submit(source):
    if not request.is_json:
        return problem("Send a JSON request.", 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return problem("Send a valid JSON object.", 400)
    key = request.headers.get("Idempotency-Key", "")
    if not KEY.fullmatch(key):
        return problem("A valid submission key is required.", 400)
    try:
        rate_secret = os.environ.get("RATE_LIMIT_SECRET", "").encode("utf-8")
        fingerprint_secret = os.environ.get("SUBMISSION_FINGERPRINT_SECRET", "").encode("utf-8")
        if len(rate_secret) < 32 or len(fingerprint_secret) < 32:
            raise database.DatabaseUnavailable("Submission secrets are not configured.")
        if source == "web" and (
            data.get("website") or not verify_form_ticket(data.get("form_ticket"), rate_secret)
        ):
            return problem("Reload the registration page to refresh the security check, then try again.", 403)
        delay = database.consume_rate_limit(source, client_address(), rate_secret)
        if delay:
            response, status = problem("Too many attempts. Try again shortly.", 429)
            response.headers["Retry-After"] = str(delay)
            return response, status
        customer, errors = validate_customer(data, today())
        if errors:
            return problem("Check the highlighted fields.", 422, errors)
        customer_id, replayed = database.register(customer, source, key, fingerprint_secret)
        return jsonify(customer_id=customer_id, replayed=replayed), 200 if replayed else 201
    except database.SubmissionConflict:
        return problem("That submission key was already used with different details.", 409)
    except (database.DatabaseUnavailable, pymysql.MySQLError, OSError, ValueError) as exc:
        app.logger.error("Registration dependency failure (%s, %s)", source, type(exc).__name__)
        return problem("Registration is temporarily unavailable. Try again shortly.", 503)


@app.post("/api/customers")
def web_submission():
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return problem("Open the registration form to submit your details.", 403)
    return submit("web")


@app.post("/api/google-form")
def google_submission():
    secret = os.environ.get("GOOGLE_WEBHOOK_SECRET", "").encode("utf-8")
    expected = b"Bearer " + secret
    supplied = request.headers.get("Authorization", "").encode("utf-8")
    if len(secret) < 32 or not hmac.compare_digest(expected, supplied):
        return problem("Unauthorized.", 401)
    return submit("google")
