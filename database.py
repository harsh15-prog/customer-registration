import hashlib
import hmac
import json
import os
import ssl
import time
from contextlib import closing

import pymysql


MYSQL_REQUIRED = ("MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")


class DatabaseUnavailable(Exception):
    pass


class SubmissionConflict(Exception):
    pass


def connect():
    if not all(os.environ.get(key) for key in MYSQL_REQUIRED):
        raise DatabaseUnavailable("MySQL configuration is incomplete.")
    try:
        tls = ssl.create_default_context()
        ca_pem = os.environ.get("MYSQL_SSL_CA_PEM")
        ca_file = os.environ.get("MYSQL_SSL_CA_FILE")
        if ca_pem or ca_file:
            tls.load_verify_locations(cafile=ca_file or None,
                                      cadata=ca_pem.replace("\\n", "\n") if ca_pem else None)
        if not os.environ.get("VERCEL") and os.environ.get("LOCAL_MYSQL_ALLOW_UNVERIFIED_TLS") == "1":
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
        connection = pymysql.connect(
            host=os.environ["MYSQL_HOST"], port=int(os.environ.get("MYSQL_PORT", "3306")),
            database=os.environ["MYSQL_DATABASE"], user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"], charset="utf8mb4",
            ssl=tls, connect_timeout=4, read_timeout=6, write_timeout=6,
        )
    except (ValueError, OSError, pymysql.MySQLError) as exc:
        raise DatabaseUnavailable("MySQL connection failed.") from exc

    # Some MySQL protocol servers can fall back to a plaintext connection.
    with connection.cursor() as cursor:
        try:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            result = cursor.fetchone()
            if not result or not result[1]:
                raise DatabaseUnavailable("MySQL TLS is required.")
        except (DatabaseUnavailable, OSError, pymysql.MySQLError):
            connection.close()
            raise
    return connection


def register(customer, source, key, secret):
    key_hash = hashlib.sha256(key.encode("utf-8")).digest()
    encoded = json.dumps(customer.identity(), ensure_ascii=False).encode("utf-8")
    payload_hash = hmac.digest(secret, encoded, "sha256")
    with closing(connect()) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO customer_submissions (source, key_hash, payload_hash)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE key_hash = customer_submissions.key_hash
            """,
            (source, key_hash, payload_hash),
        )
        cursor.execute(
            """SELECT payload_hash, customer_id FROM customer_submissions
               WHERE source = %s AND key_hash = %s FOR UPDATE""",
            (source, key_hash),
        )
        saved_hash, customer_id = cursor.fetchone()
        if not hmac.compare_digest(saved_hash, payload_hash):
            raise SubmissionConflict
        if customer_id is not None:
            connection.commit()
            return customer_id, True

        cursor.execute(
            "INSERT INTO customers (name, email, dob, age, balance) VALUES (%s, %s, %s, %s, %s)",
            (customer.name, customer.email, customer.dob, customer.age, customer.balance),
        )
        customer_id = cursor.lastrowid
        cursor.execute(
            "UPDATE customer_submissions SET customer_id = %s WHERE source = %s AND key_hash = %s",
            (customer_id, source, key_hash),
        )
        connection.commit()
        return customer_id, False


def consume_rate_limit(source, client, secret):
    window_seconds = 60
    now = int(time.time())
    window = now - now % window_seconds
    scopes = [(f"{source}:all", 120 if source == "google" else 90)]
    if source == "web":
        scopes.append((f"web:client:{client}", 10))

    with closing(connect()) as connection, connection.cursor() as cursor:
        for scope, limit in scopes:
            scope_hash = hmac.digest(secret, scope.encode("utf-8"), "sha256")
            cursor.execute(
                """
                INSERT INTO customer_rate_limits (scope_hash, window_start) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE hits = LEAST(hits + 1, %s)
                """,
                (scope_hash, window, limit + 1),
            )
            cursor.execute(
                "SELECT hits FROM customer_rate_limits WHERE scope_hash = %s AND window_start = %s",
                (scope_hash, window),
            )
            if cursor.fetchone()[0] > limit:
                connection.commit()
                return window_seconds - (now - window)
        connection.commit()
        if now % 60 < 2:
            cursor.execute("DELETE FROM customer_rate_limits WHERE window_start < %s LIMIT 1000",
                           (window - 3600,))
            connection.commit()
    return 0
