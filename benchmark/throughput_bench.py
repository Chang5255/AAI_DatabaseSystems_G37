"""
INF2003 Group Project - Team 37
benchmark/throughput_bench.py : Experiment 4, throughput under load

Experiments 1 to 3 measure LATENCY: how long one operation takes when
nothing else is happening. That is not what a running application sees.
The project brief asks about speed AND throughput, which is a different
question:

    how many operations per second can each database sustain, and what
    happens to response time as more clients arrive at once?

Method
  * N worker threads, each with its OWN connection (a MySQL connection
    is not thread-safe, and pymongo pools per client), run the same
    operation in a loop for DURATION seconds.
  * Every worker records the latency of every operation it completes.
  * Throughput is the total completed operations divided by the wall
    time of the measurement window; latency is reported as median and
    p95 over all of them.
  * The run repeats at 1, 2, 4, 8 and 16 concurrent workers, so the
    output is a curve, not a single number. The interesting part of the
    curve is where throughput stops rising while p95 keeps climbing:
    that is the point where the database is saturated and extra clients
    only queue.

Workloads
  read-film     MySQL   : the two cast/crew joins (Experiment 1 T1)
                MongoDB : find_one on movie_id
  write-rating  MySQL   : INSERT ... ON DUPLICATE KEY UPDATE on ratings,
                          which is what the web app's rating button
                          runs, including the audit trigger from
                          08_triggers.sql
                MongoDB : insert_one into user_activity_logs, the
                          append-only collection

The write workloads are NOT the same operation, and the report should
say so: an upsert into a constrained, indexed, audited, transactional
table is doing more work than appending a document. The comparison
that is fair is each database against ITSELF as concurrency rises.

Safety: writes go to one throw-away user in each database, created at
the start and deleted at the end. Nothing else is touched.

Usage (from the repo root, both databases running):
    python benchmark/throughput_bench.py
    python benchmark/throughput_bench.py --duration 5 --workers 1 4 16
    python benchmark/throughput_bench.py --only read-film

Outputs in benchmark/results/:
    throughput_summary.csv   one row per workload x worker count
"""

import argparse
import os
import platform
import random
import threading
import time
from datetime import datetime, timezone

import harness
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(harness.REPO_ROOT / ".env")

BENCH_USERNAME = "bench_throughput_g37"
WORKER_COUNTS = [1, 2, 4, 8, 16]
DURATION = 5.0


# =====================================================================
# Set-up and tear-down of the throw-away user
# =====================================================================
def bench_user_id(conn):
    """Create (or reuse) the benchmark user and return its id."""
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (BENCH_USERNAME,))
    row = cur.fetchone()
    if row:
        user_id = row[0]
    else:
        cur.execute("INSERT INTO users (username, display_name) VALUES (%s, %s)",
                    (BENCH_USERNAME, "Throughput benchmark"))
        user_id = cur.lastrowid
    cur.close()
    return user_id


def remove_bench_user(conn, mongo_db, user_id):
    cur = conn.cursor()
    # ON DELETE CASCADE takes the ratings with the user.
    cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    # The audit rows the triggers wrote are deliberately not deleted by
    # the cascade; remove them too, if this account may.
    try:
        cur.execute("DELETE FROM rating_audit WHERE user_id = %s", (user_id,))
    except Exception as exc:                      # no privilege, or no table
        print(f"  (left rating_audit rows in place: {exc})")
    cur.close()
    mongo_db.user_activity_logs.delete_many({"user_id": user_id})


# =====================================================================
# Workloads. Each returns (setup, operation) where setup() builds one
# per-thread context and operation(context) does one unit of work.
# =====================================================================
SQL_CAST = """
    SELECT p.person_id, p.name, mc.character_name, mc.cast_order
      FROM movie_cast mc JOIN people p ON p.person_id = mc.person_id
     WHERE mc.movie_id = %s ORDER BY mc.cast_order
"""
SQL_CREW = """
    SELECT p.person_id, p.name, cr.department, cr.job
      FROM movie_crew cr JOIN people p ON p.person_id = cr.person_id
     WHERE cr.movie_id = %s
"""
SQL_UPSERT = """
    INSERT INTO ratings (user_id, movie_id, rating_value)
    VALUES (%s, %s, %s) AS new
    ON DUPLICATE KEY UPDATE rating_value = new.rating_value
"""


