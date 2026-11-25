import os
import re
import unittest
from datetime import date
from unittest.mock import patch

import database
from app import app
from validation import validate_customer


DETAILS = {
    "name": "Taylor Example",
    "email": "Taylor@EXAMPLE.COM",
    "dob": "2000-11-26",
    "balance": "123.40",
    "age": 2,
    "c_id": 200,
}
GOOGLE_SECRET = "g" * 40
TEST_CONFIG = {
    "VERCEL": "",
    "MYSQL_HOST": "example.invalid",
    "MYSQL_DATABASE": "test",
    "MYSQL_USER": "test",
    "MYSQL_PASSWORD": "test",
    "RATE_LIMIT_SECRET": "r" * 40,
    "SUBMISSION_FINGERPRINT_SECRET": "s" * 40,
    "GOOGLE_WEBHOOK_SECRET": GOOGLE_SECRET,
    "GOOGLE_FORM_URL": "https://docs.google.com/forms/d/e/1FAIpExample/viewform",
}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, TEST_CONFIG).start()
        patch("app.today", return_value=date(2026, 11, 25)).start()
        self.rate = patch("database.consume_rate_limit", return_value=0).start()
        self.insert = patch("database.register", return_value=(123, False)).start()
        self.client = app.test_client()
        form_html = self.client.get("/form").get_data(as_text=True)
        self.ticket = re.search(r'name="form_ticket" value="([^"]+)"', form_html)[1]

    def post(self, path="/api/customers", payload=DETAILS, auth=None):
        headers = {"Idempotency-Key": "fixture-key"}
        if auth is not None:
            headers["Authorization"] = "Bearer " + auth
        if path == "/api/customers":
            payload = {"form_ticket": self.ticket, **payload}
        return self.client.post(path, json=payload, headers=headers)

    def test_pages_and_redirect(self):
        self.assertIn(b"Google Registration Form", self.client.get("/").data)
        self.assertIn(b'id="age"', self.client.get("/form").data)
        with self.client.get("/static/css/style.css") as css:
            self.assertEqual(css.status_code, 200)
        self.assertEqual(self.client.get("/google-form").location, TEST_CONFIG["GOOGLE_FORM_URL"])
        self.assertEqual(self.client.get("/api/health").json, {"status": "ok"})

    def test_google_redirect_allowlist(self):
        with patch.dict(os.environ, {"GOOGLE_FORM_URL": "https://docs.google.com.evil.test/forms/d/e/id/viewform"}):
            self.assertEqual(self.client.get("/google-form").status_code, 503)

    def test_web_generates_id_and_recalculates_age(self):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json, {"customer_id": 123, "replayed": False})
        customer, source, key, _ = self.insert.call_args.args
        self.assertEqual(customer.age, 25)
        self.assertEqual(customer.email, "Taylor@example.com")
        self.assertEqual((source, key), ("web", "fixture-key"))

    def test_google_requires_secret_and_uses_same_validation(self):
        path = "/api/google-form"
        self.assertEqual(self.post(path).status_code, 401)
        self.rate.assert_not_called()
        self.insert.assert_not_called()
        self.assertEqual(self.post(path, auth=GOOGLE_SECRET).status_code, 201)
        self.assertEqual(self.insert.call_args.args[0].age, 25)
        self.assertEqual(self.insert.call_args.args[1], "google")

    def test_replay_and_conflict_have_distinct_statuses(self):
        self.insert.return_value = (123, True)
        self.assertEqual(self.post().json, {"customer_id": 123, "replayed": True})
        self.assertEqual(self.post().status_code, 200)
        self.insert.side_effect = database.SubmissionConflict
        self.assertEqual(self.post().status_code, 409)

    def test_rate_limit(self):
        self.rate.return_value = 17
        response = self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "17")
        self.insert.assert_not_called()

    def test_payload_and_size_validation(self):
        response = self.post(payload={**DETAILS, "dob": "2027-01-01", "balance": "7.999",
                                      "name": " ", "email": "test@invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(set(response.json["fields"]), {"dob", "balance", "name", "email"})
        oversized = self.post(payload={**DETAILS, "name": "x" * 9000})
        self.assertEqual(oversized.status_code, 413)
        self.insert.assert_not_called()

    def test_invalid_ticket_and_honeypot_are_blocked_before_database(self):
        self.assertEqual(self.post(payload={**DETAILS, "form_ticket": "fake"}).status_code, 403)
        self.assertEqual(self.post(payload={**DETAILS, "website": "spam"}).status_code, 403)
        self.rate.assert_not_called()
        self.insert.assert_not_called()

    def test_expired_ticket_requires_reload(self):
        with patch("itsdangerous.timed.time.time", return_value=1):
            form_html = self.client.get("/form").get_data(as_text=True)
            expired = re.search(r'name="form_ticket" value="([^"]+)"', form_html)[1]
        self.assertEqual(self.post(payload={**DETAILS, "form_ticket": expired}).status_code, 403)
        self.assertEqual(self.post().status_code, 201)

    def test_age_at_birthday_and_decimal_canonicalization(self):
        before, _ = validate_customer(DETAILS, date(2026, 11, 25))
        after, _ = validate_customer({**DETAILS, "balance": "123.4"}, date(2026, 11, 26))
        self.assertEqual((before.age, after.age), (25, 26))
        self.assertEqual(before.identity(), after.identity())


if __name__ == "__main__":
    unittest.main()
