"""
INF2003 Group Project - Team 37
benchmark/load_storage_bench.py : Experiment 6, load time and storage

The two questions nobody asks until the data is big:

  1. How long does it take to get the same information INTO each
     database?
  2. How much disk does each one then use?

Why it matters here specifically: movie_features is a denormalised
copy of what MySQL keeps in movies + people + movie_cast + movie_crew.
Every actor's name is stored once in MySQL's people table and again in
every film document they appear in. That duplication is what makes
MongoDB fast at "give me this film" (Experiment 1 T1) and slow at
"rename this person" (T4). This script puts a number on the storage
side of that bargain, so the trade-off can be stated with figures
instead of adjectives.

What is measured

  A. Storage, as the databases are now
     MySQL   : information_schema.TABLES - data vs index bytes per table
     MongoDB : collStats - size, storageSize, totalIndexSize
     Reported for the cast/crew data on its own as well as in total,
     because that is the part stored twice.

  B. Load time for the SAME information, client-driven both sides
     MySQL   : the cleaned CSVs for movies, people, movie_cast and
               movie_crew, inserted with executemany into scratch
               tables in the moviedb_bench database
     MongoDB : the 4,803 documents of movie_features.jsonl, inserted
               with insert_many into a scratch collection
     Both scratch objects are dropped afterwards. The application's
     own databases are only read.

     Note what this does and does not compare. MySQL loads 241,000
     rows across four tables with foreign keys to check; MongoDB loads
     4,803 documents with no cross-document checking. That IS the
     difference between the two designs, not a flaw in the test, but
     it means the right headline is bytes and seconds per unit of
     information, not "database X is faster".

     database/mysql/02_load.sql uses LOAD DATA LOCAL INFILE instead,
     which is far faster than executemany because the server reads the
     file itself. It is not used here because it needs local_infile
     enabled on both client and server, and because MongoDB has no
     equivalent that pymongo can call - a fair client-side comparison
     has to go through the driver on both sides.

Usage (from the repo root):
    python benchmark/load_storage_bench.py
    python benchmark/load_storage_bench.py --skip-load   # storage only

Outputs in benchmark/results/:
    storage_summary.csv
    load_summary.csv
"""

import argparse
import csv
import json
import os
import platform
import time
from datetime import datetime

import harness
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(harness.REPO_ROOT / ".env")

CLEANED = harness.REPO_ROOT / "data" / "cleaned"
BENCH_DATABASE = "moviedb_bench"
SCRATCH_COLLECTION = "movie_features_loadtest"
BATCH = 1000

# The tables that hold the cast and crew information, i.e. the part
# MongoDB stores a second time inside movie_features.
CAST_CREW_TABLES = ("people", "movie_cast", "movie_crew")