def mysql_read_workload(movie_ids):
    def setup():
        conn = harness.mysql_connect()
        return conn, conn.cursor()

    def operation(context, rng):
        _, cur = context
        movie_id = rng.choice(movie_ids)
        cur.execute(SQL_CAST, (movie_id,))
        cur.fetchall()
        cur.execute(SQL_CREW, (movie_id,))
        cur.fetchall()

    def teardown(context):
        conn, cur = context
        cur.close()
        conn.close()

    return setup, operation, teardown


def mongo_read_workload(movie_ids):
    def setup():
        client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
        return client, client[os.getenv("MONGODB_DATABASE", "movie_app")].movie_features

    def operation(context, rng):
        _, coll = context
        coll.find_one({"movie_id": rng.choice(movie_ids)},
                      {"_id": 0, "cast": 1, "crew": 1})

    def teardown(context):
        context[0].close()

    return setup, operation, teardown


def mysql_write_workload(movie_ids, user_id):
    def setup():
        conn = harness.mysql_connect()
        return conn, conn.cursor()

    def operation(context, rng):
        _, cur = context
        cur.execute(SQL_UPSERT, (user_id, rng.choice(movie_ids),
                                 rng.choice([0.5, 1.0, 2.0, 3.0, 4.0, 5.0])))

    def teardown(context):
        conn, cur = context
        cur.close()
        conn.close()

    return setup, operation, teardown


def mongo_write_workload(movie_ids, user_id):
    def setup():
        client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
        db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
        return client, db.user_activity_logs

    def operation(context, rng):
        _, coll = context
        coll.insert_one({
            "user_id": user_id,
            "event_type": "view_movie",
            "movie_id": rng.choice(movie_ids),
            "timestamp": datetime.now(timezone.utc),
            "metadata": {"source": "throughput_bench"},
        })

    def teardown(context):
        context[0].close()

    return setup, operation, teardown


