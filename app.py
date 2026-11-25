import hmac
import json
import os
import re
from datetime import datetime
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import pymysql
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request

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
        "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
        "frame-src https://challenges.cloudflare.com; base-uri 'none'; "
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
    ready = local_skip_turnstile() or bool(turnstile_config())
    return render_template("form.html", today=today(), earliest=MIN_DOB,
                           configured=ready and bool(os.environ.get("MYSQL_HOST")),
                           turnstile_site_key=None if local_skip_turnstile()
                           else os.environ.get("TURNSTILE_SITE_KEY"))


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


def local_skip_turnstile():
    return not os.environ.get("VERCEL") and os.environ.get("LOCAL_SKIP_TURNSTILE") == "1"


def turnstile_config():
    values = tuple(os.environ.get(key) for key in (
        "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "TURNSTILE_HOSTNAME"
    ))
    return values if all(values) else None


def verify_turnstile(token):
    if local_skip_turnstile():
        return True
    config = turnstile_config()
    if not config:
        raise database.DatabaseUnavailable("Registration verification is not configured.")
    if not isinstance(token, str) or not 1 <= len(token) <= 2048:
        return False
    _, secret, hostname = config
    params = urlencode({"secret": secret, "response": token}).encode("ascii")
    with urlopen("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                 data=params, timeout=5) as response:
        result = json.load(response)
    return (result.get("success") is True and result.get("hostname") == hostname
            and result.get("action") == "register")


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
        delay = database.consume_rate_limit(source, client_address(), rate_secret)
        if delay:
            response, status = problem("Too many attempts. Try again shortly.", 429)
            response.headers["Retry-After"] = str(delay)
            return response, status
        customer, errors = validate_customer(data, today())
        if errors:
            return problem("Check the highlighted fields.", 422, errors)
        if source == "web" and (
            data.get("website") or not verify_turnstile(data.get("turnstile_token"))
        ):
            return problem("Complete the security check and try again.", 403)

        customer_id, replayed = database.register(customer, source, key, fingerprint_secret)
        return jsonify(customer_id=customer_id, replayed=replayed), 200 if replayed else 201
    except database.SubmissionConflict:
        return problem("That submission key was already used with different details.", 409)
    except (database.DatabaseUnavailable, pymysql.MySQLError, OSError, URLError, ValueError) as exc:
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
