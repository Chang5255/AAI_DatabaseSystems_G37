"""
INF2003 Group Project - Team 37
benchmark/txn_cost_bench.py : Experiment 5, what a commit costs

Lecture 7 says a transaction is not free: before a commit can return,
the redo log has to be on disk, or durability is a lie. This script
measures that, by writing the same N rows five different ways.

MySQL
  1. autocommit per row   N statements, N commits. The naive loop that
                          most applications start with.
  2. one transaction      N statements, 1 commit.
  3. executemany batches  N rows sent in batches of BATCH, 1 commit.
                          The driver rewrites them into multi-row
                          INSERTs, so this also cuts round trips.
MongoDB
  4. insert_one per doc   N round trips, each acknowledged.
  5. insert_many batches  N documents in batches of BATCH.

The MySQL and MongoDB halves are NOT the same operation and must not
be reported as a head-to-head race: the rows go into a constrained,
indexed, audited table, while the documents are appended to a
collection. What each half shows on its own is the real lesson - the
gap between 1 and 2 is the cost of committing, and the gap between 2
and 3 is the cost of round trips.

MongoDB's writes are also acknowledged by default (w=1) but are not
journalled on every write unless j=true is asked for, so the script
measures the default and, optionally, j=true for a fair comparison
with MySQL's default durability.

Safety: everything is written under one throw-away user and deleted
afterwards. MySQL ratings need distinct film ids (the primary key is
(user_id, movie_id)), so N is capped at the number of films available.

Usage (from the repo root):
    python benchmark/txn_cost_bench.py
    python benchmark/txn_cost_bench.py --rows 2000 --batch 200 --repeats 3

Outputs in benchmark/results/:
    txn_cost_summary.csv
"""

import argparse
import os
import platform
import random
import time
from datetime import datetime, timezone

import harness
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.write_concern import WriteConcern

load_dotenv(harness.REPO_ROOT / ".env")

BENCH_USERNAME = "bench_txncost_g37"
ROWS = 2000
BATCH = 200
REPEATS = 3

# A plain INSERT, not the application's upsert. Two reasons: all three
# MySQL variants must run the SAME statement or the comparison means
# nothing, and mysql-connector can only rewrite a plain
# "INSERT ... VALUES (...)" into the multi-row form that makes
# executemany worth using. Every run starts from an empty set of rows,
# so no duplicate key can arise.
SQL_INSERT = """
    INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (%s, %s, %s)
"""


# ---------------------------------------------------------------------
def setup_user(conn):
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (BENCH_USERNAME,))
    row = cur.fetchone()
    if row:
        user_id = row[0]
    else:
        cur.execute("INSERT INTO users (username, display_name) VALUES (%s, %s)",
                    (BENCH_USERNAME, "Transaction cost benchmark"))
        user_id = cur.lastrowid
    cur.close()
    return user_id