# =====================================================================
# The driver
# =====================================================================
def run_level(setup, operation, teardown, workers, duration):
    """Run `workers` threads for `duration` seconds. Returns (ops, latencies_ms)."""
    start_barrier = threading.Barrier(workers + 1)
    stop_at = [0.0]
    results = [None] * workers
    errors = []

    def worker(index):
        try:
            context = setup()
        except Exception as exc:                  # could not connect
            errors.append(repr(exc))
            results[index] = []
            start_barrier.abort()                 # never leave main waiting
            return
        rng = random.Random(1000 + index)
        latencies = []
        try:
            start_barrier.wait(timeout=60)        # all threads start together
            while time.perf_counter() < stop_at[0]:
                t0 = time.perf_counter()
                try:
                    operation(context, rng)
                except Exception as exc:
                    errors.append(repr(exc))
                    break
                latencies.append((time.perf_counter() - t0) * 1000.0)
        except threading.BrokenBarrierError:
            pass
        finally:
            teardown(context)
        results[index] = latencies

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(workers)]
    for thread in threads:
        thread.start()

    # Connections are open and every thread is waiting: start the clock.
    stop_at[0] = time.perf_counter() + duration
    wall_start = time.perf_counter()
    try:
        start_barrier.wait(timeout=60)
    except threading.BrokenBarrierError:
        pass                                      # a worker failed to connect
    for thread in threads:
        thread.join(timeout=duration + 60)
    wall = time.perf_counter() - wall_start

    latencies = [ms for per_thread in results if per_thread for ms in per_thread]
    return latencies, wall, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=DURATION,
                        help=f"seconds per concurrency level (default {DURATION})")
    parser.add_argument("--workers", type=int, nargs="+", default=WORKER_COUNTS,
                        help="concurrency levels to test")
    parser.add_argument("--only", choices=["read-film", "write-rating"],
                        help="run just one workload")
    args = parser.parse_args()

    conn = harness.mysql_connect()
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    mysql_version = cur.fetchone()[0]
    cur.close()

    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    mongo_db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
    mongo_version = client.server_info()["version"]

    samples = harness.load_samples() if harness.SAMPLE_FILE.exists() else None
    if samples is None:
        print("sample_ids.json is missing; run mysql_bench.py first.")
        return
    movie_ids = samples["movie_ids"]

    user_id = bench_user_id(conn)
    print(f"MySQL {mysql_version} | MongoDB {mongo_version}")
    print(f"{args.duration:.0f}s per level, levels: {args.workers}")
    print(f"writes go to throw-away user {user_id} ({BENCH_USERNAME}), "
          f"deleted at the end\n")

    workloads = []
    if args.only in (None, "read-film"):
        workloads += [
            ("read-film", "MySQL", "2 joins on movie_cast / movie_crew",
             mysql_read_workload(movie_ids)),
            ("read-film", "MongoDB", "find_one on movie_id (embedded arrays)",
             mongo_read_workload(movie_ids)),
        ]
    if args.only in (None, "write-rating"):
        workloads += [
            ("write-rating", "MySQL", "upsert into ratings (constraints + audit trigger)",
             mysql_write_workload(movie_ids, user_id)),
            ("write-rating", "MongoDB", "insert_one into user_activity_logs",
             mongo_write_workload(movie_ids, user_id)),
        ]

    rows = []
    try:
        for workload, database, method, (setup, operation, teardown) in workloads:
            print(f"{workload} / {database}: {method}")
            for workers in args.workers:
                latencies, wall, errors = run_level(setup, operation, teardown,
                                                    workers, args.duration)
                if not latencies:
                    print(f"  {workers:>2} workers: no operations completed "
                          f"({errors[:1]})")
                    continue
                stats = harness.summarise(latencies)
                throughput = len(latencies) / wall
                rows.append({
                    "workload": workload,
                    "database": database,
                    "method": method,
                    "workers": workers,
                    "duration_s": round(wall, 2),
                    "operations": len(latencies),
                    "ops_per_second": round(throughput, 1),
                    "median_ms": stats["median_ms"],
                    "p95_ms": stats["p95_ms"],
                    "mean_ms": stats["mean_ms"],
                    "max_ms": stats["max_ms"],
                    "errors": len(errors),
                })
                print(f"  {workers:>2} workers: {throughput:>9.1f} ops/s   "
                      f"median {stats['median_ms']:>7.2f} ms   "
                      f"p95 {stats['p95_ms']:>7.2f} ms"
                      + (f"   {len(errors)} error(s)" if errors else ""))
            print()
    finally:
        remove_bench_user(conn, mongo_db, user_id)
        conn.close()
        client.close()

    if not rows:
        print("Nothing measured.")
        return

    header = (f"MySQL {mysql_version} | MongoDB {mongo_version}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"CPU count {os.cpu_count()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"duration={args.duration}s levels={args.workers}\n")
    harness.write_text("throughput_notes.txt", header)
    path = harness.write_csv("throughput_summary.csv", rows)

    print("Peak throughput per workload")
    for workload in dict.fromkeys(row["workload"] for row in rows):
        for database in ("MySQL", "MongoDB"):
            matching = [row for row in rows
                        if row["workload"] == workload and row["database"] == database]
            if matching:
                best = max(matching, key=lambda row: row["ops_per_second"])
                print(f"  {workload:<13} {database:<8} {best['ops_per_second']:>9.1f} "
                      f"ops/s at {best['workers']} worker(s), "
                      f"p95 {best['p95_ms']:.1f} ms")
    print(f"\nResults written to {path.parent}")
    print("A note on reading these numbers: every worker is a Python thread in one\n"
          "process, so the client itself is limited by the GIL. The curve still shows\n"
          "where each database saturates, but the absolute ceiling is partly ours.")


if __name__ == "__main__":
    main()
