"""
INF2003 Group Project - Team 37
benchmark/ratings_scaling.py : Experiment 2, MySQL indexing at scale

Question: how does looking up all ratings for one film scale with table
size, with and without an index on movie_id?

Why a separate database
  The application's ratings table is small and must stay as it is.
  This script builds synthetic ratings in its own schema, moviedb_bench,
  so the application database (moviedb) is never modified. moviedb is
  only read, to get the list of real film ids and their TMDB scores.

What it does for each size (default 1k, 10k, 100k, 1M ratings)
  1. Create ratings_bench with PRIMARY KEY (user_id, movie_id) and no
     other index.
  2. Fill it with synthetic ratings (fixed seed, so reruns give the same
     data): 100 films per user, chosen with probability proportional to
     the film's TMDB vote_count (popular films get rated more); each
     score is centred on the film's TMDB average.
  3. Time   SELECT * FROM ratings_bench WHERE movie_id = %s   for a fixed
     sample of films -> no index.
  4. CREATE INDEX on movie_id (build time recorded), then time the same
     query -> with index.
  5. Control query WHERE user_id = %s, which always uses the primary key
     because user_id is its leftmost column.
  6. Record the plan MySQL chose (EXPLAIN FORMAT=JSON access_type:
     "ALL" = full table scan, "ref" = index lookup) and the estimated
     rows examined, plus data and index size on disk.

Usage (from the repo root):
    python benchmark/ratings_scaling.py
    python benchmark/ratings_scaling.py --sizes 1000 10000 100000
    python benchmark/ratings_scaling.py --keep     # keep moviedb_bench afterwards

The MySQL account needs rights on moviedb_bench:
    GRANT ALL PRIVILEGES ON moviedb_bench.* TO 'inf2003'@'localhost';

Outputs in benchmark/results/:
    ratings_scaling_summary.csv   one row per size x query x index state
    ratings_scaling_raw.csv       every timed call
    ratings_scaling_plans.txt     EXPLAIN ANALYZE before/after per size

The 1M size takes about 30 s to insert; a full run takes about 5 minutes,
most of it the unindexed lookups on 1M rows.
"""

import argparse
import json
import time
from datetime import datetime, timedelta

import numpy as np

import harness

BENCH_DB = "moviedb_bench"
RATINGS_PER_USER = 100
INSERT_BATCH = 10_000

DDL = """
CREATE TABLE ratings_bench (
  user_id      INT          NOT NULL,
  movie_id     INT          NOT NULL,
  rating_value DECIMAL(2,1) NOT NULL,
  rated_at     DATETIME     NOT NULL,
  PRIMARY KEY (user_id, movie_id)
) ENGINE=InnoDB
"""
Q_BY_MOVIE = "SELECT * FROM ratings_bench WHERE movie_id = %s"
Q_BY_USER = "SELECT * FROM ratings_bench WHERE user_id = %s"


def load_movies(cur):
    cur.execute(f"""
        SELECT movie_id, vote_count, vote_average
          FROM moviedb.movies
         WHERE vote_count > 0
         ORDER BY movie_id
    """)
    rows = cur.fetchall()
    ids = np.array([r[0] for r in rows])
    votes = np.array([float(r[1]) for r in rows])
    score = np.array([float(r[2]) for r in rows])
    return ids, votes / votes.sum(), score


