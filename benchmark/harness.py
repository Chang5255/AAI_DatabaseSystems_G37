"""
INF2003 Group Project - Team 37
benchmark/harness.py : shared timing and reporting helpers

Both the MySQL and the MongoDB benchmarks should use time_call() and
summarise() from this file, so every number in the report is measured
the same way:
  * the connection is opened once, outside the timed code
  * each timed call includes fetching every result row, not just
    sending the query (MongoDB's find_one also returns the full document)
  * warm-up calls are run first and discarded, so results describe a
    warm cache, which is what a running application sees
  * time.perf_counter() is used: it is monotonic and has sub-microsecond
    resolution
"""

import csv
import json
import math
import os
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_DIR = BENCH_DIR / "results"
SAMPLE_FILE = BENCH_DIR / "sample_ids.json"

load_dotenv(REPO_ROOT / ".env")


# ---------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------
def mysql_connect(database=None, autocommit=True):
    """Open a MySQL connection using the MYSQL_* settings in .env."""
    import mysql.connector  # imported here so MongoDB-only users need not install it

    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "inf2003"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=database if database is not None else os.getenv("MYSQL_DATABASE", "moviedb"),
        autocommit=autocommit,
        charset="utf8mb4",
    )


# ---------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------
def time_call(fn, repeats=5, warmup=1):
    """Run fn() `warmup` times untimed, then `repeats` times timed.

    Returns (samples_ms, last_result). fn must do the full piece of work,
    including fetching all rows.
    """
    result = None
    for _ in range(warmup):
        result = fn()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples, result


def percentile(values, pct):
    """Nearest-rank percentile (pct in 0-100). Simple and easy to explain."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarise(samples_ms):
    """Summary statistics in milliseconds, rounded for the report."""
    return {
        "n": len(samples_ms),
        "mean_ms": round(statistics.fmean(samples_ms), 4),
        "median_ms": round(statistics.median(samples_ms), 4),
        "p95_ms": round(percentile(samples_ms, 95), 4),
        "min_ms": round(min(samples_ms), 4),
        "max_ms": round(max(samples_ms), 4),
        "stdev_ms": round(statistics.stdev(samples_ms), 4) if len(samples_ms) > 1 else 0.0,
    }


# ---------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------
def write_csv(filename, rows):
    """Write a list of dicts to benchmark/results/<filename>."""
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / filename
    if not rows:
        path.write_text("")
        return path
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_text(filename, text):
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / filename
    path.write_text(text, encoding="utf-8")
    return path


def print_table(rows, columns):
    """Print a small aligned table to the console."""
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in columns))


# ---------------------------------------------------------------------
# Shared sample of ids (MySQL and MongoDB must test the same ids)
# ---------------------------------------------------------------------
def load_samples():
    with SAMPLE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_samples(samples):
    with SAMPLE_FILE.open("w", encoding="utf-8") as f:
        json.dump(samples, f, indent=2)
