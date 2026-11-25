import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import database
from app import app


@unittest.skipUnless(os.environ.get("RUN_MYSQL_INTEGRATION") == "1", "Needs an isolated MySQL test database.")
class MysqlTests(unittest.TestCase):
    def test_both_routes_commit_and_retries_do_not_duplicate(self):
        if os.environ.get("MYSQL_DATABASE") != "customer_registration_test":
            self.fail("Point this test at the isolated customer_registration_test database.")
        schema = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
        with closing(database.connect()) as connection, connection.cursor() as cursor:
            for statement in schema.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    cursor.execute(statement)
            connection.commit()
            cursor.execute("SELECT COUNT(*) FROM customers")
            initial_count = cursor.fetchone()[0]

        config = {
            "LOCAL_SKIP_TURNSTILE": "1",
            "SUBMISSION_FINGERPRINT_SECRET": "f" * 40,
            "RATE_LIMIT_SECRET": "r" * 40,
            "GOOGLE_WEBHOOK_SECRET": "g" * 40,
        }
        details = {"name": "D'Arcy Example", "email": "qa@example.com", "dob": "2000-02-29",
                   "balance": "10.50", "age": 9000}
        key = os.urandom(12).hex()

        def post_web(_):
            with app.test_client() as client:
                return client.post("/api/customers", json=details, headers={"Idempotency-Key": key})

        with patch.dict(os.environ, config), ThreadPoolExecutor(max_workers=4) as pool:
            web = list(pool.map(post_web, range(4)))
            self.assertEqual(sorted(row.status_code for row in web), [200, 200, 200, 201])
            self.assertEqual(len({row.json["customer_id"] for row in web}), 1)
            with app.test_client() as client:
                google_path = "/api/google-form"
                headers = {"Idempotency-Key": key, "Authorization": "Bearer " + config["GOOGLE_WEBHOOK_SECRET"]}
                created = client.post(google_path, json=details, headers=headers)
                replay = client.post(google_path, json=details, headers=headers)
                conflict = client.post(google_path, json={**details, "balance": "19"}, headers=headers)
            self.assertEqual([created.status_code, replay.status_code, conflict.status_code], [201, 200, 409])
            expected_ids = sorted([web[0].json["customer_id"], created.json["customer_id"]])
            self.assertEqual(created.json["customer_id"], replay.json["customer_id"])

        with closing(database.connect()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM customers")
            self.assertEqual(cursor.fetchone()[0], initial_count + 2)
            cursor.execute("SELECT c_id, name, age, balance FROM customers WHERE c_id IN (%s, %s) ORDER BY c_id",
                           expected_ids)
            rows = cursor.fetchall()
            self.assertEqual([row[0] for row in rows], expected_ids)
            self.assertTrue(all(row[1] == details["name"] and row[2] < 150 and row[3] == 10.5 for row in rows))