SCRATCH_SCHEMA = [
    ("movies_lt", """
        CREATE TABLE movies_lt (
          movie_id INT NOT NULL, title VARCHAR(255) NOT NULL,
          original_title VARCHAR(255) NULL, original_language CHAR(2) NULL,
          release_date DATE NULL, runtime SMALLINT NULL, budget BIGINT NULL,
          revenue BIGINT NULL, popularity DECIMAL(10,4) NULL,
          vote_average DECIMAL(3,1) NULL, vote_count INT NULL,
          status VARCHAR(20) NULL,
          PRIMARY KEY (movie_id)) ENGINE=InnoDB""",
     "movies.csv", 12),
    ("people_lt", """
        CREATE TABLE people_lt (
          person_id INT NOT NULL, name VARCHAR(255) NOT NULL, gender TINYINT NULL,
          PRIMARY KEY (person_id), INDEX idx_lt_people_name (name)) ENGINE=InnoDB""",
     "people.csv", 3),
    ("movie_cast_lt", """
        CREATE TABLE movie_cast_lt (
          credit_id CHAR(24) NOT NULL, movie_id INT NOT NULL, person_id INT NOT NULL,
          character_name VARCHAR(300) NULL, cast_order SMALLINT NULL,
          PRIMARY KEY (credit_id),
          CONSTRAINT fk_lt_cast_movie FOREIGN KEY (movie_id)
            REFERENCES movies_lt (movie_id) ON DELETE CASCADE,
          CONSTRAINT fk_lt_cast_person FOREIGN KEY (person_id)
            REFERENCES people_lt (person_id),
          INDEX idx_lt_cast_movie (movie_id),
          INDEX idx_lt_cast_person (person_id)) ENGINE=InnoDB""",
     "movie_cast.csv", 5),
    ("movie_crew_lt", """
        CREATE TABLE movie_crew_lt (
          credit_id CHAR(24) NOT NULL, movie_id INT NOT NULL, person_id INT NOT NULL,
          department VARCHAR(50) NOT NULL, job VARCHAR(100) NOT NULL,
          PRIMARY KEY (credit_id),
          CONSTRAINT fk_lt_crew_movie FOREIGN KEY (movie_id)
            REFERENCES movies_lt (movie_id) ON DELETE CASCADE,
          CONSTRAINT fk_lt_crew_person FOREIGN KEY (person_id)
            REFERENCES people_lt (person_id),
          INDEX idx_lt_crew_movie (movie_id),
          INDEX idx_lt_crew_person (person_id)) ENGINE=InnoDB""",
     "movie_crew.csv", 5),
]


# =====================================================================
# A. Storage
# =====================================================================
def mysql_storage(conn, database):
    cur = conn.cursor()
    cur.execute("""
        SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH
          FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
         ORDER BY DATA_LENGTH + INDEX_LENGTH DESC
    """, (database,))
    tables = cur.fetchall()
    rows = []
    for name, approx_rows, data_length, index_length in tables:
        # TABLE_ROWS is only an estimate for InnoDB, and these numbers
        # go into a report, so count the rows properly.
        cur.execute(f"SELECT COUNT(*) FROM `{database}`.`{name}`")
        exact = cur.fetchone()[0]
        rows.append({
            "database": "MySQL",
            "object": name,
            "records": exact,
            "data_mb": round((data_length or 0) / 1e6, 3),
            "index_mb": round((index_length or 0) / 1e6, 3),
            "total_mb": round(((data_length or 0) + (index_length or 0)) / 1e6, 3),
        })
    cur.close()
    return rows


def mongo_storage(db):
    rows = []
    for name in sorted(db.list_collection_names()):
        stats = db.command("collStats", name)
        rows.append({
            "database": "MongoDB",
            "object": name,
            "records": stats.get("count", 0),
            "data_mb": round(stats.get("size", 0) / 1e6, 3),
            "index_mb": round(stats.get("totalIndexSize", 0) / 1e6, 3),
            "total_mb": round((stats.get("size", 0)
                               + stats.get("totalIndexSize", 0)) / 1e6, 3),
            # WiredTiger compresses on disk, so this is usually much
            # smaller than data_mb; it is the figure that matters for
            # "how much disk does this cost".
            "compressed_storage_mb": round(stats.get("storageSize", 0) / 1e6, 3),
        })
    return rows


