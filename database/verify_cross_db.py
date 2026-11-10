"""
INF2003 Group Project - Team 37
database/verify_cross_db.py : the integrity check neither database can do

WHY THIS FILE EXISTS
--------------------
03_verify.sql proves MySQL is internally consistent: every foreign key
holds, every CHECK holds, no orphans. create_collections.py gives
MongoDB $jsonSchema validators that prove each document is well formed.
Both are real, and both stop at their own edge.

Nothing checks that the two databases agree WITH EACH OTHER. There is no
foreign key from movie_features.cast[].person_id to people.person_id,
because the two stores cannot see one another. The catalogue is written
into MySQL as rows and into MongoDB as embedded copies, and from that
moment the only thing keeping them equal is that nobody wrote to one
without writing to the other.

Two things in this project write across that line:

  * benchmark/mysql_bench.py and benchmark/mongo_bench.py task T4
    deliberately rename a person and rename them back. MySQL changes one
    row; MongoDB rewrites every embedded copy. Each has a finally: block
    that restores the original name, but a process killed hard enough
    leaves the rename in place - in ONE database.
  * webapp/routes.py delete_user() removes a user from both stores as a
    compensating transaction. It narrows the window in which the two can
    disagree; it does not close it.

So this script is not decoration. It is the check that corresponds to
the guarantee we gave up by splitting the data across two stores, and
running it is the only way to know the split has not already drifted.

Every check prints a count that must be 0, except the inventory counts
at the end. Exit status is 0 if everything agrees, 1 if anything does
not, so this can be run before a demo or a submission.

Usage (from the repo root, both databases running, .env filled in):
    python database/verify_cross_db.py
    python database/verify_cross_db.py --sample 1000
"""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    print("python-dotenv is not installed; relying on the real environment.")

BENCH_TAG = " [bench]"


# ---------------------------------------------------------------------
# Connections. Read-only work, so the application account is enough.
# ---------------------------------------------------------------------
def mysql_connect():
    import mysql.connector
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "inf2003"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "moviedb"),
        charset="utf8mb4",
    )


def mongo_connect():
    from pymongo import MongoClient
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                         serverSelectionTimeoutMS=5000)
    return client, client[os.getenv("MONGO_DB", "moviedb_nosql")]


# ---------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------
class Report:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def check(self, name, count, detail=""):
        """Record a check whose correct answer is 0."""
        ok = (count == 0)
        if not ok:
            self.failed += 1
        self.rows.append((name, count, ok, detail))

    def note(self, name, value, detail=""):
        """Record an inventory figure that is not pass/fail."""
        self.rows.append((name, value, None, detail))

    def print_table(self):
        width = max(len(r[0]) for r in self.rows)
        print()
        print(f"{'check':<{width}}  {'value':>10}  status")
        print(f"{'-' * width}  {'-' * 10}  ------")
        for name, value, ok, detail in self.rows:
            status = "     " if ok is None else ("[OK  ]" if ok else "[FAIL]")
            print(f"{name:<{width}}  {value:>10}  {status}"
                  + (f"  {detail}" if detail else ""))
        print()


# ---------------------------------------------------------------------
# A. Leftovers from an interrupted benchmark
# ---------------------------------------------------------------------
def check_bench_leftovers(cur, db, report):
    """T4 renames a person and renames them back. Did a run die between?"""
    cur.execute("SELECT COUNT(*) FROM people WHERE name LIKE %s",
                ("%" + BENCH_TAG,))
    mysql_left = cur.fetchone()[0]
    report.check("MySQL people still carrying the benchmark tag", mysql_left,
                 "run benchmark/mysql_bench.py to clean, or UPDATE by hand")

    # A regex rather than a LIKE, and anchored at the end for the same reason.
    pattern = {"$regex": r"\[bench\]$"}
    cast_left = db.movie_features.count_documents({"cast.name": pattern})
    crew_left = db.movie_features.count_documents({"crew.name": pattern})
    report.check("MongoDB films with a tagged cast name", cast_left)
    report.check("MongoDB films with a tagged crew name", crew_left)


