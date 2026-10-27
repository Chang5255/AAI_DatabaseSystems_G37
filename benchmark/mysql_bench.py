"""
INF2003 Group Project - Team 37
benchmark/mysql_bench.py : Experiment 1, MySQL side

Main SQL-vs-MongoDB comparison: the same cast/crew data, stored as
normalised tables (MySQL) vs embedded arrays (MongoDB movie_features).

Tasks timed here (the MongoDB benchmark must run the same four, on the
same ids from sample_ids.json, with harness.time_call):

  T1 movie_cast_crew_2q   one movie's cast and crew, two SQL queries
                          (cast join, crew join)
                          MongoDB: find_one({"movie_id": id},
                                            {"cast": 1, "crew": 1})
  T2 movie_cast_crew_json the same, as ONE SQL statement returning one
                          JSON document (closest match to find_one)
  T3 films_by_actor       every film one actor appears in
                          MongoDB: find({"cast.person_id": pid},
                                        {"movie_id": 1, "cast.$": 1})
                          (needs an index on cast.person_id)
  T4 rename_person        change one person's name
                          MySQL updates 1 row in people. MongoDB must
                          update every embedded copy in cast[] and crew[]
                          (update_many with array filters).
                          The name is restored after each run, untimed.

T1 and T2 favour MongoDB's design (one document holds everything).
T3 and T4 favour the relational design. Reporting all four keeps the
comparison balanced.

Usage (from the repo root, MySQL running, .env filled in):
    python benchmark/mysql_bench.py                 # 300 ids, 5 repeats
    python benchmark/mysql_bench.py --samples 100 --repeats 10
    python benchmark/mysql_bench.py --resample      # draw new sample ids

Outputs in benchmark/results/:
    mysql_experiment1_summary.csv   mean / median / p95 per task
    mysql_experiment1_raw.csv       every timed call
    mysql_experiment1_plans.txt     EXPLAIN ANALYZE for one example each
"""

import argparse
import json
import platform
import random
from datetime import datetime

import harness

BENCH_TAG = " [bench]"

SQL_CAST = """
    SELECT p.person_id, p.name, mc.character_name, mc.cast_order
      FROM movie_cast mc
      JOIN people p ON p.person_id = mc.person_id
     WHERE mc.movie_id = %s
     ORDER BY mc.cast_order
"""

SQL_CREW = """
    SELECT p.person_id, p.name, cr.department, cr.job
      FROM movie_crew cr
      JOIN people p ON p.person_id = cr.person_id
     WHERE cr.movie_id = %s
"""

SQL_JSON = """
    SELECT JSON_OBJECT(
             'movie_id', m.movie_id,
             'cast', (SELECT JSON_ARRAYAGG(JSON_OBJECT(
                               'person_id', p.person_id, 'name', p.name,
                               'character', mc.character_name, 'order', mc.cast_order))
                        FROM movie_cast mc JOIN people p ON p.person_id = mc.person_id
                       WHERE mc.movie_id = m.movie_id),
             'crew', (SELECT JSON_ARRAYAGG(JSON_OBJECT(
                               'person_id', p.person_id, 'name', p.name,
                               'department', cr.department, 'job', cr.job))
                        FROM movie_crew cr JOIN people p ON p.person_id = cr.person_id
                       WHERE cr.movie_id = m.movie_id))
      FROM movies m
     WHERE m.movie_id = %s
"""

# Same output as MongoDB's {"movie_id": 1, "cast.$": 1} projection:
# the film id plus this actor's cast entry. movie_features has no title,
# so the SQL side does not join movies either.
SQL_FILMS_BY_ACTOR = """
    SELECT movie_id, character_name, cast_order
      FROM movie_cast
     WHERE person_id = %s
"""

SQL_RENAME = "UPDATE people SET name = %s WHERE person_id = %s"