def generate(size, movie_ids, weights, scores, seed):
    """Yield (user_id, movie_id, rating, rated_at) rows, `size` in total."""
    rng = np.random.default_rng(seed)
    users = max(1, size // RATINGS_PER_USER)
    start = datetime(2025, 10, 1)
    span_s = int(timedelta(days=365).total_seconds())
    index_of = {m: i for i, m in enumerate(movie_ids)}
    produced = 0
    for uid in range(1, users + 1):
        n = min(RATINGS_PER_USER, size - produced)
        if n <= 0:
            break
        chosen = rng.choice(movie_ids, size=n, replace=False, p=weights)
        centre = np.array([scores[index_of[m]] for m in chosen]) / 2.0  # 10-point -> 5-star
        stars = np.clip(np.round((centre + rng.normal(0, 0.8, n)) * 2) / 2, 0.5, 5.0)
        offsets = rng.integers(0, span_s, n)
        for m, s, off in zip(chosen, stars, offsets):
            yield (uid, int(m), float(s), start + timedelta(seconds=int(off)))
        produced += n


def fill(cur, conn, rows):
    sql = "INSERT INTO ratings_bench (user_id, movie_id, rating_value, rated_at) VALUES (%s, %s, %s, %s)"
    batch, total = [], 0
    for r in rows:
        batch.append(r)
        if len(batch) == INSERT_BATCH:
            cur.executemany(sql, batch)
            total += len(batch)
            batch.clear()
    if batch:
        cur.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total


def plan_info(cur, sql, param):
    """Access type and estimated rows examined, from EXPLAIN FORMAT=JSON."""
    cur.execute("EXPLAIN FORMAT=JSON " + sql, (param,))
    table = json.loads(cur.fetchone()[0])["query_block"]["table"]
    return table.get("access_type"), table.get("key"), table.get("rows_examined_per_scan")


def explain_analyze(cur, sql, param):
    cur.execute("EXPLAIN ANALYZE " + sql, (param,))
    return cur.fetchone()[0]


def time_lookups(cur, sql, ids, repeats, warmup):
    samples = []
    rows_total = 0
    for i in ids:
        def run():
            cur.execute(sql, (int(i),))
            return len(cur.fetchall())
        s, rows = harness.time_call(run, repeats, warmup)
        samples += s
        rows_total += rows
    return samples, rows_total / len(ids)


def table_size(cur):
    cur.execute("""
        SELECT DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'ratings_bench'
    """, (BENCH_DB,))
    d, i = cur.fetchone()
    return round(d / 1024 / 1024, 2), round(i / 1024 / 1024, 2)


def main():
    ap = argparse.ArgumentParser(description="Experiment 2: ratings lookup with and without an index, at several sizes")
    ap.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000, 1_000_000])
    ap.add_argument("--lookups", type=int, default=100, help="sampled ids per query (default 100)")
    ap.add_argument("--repeats", type=int, default=5, help="timed runs per id (default 5)")
    ap.add_argument("--warmup", type=int, default=1, help="untimed runs per id (default 1)")
    ap.add_argument("--seed", type=int, default=37)
    ap.add_argument("--keep", action="store_true", help="keep moviedb_bench at the end")
    args = ap.parse_args()

    conn = harness.mysql_connect(database="", autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    movie_ids, weights, scores = load_movies(cur)

    cur.execute(f"CREATE DATABASE IF NOT EXISTS {BENCH_DB} CHARACTER SET utf8mb4")
    cur.execute(f"USE {BENCH_DB}")

    summary, raw, plans = [], [], []
    rng = np.random.default_rng(args.seed + 1)

    for size in sorted(args.sizes):
        print(f"\n=== {size:,} ratings ===")
        cur.execute("DROP TABLE IF EXISTS ratings_bench")
        cur.execute(DDL)

        t0 = time.perf_counter()
        inserted = fill(cur, conn, generate(size, movie_ids, weights, scores, args.seed))
        load_s = time.perf_counter() - t0
        cur.execute("ANALYZE TABLE ratings_bench")
        cur.fetchall()
        print(f"  inserted {inserted:,} rows in {load_s:.1f} s")

        # Same films and users for both index states at this size.
        cur.execute("SELECT DISTINCT movie_id FROM ratings_bench")
        present = [r[0] for r in cur.fetchall()]
        film_ids = rng.choice(present, size=min(args.lookups, len(present)), replace=False)
        cur.execute("SELECT DISTINCT user_id FROM ratings_bench")
        all_users = [r[0] for r in cur.fetchall()]
        user_ids = rng.choice(all_users, size=min(args.lookups, len(all_users)), replace=False)
        example = int(film_ids[0])

        def record(query, index_state, sql, ids, extra=None):
            access, key, est_rows = plan_info(cur, sql, int(ids[0]))
            samples, avg_rows = time_lookups(cur, sql, ids, args.repeats, args.warmup)
            data_mb, index_mb = table_size(cur)
            row = {
                "table_rows": inserted,
                "query": query,
                "index_on_movie_id": index_state,
                "access_type": access,
                "key_used": key,
                "est_rows_examined": est_rows,
                "avg_rows_returned": round(avg_rows, 1),
                **harness.summarise(samples),
                "data_mb": data_mb,
                "index_mb": index_mb,
            }
            if extra:
                row.update(extra)
            summary.append(row)
            raw.extend({"table_rows": inserted, "query": query,
                        "index_on_movie_id": index_state, "ms": round(ms, 4)}
                       for ms in samples)
            print(f"  {query:<15} index={index_state:<3} {access:<4} "
                  f"est_rows={est_rows!s:<8} median={row['median_ms']} ms  p95={row['p95_ms']} ms")

        # No secondary index
        plans.append(f"===== {inserted:,} rows | WHERE movie_id = {example} | NO index =====\n"
                     f"{explain_analyze(cur, Q_BY_MOVIE, example)}")
        record("by_movie_id", "no", Q_BY_MOVIE, film_ids)
        record("by_user_id_pk", "no", Q_BY_USER, user_ids)

        # Build the index
        t0 = time.perf_counter()
        cur.execute("CREATE INDEX idx_bench_movie ON ratings_bench (movie_id)")
        build_ms = round((time.perf_counter() - t0) * 1000, 1)
        cur.execute("ANALYZE TABLE ratings_bench")
        cur.fetchall()
        print(f"  index built in {build_ms} ms")

        plans.append(f"===== {inserted:,} rows | WHERE movie_id = {example} | WITH index =====\n"
                     f"{explain_analyze(cur, Q_BY_MOVIE, example)}")
        record("by_movie_id", "yes", Q_BY_MOVIE, film_ids, {"index_build_ms": build_ms})

    if not args.keep:
        cur.execute(f"DROP DATABASE {BENCH_DB}")
        print(f"\nDropped {BENCH_DB} (use --keep to keep it)")

    header = (f"MySQL {version}\nRun at {datetime.now().isoformat(timespec='seconds')}\n"
              f"lookups={args.lookups} repeats={args.repeats} warmup={args.warmup} seed={args.seed}\n\n")
    harness.write_text("ratings_scaling_plans.txt", header + "\n\n".join(plans) + "\n")
    harness.write_csv("ratings_scaling_raw.csv", raw)
    # Same columns in every row so the CSV is rectangular.
    for r in summary:
        r.setdefault("index_build_ms", "")
    path = harness.write_csv("ratings_scaling_summary.csv", summary)

    print()
    harness.print_table(
        [r for r in summary if r["query"] == "by_movie_id"],
        ["table_rows", "index_on_movie_id", "access_type", "est_rows_examined",
         "median_ms", "p95_ms", "index_build_ms"],
    )
    print(f"\nResults written to {path.parent}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