# ---------------------------------------------------------------------
# B. The two stores hold the same films
# ---------------------------------------------------------------------
def check_film_sets(cur, db, report):
    cur.execute("SELECT movie_id FROM movies")
    mysql_films = {row[0] for row in cur.fetchall()}
    mongo_films = set(db.movie_features.distinct("movie_id"))

    report.check("films in MySQL but not in MongoDB",
                 len(mysql_films - mongo_films),
                 _sample_ids(mysql_films - mongo_films))
    report.check("films in MongoDB but not in MySQL",
                 len(mongo_films - mysql_films),
                 _sample_ids(mongo_films - mysql_films))
    report.note("films held by both", len(mysql_films & mongo_films))
    return sorted(mysql_films & mongo_films)


# ---------------------------------------------------------------------
# C. The embedded names still match the rows they were copied from
# ---------------------------------------------------------------------
def check_person_names(cur, db, report, sample_size):
    """The duplicated-data check: does people.name equal every copy?

    This is the update anomaly asked as a question. If any write ever
    touched one store without the other, the answer is no, and the count
    below is how many people it happened to.
    """
    cur.execute("""
        SELECT person_id FROM movie_cast
         GROUP BY person_id
         HAVING COUNT(*) >= 2
         ORDER BY person_id
    """)
    candidates = [row[0] for row in cur.fetchall()]
    if not candidates:
        report.note("people compared across both stores", 0, "no candidates")
        return

    step = max(1, len(candidates) // sample_size)
    sample = candidates[::step][:sample_size]

    cur.execute(
        "SELECT person_id, name FROM people WHERE person_id IN ({})".format(
            ",".join(["%s"] * len(sample))), sample)
    mysql_names = {pid: name for pid, name in cur.fetchall()}

    # One aggregation rather than one query per person: match the films
    # that mention any sampled person, unwind the cast array, keep only
    # the sampled entries, and collect the DISTINCT names each person is
    # stored under.
    pipeline = [
        {"$match": {"cast.person_id": {"$in": sample}}},
        {"$unwind": "$cast"},
        {"$match": {"cast.person_id": {"$in": sample}}},
        {"$group": {"_id": "$cast.person_id",
                    "names": {"$addToSet": "$cast.name"},
                    "films": {"$sum": 1}}},
    ]
    mongo_names = {doc["_id"]: doc for doc in db.movie_features.aggregate(
        pipeline, allowDiskUse=True)}

    disagreeing, internally_split, missing = [], [], []
    for pid in sample:
        doc = mongo_names.get(pid)
        if doc is None:
            missing.append(pid)
            continue
        names = doc["names"]
        if len(names) > 1:
            # MongoDB disagrees with ITSELF: the same person is stored
            # under two spellings in different films. No foreign key
            # exists to prevent this, which is the point.
            internally_split.append(pid)
        elif names and names[0] != mysql_names.get(pid):
            disagreeing.append(pid)

    report.note("people compared across both stores", len(sample))
    report.check("people whose MongoDB copies disagree with MySQL",
                 len(disagreeing), _sample_ids(disagreeing))
    report.check("people stored under two names inside MongoDB",
                 len(internally_split), _sample_ids(internally_split))
    report.check("sampled people with no embedded copy at all",
                 len(missing), _sample_ids(missing))


# ---------------------------------------------------------------------
# D. The embedded arrays are the same size as the tables they came from
# ---------------------------------------------------------------------
def check_cast_counts(cur, db, report, film_ids, sample_size):
    step = max(1, len(film_ids) // sample_size)
    sample = film_ids[::step][:sample_size]
    if not sample:
        return

    marks = ",".join(["%s"] * len(sample))
    cur.execute(f"""
        SELECT movie_id, COUNT(*) FROM movie_cast
         WHERE movie_id IN ({marks}) GROUP BY movie_id
    """, sample)
    mysql_cast = dict(cur.fetchall())
    cur.execute(f"""
        SELECT movie_id, COUNT(*) FROM movie_crew
         WHERE movie_id IN ({marks}) GROUP BY movie_id
    """, sample)
    mysql_crew = dict(cur.fetchall())

    mongo_sizes = {}
    for doc in db.movie_features.find(
            {"movie_id": {"$in": sample}},
            {"_id": 0, "movie_id": 1, "cast": 1, "crew": 1}):
        mongo_sizes[doc["movie_id"]] = (len(doc.get("cast") or []),
                                        len(doc.get("crew") or []))

    cast_off, crew_off = [], []
    for mid in sample:
        sizes = mongo_sizes.get(mid)
        if sizes is None:
            continue
        if mysql_cast.get(mid, 0) != sizes[0]:
            cast_off.append(mid)
        if mysql_crew.get(mid, 0) != sizes[1]:
            crew_off.append(mid)

    report.note("films compared by credit count", len(sample))
    report.check("films whose cast count differs", len(cast_off),
                 _sample_ids(cast_off))
    report.check("films whose crew count differs", len(crew_off),
                 _sample_ids(crew_off))


# ---------------------------------------------------------------------
# E. MongoDB documents pointing at users and films that no longer exist
# ---------------------------------------------------------------------
def check_dangling_references(cur, db, report):
    """The foreign keys that cannot be declared, checked by hand.

    delete_user() is supposed to remove a user from both stores at once.
    Anything found here is a user whose MySQL row went and whose MongoDB
    documents stayed - the exact failure the compensating transaction
    was written to prevent.
    """
    cur.execute("SELECT user_id FROM users")
    users = {row[0] for row in cur.fetchall()}
    cur.execute("SELECT movie_id FROM movies")
    films = {row[0] for row in cur.fetchall()}

    for collection in ("user_tags", "user_activity_logs", "recommendations"):
        if collection not in db.list_collection_names():
            continue
        orphan_users = [uid for uid in db[collection].distinct("user_id")
                        if uid is not None and uid not in users]
        report.check(f"{collection} documents for a deleted user",
                     len(orphan_users), _sample_ids(orphan_users))

    orphan_films = [mid for mid in db.user_tags.distinct("movie_id")
                    if mid is not None and mid not in films]
    report.check("user_tags pointing at a film that is gone",
                 len(orphan_films), _sample_ids(orphan_films))


# ---------------------------------------------------------------------
def _sample_ids(ids, limit=5):
    ids = list(ids)
    if not ids:
        return ""
    head = ", ".join(str(i) for i in ids[:limit])
    return f"e.g. {head}" + (" ..." if len(ids) > limit else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", type=int, default=300,
                        help="people and films to compare in detail "
                             "(default 300; use 0 for every one)")
    args = parser.parse_args()

    try:
        conn = mysql_connect()
    except Exception as exc:
        print(f"Cannot reach MySQL: {exc}")
        return 2
    try:
        client, db = mongo_connect()
        client.server_info()
    except Exception as exc:
        print(f"Cannot reach MongoDB: {exc}")
        conn.close()
        return 2

    cur = conn.cursor()
    report = Report()
    print(f"MySQL {conn.get_server_info()} | "
          f"MongoDB {client.server_info()['version']}")

    sample_size = args.sample if args.sample > 0 else 10 ** 9

    check_bench_leftovers(cur, db, report)
    film_ids = check_film_sets(cur, db, report)
    check_person_names(cur, db, report, sample_size)
    check_cast_counts(cur, db, report, film_ids, sample_size)
    check_dangling_references(cur, db, report)

    report.print_table()
    cur.close()
    conn.close()
    client.close()

    if report.failed:
        print(f"{report.failed} check(s) FAILED. The two databases do not "
              f"agree; see the ids above before demonstrating anything.")
        return 1
    print("Both databases agree on every check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