TASKS = {
    "T1_movie_cast_crew_2q": "One movie's cast + crew, two SQL queries",
    "T2_movie_cast_crew_json": "One movie's cast + crew, one SQL statement returning JSON",
    "T3_films_by_actor": "All films for one actor",
    "T4_rename_person": "Rename one person (1 row in people)",
}


# ---------------------------------------------------------------------
# Sample ids shared with the MongoDB benchmark
# ---------------------------------------------------------------------
def build_samples(cur, k, seed):
    rng = random.Random(seed)
    cur.execute("SELECT DISTINCT movie_id FROM movie_cast ORDER BY movie_id")
    movie_ids = [r[0] for r in cur.fetchall()]
    cur.execute("""
        SELECT person_id FROM movie_cast
         GROUP BY person_id HAVING COUNT(*) >= 3
         ORDER BY person_id
    """)
    actor_ids = [r[0] for r in cur.fetchall()]
    samples = {
        "seed": seed,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "Movies with at least one cast credit; actors with at least 3 cast credits.",
        "movie_ids": sorted(rng.sample(movie_ids, min(k, len(movie_ids)))),
        "actor_person_ids": sorted(rng.sample(actor_ids, min(k, len(actor_ids)))),
    }
    harness.save_samples(samples)
    return samples


# ---------------------------------------------------------------------
# Task implementations: each does the full work, including fetching
# ---------------------------------------------------------------------
def t1(cur, movie_id):
    cur.execute(SQL_CAST, (movie_id,))
    cast = cur.fetchall()
    cur.execute(SQL_CREW, (movie_id,))
    crew = cur.fetchall()
    return len(cast) + len(crew)


def t2(cur, movie_id):
    cur.execute(SQL_JSON, (movie_id,))
    doc = json.loads(cur.fetchone()[0])  # decode, as pymongo decodes BSON
    return len(doc["cast"] or []) + len(doc["crew"] or [])


def t3(cur, person_id):
    cur.execute(SQL_FILMS_BY_ACTOR, (person_id,))
    return len(cur.fetchall())


def cleanup_tags(cur):
    """Undo any rename left behind by an interrupted run."""
    cur.execute(
        "UPDATE people SET name = LEFT(name, CHAR_LENGTH(name) - %s) WHERE name LIKE %s",
        (len(BENCH_TAG), "%" + BENCH_TAG),
    )


def run_rename(cur, person_ids, repeats, warmup):
    """T4. Custom loop: every timed rename is followed by an untimed restore."""
    import time

    raw = []
    for pid in person_ids:
        cur.execute("SELECT name FROM people WHERE person_id = %s", (pid,))
        original = cur.fetchone()[0]
        try:
            for run in range(warmup + repeats):
                t0 = time.perf_counter()
                cur.execute(SQL_RENAME, (original + BENCH_TAG, pid))  # autocommit
                ms = (time.perf_counter() - t0) * 1000.0
                rows = cur.rowcount
                cur.execute(SQL_RENAME, (original, pid))               # restore, untimed
                if run >= warmup:
                    raw.append({"task": "T4_rename_person", "sample_id": pid,
                                "run": run - warmup + 1, "ms": round(ms, 4),
                                "rows": rows})
        finally:
            cur.execute(SQL_RENAME, (original, pid))
    return raw


def run_task(cur, task, fn, ids, repeats, warmup):
    raw = []
    for sid in ids:
        samples, rows = harness.time_call(lambda: fn(cur, sid), repeats, warmup)
        for i, ms in enumerate(samples, start=1):
            raw.append({"task": task, "sample_id": sid, "run": i,
                        "ms": round(ms, 4), "rows": rows})
    return raw


