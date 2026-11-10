"""
INF2003 Group Project - Team 37
webapp/db.py : connections and timing for both databases

MySQL  : one connection per request (opened lazily, closed at teardown).
MongoDB: one shared MongoClient for the whole app (pymongo pools it).

Every query the UI runs goes through mysql_query() / mysql_exec() /
mongo_timed(), which record how long it took. The page footer shows
those timings, so each page doubles as a small SQL vs NoSQL comparison.

Transactions: the connection runs with autocommit ON, so an ordinary
statement commits itself, which is what a single read or single write
wants. Anything that must be all-or-nothing goes through the
mysql_transaction() context manager below: it turns autocommit off for
the duration, commits at the end, and rolls back on any exception.
database/mysql/09_transactions.sql shows the same ideas in plain SQL.
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from flask import g
from pymongo import MongoClient, TEXT

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# The team's MongoDB modules and benchmark helpers are imported as-is,
# so the UI runs the same code that the report describes.
for folder in ("database/mongodb", "benchmark"):
    path = str(REPO_ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)


# ---------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------
def get_mysql():
    if "mysql_conn" not in g:
        g.mysql_conn = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "inf2003"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "moviedb"),
            autocommit=True,
            charset="utf8mb4",
        )
    return g.mysql_conn


def close_mysql(_exc=None):
    conn = g.pop("mysql_conn", None)
    if conn is not None:
        conn.close()


def _record(database, label, started, rows):
    g.setdefault("timings", []).append({
        "db": database,
        "label": label,
        "ms": (time.perf_counter() - started) * 1000.0,
        "rows": rows,
    })


def mysql_query(sql, params=(), label="query", one=False):
    """Run a SELECT with %s placeholders and return dict rows."""
    started = time.perf_counter()
    cur = get_mysql().cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        cur.close()
    _record("MySQL", label, started, len(rows))
    if one:
        return rows[0] if rows else None
    return rows


def mysql_exec(sql, params=(), label="write"):
    """Run INSERT / UPDATE / DELETE. Returns (rowcount, lastrowid)."""
    started = time.perf_counter()
    cur = get_mysql().cursor()
    try:
        cur.execute(sql, params)
        result = (cur.rowcount, cur.lastrowid)
    finally:
        cur.close()
    _record("MySQL", label, started, result[0])
    return result


@contextmanager
def mysql_transaction(label="transaction"):
    """Run several statements as one unit of work.

        with mysql_transaction("Import ratings") as cur:
            cur.execute(...)
            cur.execute(...)

    Leaving the block normally commits; raising anything rolls the whole
    thing back, so a half-finished change can never be left behind. The
    cursor is a plain (non-dictionary) cursor, and SAVEPOINT /
    ROLLBACK TO SAVEPOINT can be issued on it for per-row error
    handling inside the transaction (see routes.import_ratings).

    Timings are recorded like any other call, so the page footer shows
    how long the transaction took.
    """
    conn = get_mysql()
    started = time.perf_counter()
    was_autocommit = conn.autocommit
    conn.autocommit = False
    cur = conn.cursor()
    committed = False
    try:
        yield cur
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.autocommit = was_autocommit
        _record("MySQL", f"{label} ({'COMMIT' if committed else 'ROLLBACK'})",
                started, 0)


# ---------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------
_mongo_client = None


def get_mongo():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            os.getenv("MONGODB_URI", "mongodb://localhost:27017/"),
            serverSelectionTimeoutMS=3000,
        )
    return _mongo_client[os.getenv("MONGODB_DATABASE", "movie_app")]


def mongo_timed(label, fn):
    """Run fn() (a pymongo call that returns a list/doc) and time it."""
    started = time.perf_counter()
    result = fn()
    if isinstance(result, list):
        rows = len(result)
    else:
        rows = 0 if result is None else 1
    _record("MongoDB", label, started, rows)
    return result


def ensure_mongo_indexes():
    """Text index used by the overview search page.

    MongoDB allows one text index per collection; create_index is a no-op
    if it already exists, so this is safe to call at every start-up.
    """
    get_mongo().movie_features.create_index(
        [("overview", TEXT)], name="idx_overview_text"
    )