def clear_rows(conn, user_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM ratings WHERE user_id = %s", (user_id,))
    try:
        cur.execute("DELETE FROM rating_audit WHERE user_id = %s", (user_id,))
    except Exception:
        pass                                   # no privilege, or no audit table
    cur.close()


def drop_user(conn, user_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    try:
        cur.execute("DELETE FROM rating_audit WHERE user_id = %s", (user_id,))
    except Exception:
        pass
    cur.close()


def film_ids(conn, count):
    cur = conn.cursor()
    cur.execute("SELECT movie_id FROM movies ORDER BY movie_id")
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    return ids[:count]


def payload(ids, user_id, rng):
    return [(user_id, movie_id, rng.choice([0.5, 1.5, 2.5, 3.5, 4.5, 5.0]))
            for movie_id in ids]


# ---------------------------------------------------------------------
# MySQL variants. Each opens its own connection so autocommit is set
# before anything is written, and each returns the elapsed seconds.
# ---------------------------------------------------------------------
def mysql_autocommit_per_row(rows):
    conn = harness.mysql_connect(autocommit=True)
    cur = conn.cursor()
    started = time.perf_counter()
    for row in rows:
        cur.execute(SQL_INSERT, row)           # each statement commits itself
    elapsed = time.perf_counter() - started
    cur.close()
    conn.close()
    return elapsed


def mysql_single_transaction(rows):
    conn = harness.mysql_connect(autocommit=False)
    cur = conn.cursor()
    started = time.perf_counter()
    for row in rows:
        cur.execute(SQL_INSERT, row)
    conn.commit()                              # one commit for all of them
    elapsed = time.perf_counter() - started
    cur.close()
    conn.close()
    return elapsed


def mysql_executemany(rows, batch):
    conn = harness.mysql_connect(autocommit=False)
    cur = conn.cursor()
    started = time.perf_counter()
    for start in range(0, len(rows), batch):
        cur.executemany(SQL_INSERT, rows[start:start + batch])
    conn.commit()
    elapsed = time.perf_counter() - started
    cur.close()
    conn.close()
    return elapsed


# ---------------------------------------------------------------------
# MongoDB variants
# ---------------------------------------------------------------------
def mongo_documents(ids, user_id):
    now = datetime.now(timezone.utc)
    return [{"user_id": user_id, "event_type": "view_movie", "movie_id": movie_id,
             "timestamp": now, "metadata": {"source": "txn_cost_bench"}}
            for movie_id in ids]


def mongo_insert_one(collection, documents):
    started = time.perf_counter()
    for document in documents:
        collection.insert_one(dict(document))
    return time.perf_counter() - started


def mongo_insert_many(collection, documents, batch):
    started = time.perf_counter()
    for start in range(0, len(documents), batch):
        collection.insert_many([dict(d) for d in documents[start:start + batch]],
                               ordered=False)
    return time.perf_counter() - started


# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rows", type=int, default=ROWS,
                        help=f"rows / documents per run (default {ROWS})")
    parser.add_argument("--batch", type=int, default=BATCH,
                        help=f"batch size for the batched variants (default {BATCH})")
    parser.add_argument("--repeats", type=int, default=REPEATS,
                        help=f"runs per variant (default {REPEATS})")
    parser.add_argument("--journalled", action="store_true",
                        help="also measure MongoDB with j=true (journal on every write)")
    args = parser.parse_args()

    conn = harness.mysql_connect(autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    mysql_version = cur.fetchone()[0]
    cur.execute("SELECT @@innodb_flush_log_at_trx_commit")
    flush_setting = cur.fetchone()[0]
    cur.close()

    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    mongo_db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
    mongo_version = client.server_info()["version"]

    user_id = setup_user(conn)
    ids = film_ids(conn, args.rows)
    rows_wanted = len(ids)
    if rows_wanted < args.rows:
        print(f"Only {rows_wanted} films exist, so N = {rows_wanted} "
              f"(ratings are keyed on (user_id, movie_id), so the ids must differ)\n")

    rng = random.Random(37)
    sql_rows = payload(ids, user_id, rng)
    documents = mongo_documents(ids, user_id)
    activity = mongo_db.user_activity_logs

    print(f"MySQL {mysql_version} (innodb_flush_log_at_trx_commit = {flush_setting}) "
          f"| MongoDB {mongo_version}")
    print(f"{rows_wanted} rows per run, batch {args.batch}, "
          f"{args.repeats} run(s) per variant\n")

    variants = [
        ("MySQL", "1. autocommit per row", f"{rows_wanted} statements, {rows_wanted} commits",
         lambda: mysql_autocommit_per_row(sql_rows),
         lambda: clear_rows(conn, user_id)),
        ("MySQL", "2. one transaction", f"{rows_wanted} statements, 1 commit",
         lambda: mysql_single_transaction(sql_rows),
         lambda: clear_rows(conn, user_id)),
        ("MySQL", f"3. executemany({args.batch})", "multi-row INSERTs, 1 commit",
         lambda: mysql_executemany(sql_rows, args.batch),
         lambda: clear_rows(conn, user_id)),
        ("MongoDB", "4. insert_one per document", f"{rows_wanted} round trips (w=1)",
         lambda: mongo_insert_one(activity, documents),
         lambda: activity.delete_many({"user_id": user_id})),
        ("MongoDB", f"5. insert_many({args.batch})", "batched round trips (w=1)",
         lambda: mongo_insert_many(activity, documents, args.batch),
         lambda: activity.delete_many({"user_id": user_id})),
    ]

    if args.journalled:
        journalled = activity.with_options(write_concern=WriteConcern(w=1, j=True))
        variants += [
            ("MongoDB", "6. insert_one, j=true", "journalled on every write",
             lambda: mongo_insert_one(journalled, documents),
             lambda: activity.delete_many({"user_id": user_id})),
            ("MongoDB", f"7. insert_many({args.batch}), j=true", "journalled, batched",
             lambda: mongo_insert_many(journalled, documents, args.batch),
             lambda: activity.delete_many({"user_id": user_id})),
        ]

    results = []
    try:
        for database, variant, method, run, reset in variants:
            print(f"{database} {variant} ...", end="", flush=True)
            timings = []
            for _ in range(args.repeats):
                reset()
                timings.append(run())
            reset()
            seconds = min(timings)              # best of N: least interference
            per_row_ms = seconds / rows_wanted * 1000.0
            results.append({
                "database": database,
                "variant": variant,
                "method": method,
                "rows": rows_wanted,
                "runs": args.repeats,
                "best_seconds": round(seconds, 4),
                "rows_per_second": round(rows_wanted / seconds, 1),
                "ms_per_row": round(per_row_ms, 4),
            })
            print(f" {seconds:.3f}s  ({rows_wanted / seconds:,.0f} rows/s)")
    finally:
        drop_user(conn, user_id)
        activity.delete_many({"user_id": user_id})
        conn.close()
        client.close()

    # Speed-up within each database, which is the comparison that holds.
    for database in ("MySQL", "MongoDB"):
        family = [row for row in results if row["database"] == database]
        if not family:
            continue
        slowest = max(row["best_seconds"] for row in family)
        for row in family:
            row["speedup_vs_slowest_in_db"] = round(slowest / row["best_seconds"], 1)

    print()
    harness.print_table(results, ["database", "variant", "best_seconds",
                                  "rows_per_second", "ms_per_row",
                                  "speedup_vs_slowest_in_db"])

    header = (f"MySQL {mysql_version}, innodb_flush_log_at_trx_commit={flush_setting}\n"
              f"MongoDB {mongo_version}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"rows={rows_wanted} batch={args.batch} repeats={args.repeats} "
              f"(best of {args.repeats} reported)\n\n"
              "innodb_flush_log_at_trx_commit = 1 means the log is flushed and\n"
              "fsynced at every commit, which is what makes variant 1 slow: the\n"
              "cost is one disk sync per row. Variant 2 pays it once. Variant 3\n"
              "also removes round trips. The MySQL and MongoDB figures are not a\n"
              "head-to-head race: different operations, different guarantees.\n")
    harness.write_text("txn_cost_notes.txt", header)
    path = harness.write_csv("txn_cost_summary.csv", results)
    print(f"\nResults written to {path.parent}")


if __name__ == "__main__":
    main()