# ---------------------------------------------------------------------
# Execution plans for the report (one example per task)
# ---------------------------------------------------------------------
def capture_plans(cur, movie_id, person_id):
    out = []

    def explain(label, sql, params):
        try:
            cur.execute("EXPLAIN ANALYZE " + sql, params)
            plan = cur.fetchone()[0]
        except Exception as exc:  # e.g. EXPLAIN ANALYZE on UPDATE in older 8.0
            cur.execute("EXPLAIN FORMAT=TREE " + sql, params)
            plan = cur.fetchone()[0] + f"\n(EXPLAIN ANALYZE unavailable: {exc})"
        out.append(f"===== {label} =====\n{sql.strip()}\nparams={params}\n\n{plan}\n")

    explain("T1 cast query", SQL_CAST, (movie_id,))
    explain("T1 crew query", SQL_CREW, (movie_id,))
    explain("T2 JSON query", SQL_JSON, (movie_id,))
    explain("T3 films by actor", SQL_FILMS_BY_ACTOR, (person_id,))
    # MySQL 8.0 cannot show UPDATE as a tree plan, so use classic EXPLAIN
    # (plan only; the UPDATE is not executed).
    cur.execute("EXPLAIN " + SQL_RENAME, ("x", person_id))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    table = "\n".join(f"  {c:<14} {v}" for c, v in zip(cols, row))
    out.append(f"===== T4 rename (plan only, not executed) =====\n{SQL_RENAME}\n"
               f"params=('<new name>', {person_id})\n\n{table}\n")
    return "\n".join(out)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--samples", type=int, default=300, help="ids per task (default 300)")
    ap.add_argument("--repeats", type=int, default=5, help="timed runs per id (default 5)")
    ap.add_argument("--warmup", type=int, default=1, help="untimed runs per id (default 1)")
    ap.add_argument("--seed", type=int, default=37, help="random seed for sampling")
    ap.add_argument("--resample", action="store_true", help="draw new ids even if sample_ids.json exists")
    args = ap.parse_args()

    conn = harness.mysql_connect()
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    cleanup_tags(cur)

    if args.resample or not harness.SAMPLE_FILE.exists():
        samples = build_samples(cur, args.samples, args.seed)
        print(f"Wrote new sample ids to {harness.SAMPLE_FILE}")
    else:
        samples = harness.load_samples()
        print(f"Using existing sample ids from {harness.SAMPLE_FILE}")
    movie_ids = samples["movie_ids"][: args.samples]
    actor_ids = samples["actor_person_ids"][: args.samples]

    print(f"MySQL {version} | {len(movie_ids)} movies, {len(actor_ids)} actors | "
          f"{args.warmup} warm-up + {args.repeats} timed runs each\n")

    raw = []
    try:
        for task, fn, ids in [
            ("T1_movie_cast_crew_2q", t1, movie_ids),
            ("T2_movie_cast_crew_json", t2, movie_ids),
            ("T3_films_by_actor", t3, actor_ids),
        ]:
            print(f"running {task} ...")
            raw += run_task(cur, task, fn, ids, args.repeats, args.warmup)
        print("running T4_rename_person ...")
        raw += run_rename(cur, actor_ids, args.repeats, args.warmup)
    finally:
        cleanup_tags(cur)

    summary = []
    for task, desc in TASKS.items():
        task_rows = [r for r in raw if r["task"] == task]
        stats = harness.summarise([r["ms"] for r in task_rows])
        summary.append({
            "database": "MySQL",
            "task": task,
            "description": desc,
            **stats,
            "avg_rows": round(sum(r["rows"] for r in task_rows) / len(task_rows), 1),
        })

    plans = capture_plans(cur, movie_ids[0], actor_ids[0])
    header = (f"MySQL {version}\nPython {platform.python_version()} on {platform.platform()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"samples={len(movie_ids)} repeats={args.repeats} warmup={args.warmup}\n\n")
    harness.write_text("mysql_experiment1_plans.txt", header + plans)
    harness.write_csv("mysql_experiment1_raw.csv", raw)
    path = harness.write_csv("mysql_experiment1_summary.csv", summary)

    print()
    harness.print_table(summary, ["task", "n", "mean_ms", "median_ms", "p95_ms", "avg_rows"])
    print(f"\nResults written to {path.parent}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