# =====================================================================
# B. Load time
# =====================================================================
def read_csv_rows(path, columns):
    """Rows from a cleaned CSV, with \\N turned back into NULL."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)                       # header
        rows = []
        for record in reader:
            if len(record) != columns:
                continue
            rows.append(tuple(None if value in ("\\N", "") else value
                              for value in record))
        return rows


def load_mysql(conn, data):
    """Create the scratch tables, insert everything, report the time."""
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {BENCH_DATABASE}")
    cur.execute(f"USE {BENCH_DATABASE}")
    for table, _, _, _ in reversed(SCRATCH_SCHEMA):
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    for table, ddl, _, _ in SCRATCH_SCHEMA:
        cur.execute(ddl)

    conn.autocommit = False
    total_rows, per_table = 0, []
    started = time.perf_counter()
    for table, _, filename, columns in SCRATCH_SCHEMA:
        rows = data[filename]
        table_started = time.perf_counter()
        placeholders = ", ".join(["%s"] * columns)
        statement = f"INSERT INTO {table} VALUES ({placeholders})"
        for start in range(0, len(rows), BATCH):
            cur.executemany(statement, rows[start:start + BATCH])
        conn.commit()
        elapsed = time.perf_counter() - table_started
        per_table.append((table, len(rows), elapsed))
        total_rows += len(rows)
    total = time.perf_counter() - started
    conn.autocommit = True

    # Point the connection back at the application database before the
    # scratch one is dropped underneath it.
    cur.execute(f"USE {os.getenv('MYSQL_DATABASE', 'moviedb')}")
    cur.close()
    return total, total_rows, per_table, mysql_storage(conn, BENCH_DATABASE)


def drop_mysql_scratch(conn):
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {BENCH_DATABASE}")
    cur.close()


def load_mongo(db, documents):
    db.drop_collection(SCRATCH_COLLECTION)
    collection = db[SCRATCH_COLLECTION]
    started = time.perf_counter()
    for start in range(0, len(documents), BATCH):
        collection.insert_many([dict(d) for d in documents[start:start + BATCH]],
                               ordered=False)
    total = time.perf_counter() - started
    stats = db.command("collStats", SCRATCH_COLLECTION)
    return total, len(documents), stats


# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-load", action="store_true",
                        help="report storage only; write nothing")
    args = parser.parse_args()

    conn = harness.mysql_connect(autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    mysql_version = cur.fetchone()[0]
    application_database = os.getenv("MYSQL_DATABASE", "moviedb")

    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    mongo_db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
    mongo_version = client.server_info()["version"]

    print(f"MySQL {mysql_version} | MongoDB {mongo_version}\n")

    # ---- A. Storage -------------------------------------------------
    storage = mysql_storage(conn, application_database) + mongo_storage(mongo_db)
    harness.print_table(
        [{k: row.get(k, "") for k in ("database", "object", "records",
                                      "data_mb", "index_mb", "total_mb")}
         for row in storage],
        ["database", "object", "records", "data_mb", "index_mb", "total_mb"])

    mysql_total = sum(row["total_mb"] for row in storage
                      if row["database"] == "MySQL")
    mongo_total = sum(row["total_mb"] for row in storage
                      if row["database"] == "MongoDB")
    cast_crew_mb = sum(row["total_mb"] for row in storage
                       if row["database"] == "MySQL"
                       and row["object"] in CAST_CREW_TABLES)
    features_mb = sum(row["total_mb"] for row in storage
                      if row["database"] == "MongoDB"
                      and row["object"] == "movie_features")

    print(f"\nMySQL total   {mysql_total:8.2f} MB "
          f"(cast and crew alone: {cast_crew_mb:.2f} MB)")
    print(f"MongoDB total {mongo_total:8.2f} MB "
          f"(movie_features alone: {features_mb:.2f} MB)")
    if cast_crew_mb:
        print(f"\nThe same cast and crew information costs {features_mb:.2f} MB "
              f"embedded in MongoDB\nand {cast_crew_mb:.2f} MB normalised in MySQL "
              f"({features_mb / cast_crew_mb:.2f}x), and MongoDB holds\nthe overviews, "
              f"keywords, companies and countries in those documents too.")

    harness.write_csv("storage_summary.csv", storage)

    if args.skip_load:
        cur.close()
        conn.close()
        client.close()
        print(f"\nStorage results written to {harness.RESULTS_DIR}")
        return

    # ---- B. Load time -----------------------------------------------
    missing = [filename for _, _, filename, _ in SCRATCH_SCHEMA
               if not (CLEANED / filename).exists()]
    jsonl = CLEANED / "movie_features.jsonl"
    if missing or not jsonl.exists():
        print(f"\nSkipping the load test: {missing or jsonl} not found in {CLEANED}")
        cur.close()
        conn.close()
        client.close()
        return

    print("\nReading the cleaned files (not timed) ...")
    data = {filename: read_csv_rows(CLEANED / filename, columns)
            for _, _, filename, columns in SCRATCH_SCHEMA}
    with jsonl.open(encoding="utf-8") as handle:
        documents = [json.loads(line) for line in handle if line.strip()]
    print(f"  {sum(len(v) for v in data.values()):,} CSV rows, "
          f"{len(documents):,} JSON documents")

    load_rows = []
    try:
        print(f"\nLoading into MySQL scratch database {BENCH_DATABASE} "
              f"(executemany, batches of {BATCH}) ...")
        seconds, rows, per_table, sizes = load_mysql(conn, data)
        for table, count, elapsed in per_table:
            print(f"  {table:<16} {count:>8,} rows in {elapsed:6.2f}s "
                  f"({count / elapsed:>9,.0f} rows/s)")
        scratch_mb = sum(row["total_mb"] for row in sizes)
        print(f"  TOTAL            {rows:>8,} rows in {seconds:6.2f}s "
              f"({rows / seconds:>9,.0f} rows/s), {scratch_mb:.2f} MB on disk")
        load_rows.append({
            "database": "MySQL",
            "what": "movies + people + movie_cast + movie_crew (4 tables, FKs checked)",
            "method": f"executemany, batches of {BATCH}, one commit per table",
            "units": rows, "unit_name": "rows",
            "seconds": round(seconds, 3),
            "units_per_second": round(rows / seconds, 1),
            "size_mb": round(scratch_mb, 3),
        })

        print(f"\nLoading into MongoDB scratch collection {SCRATCH_COLLECTION} "
              f"(insert_many, batches of {BATCH}) ...")
        seconds, count, stats = load_mongo(mongo_db, documents)
        mongo_mb = stats.get("size", 0) / 1e6
        print(f"  {count:>8,} documents in {seconds:6.2f}s "
              f"({count / seconds:>9,.0f} docs/s), {mongo_mb:.2f} MB "
              f"({stats.get('storageSize', 0) / 1e6:.2f} MB compressed on disk)")
        load_rows.append({
            "database": "MongoDB",
            "what": "movie_features (one document per film, cast and crew embedded)",
            "method": f"insert_many, batches of {BATCH}, unordered",
            "units": count, "unit_name": "documents",
            "seconds": round(seconds, 3),
            "units_per_second": round(count / seconds, 1),
            "size_mb": round(mongo_mb, 3),
        })
    finally:
        print("\nDropping the scratch objects ...")
        drop_mysql_scratch(conn)
        mongo_db.drop_collection(SCRATCH_COLLECTION)
        cur.close()
        conn.close()

    header = (f"MySQL {mysql_version} | MongoDB {mongo_version}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"batch={BATCH}\n\n"
              f"MySQL application database   {mysql_total:.2f} MB\n"
              f"MongoDB application database {mongo_total:.2f} MB\n"
              f"cast and crew, normalised    {cast_crew_mb:.2f} MB "
              f"(people + movie_cast + movie_crew)\n"
              f"cast and crew, embedded      {features_mb:.2f} MB "
              f"(movie_features, plus overviews and keywords)\n\n"
              "The load figures are client-driven on both sides (executemany /\n"
              "insert_many), so they compare drivers as much as servers.\n"
              "02_load.sql uses LOAD DATA LOCAL INFILE, where the server reads the\n"
              "file directly, and is several times faster than the number above.\n")
    harness.write_text("load_storage_notes.txt", header)
    harness.write_csv("load_summary.csv", load_rows)

    client.close()
    print(f"\nResults written to {harness.RESULTS_DIR}")


if __name__ == "__main__":
    main()
