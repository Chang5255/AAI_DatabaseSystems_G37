# Performance benchmarks

Six experiments. Every one of them is run from Python so that MySQL and
MongoDB are timed the same way, through the same helpers in `harness.py`.

| # | Question | Script | Databases |
|---|---|---|---|
| 1 | Same cast/crew data, normalised tables vs embedded arrays: how do common operations compare? | `mysql_bench.py` + `mongo_bench.py` | both |
| 2 | How does a ratings lookup scale from 1k to 1M rows, with and without an index? | `ratings_scaling.py` | MySQL |
| 3 | How much does the multikey index on `cast.person_id` matter? | `mongo_index_bench.py` | MongoDB |
| 4 | How many operations per second can each database sustain, and what happens to p95 as clients arrive? | `throughput_bench.py` | both |
| 5 | What does a commit cost: autocommit per row vs one transaction vs batches? | `txn_cost_bench.py` | both |
| 6 | How long to load the same information, and how much disk does each design use? | `load_storage_bench.py` | both |

`merge_results.py` collects the CSVs afterwards into one markdown file
(`results/ALL_RESULTS.md`) with a table per experiment, ready for the
report.

## Setup

1. MySQL loaded with `database/mysql/01` to `09`.
2. MongoDB loaded with `python database/mongodb/setup_mongodb.py`
   (experiments 1, 3, 4 and 6 need `movie_features`; experiment 3 needs
   the indexes from `create_indexes.py`).
3. Packages: everything is already in `requirements.txt`
   (`mysql-connector-python`, `pymongo`, `python-dotenv`).
4. In the repo-root `.env`:
   ```
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_USER=inf2003
   MYSQL_PASSWORD=your_password
   MYSQL_DATABASE=moviedb
   MONGODB_URI=mongodb://localhost:27017/
   MONGODB_DATABASE=movie_app
   ```
5. Experiments 2 and 6 build a scratch database, so the MySQL account
   needs rights on it. `01b_create_user.sql` grants them.

Run every script **from the repo root**, e.g. `python benchmark/mysql_bench.py`.
Results go to `benchmark/results/`.

### The order to run them in

```bash
python benchmark/mysql_bench.py          # draws sample_ids.json, which the others reuse
python benchmark/mongo_bench.py
python benchmark/mongo_index_bench.py
python benchmark/ratings_scaling.py      # ~5 minutes
python benchmark/throughput_bench.py
python benchmark/txn_cost_bench.py
python benchmark/load_storage_bench.py
python benchmark/merge_results.py        # builds results/ALL_RESULTS.md
```

`mysql_bench.py` must go first the very first time: it writes
`sample_ids.json`, and every other script reads its ids from there so
that all of them test exactly the same films and people.

## How timing works (both databases)

- Connections are opened once, outside the timed code.
- Each timed call includes fetching every result (and decoding JSON on
  the SQL side, as pymongo decodes BSON).
- Untimed warm-up calls run first, so the figures describe a warm
  cache, which is what a running application sees.
- Reported in milliseconds: mean, median, 95th percentile (nearest
  rank), min, max, standard deviation.
- The plan each database actually used is saved next to the timings:
  `EXPLAIN ANALYZE` for MySQL, `.explain("executionStats")` for MongoDB.

Use the **median** as the headline figure; it is not distorted by the
occasional slow outlier. Use **p95** to talk about worst-case behaviour.
In experiment 4, use **ops/second** for throughput and watch p95 rise.

## What writes to the databases, and what is cleaned up

| Script | Writes | Cleaned up |
|---|---|---|
| `mysql_bench.py` | renames one person, then restores the name | yes, and on start-up it repairs a rename left by an interrupted run |
| `mongo_bench.py` | the same rename, in every embedded copy | same |
| `mongo_index_bench.py` | drops and rebuilds indexes; no documents | the indexes it found are restored exactly |
| `ratings_scaling.py` | creates `moviedb_bench` | dropped at the end (`--keep` to keep it) |
| `throughput_bench.py` | one throw-away user's ratings and activity logs | user deleted, documents removed |
| | (it writes as a new user with ids above every real one, so no real user's ratings are touched) | |
| `txn_cost_bench.py` | the same | same |
| `load_storage_bench.py` | scratch tables in `moviedb_bench`, a scratch collection | both dropped |

Nothing writes to the real users' ratings, and nothing changes the
catalogue.

## Experiment 1: SQL vs MongoDB on cast and crew

`sample_ids.json` holds the ids both sides must use: 300 movies with at
least one cast credit, and 300 actors with at least 3 credits, drawn
with seed 37. It is committed, so both benchmarks test exactly the same
ids. `mysql_bench.py --resample` draws a new set; after that, rerun the
MongoDB side too.

| Task | MySQL | MongoDB | Favours |
|---|---|---|---|
| T1 one movie's cast + crew | 2 queries, each a 3-table join | `find_one({"movie_id": id}, {"cast": 1, "crew": 1})` | MongoDB |
| T2 same, one round trip | 1 statement building JSON with `JSON_ARRAYAGG` | the same `find_one` with **no** projection (so this measures what the projection saves) | MongoDB |
| T3 all films for one actor | `WHERE person_id = ?` on `movie_cast` (indexed) | `find({"cast.person_id": pid}, {"movie_id": 1, "cast.$": 1})` on the multikey index | close |
| T4 rename one person | `UPDATE people` — **1 row**, the name is stored once | every document holding that person must be rewritten | MySQL |

T4 is the interesting one: `modified_count` is part of the result, and
it is the update anomaly from lecture 4 expressed as a number. The
MongoDB side does it in two `update_many` calls rather than one, because
a single call with array filters for both `cast` and `crew` fails on the
28 films that have no `crew` array at all.

Both scripts write a summary CSV with the same columns, so
`merge_results.py` can interleave them:

```
database, task, description, n, mean_ms, median_ms, p95_ms, min_ms, max_ms, stdev_ms, avg_rows
```

## Experiment 2: ratings lookup scaling (MySQL only)

Ratings exist only in MySQL, so this is MySQL compared with itself. It
runs in a separate database, `moviedb_bench`, which is created, filled
with synthetic ratings and dropped at the end; the application database
is only read.

The application's own `ratings` table now holds 246,976 rows of real
MovieLens data, which sits between the 100k and 1M points of this
experiment — so the curve it produces says something about the real
table rather than about a hypothetical one.

For each size (1k, 10k, 100k, 1M by default) it records:

- `access_type` from `EXPLAIN FORMAT=JSON`: `ALL` = full table scan,
  `ref` = index lookup. This is the main evidence.
- `est_rows_examined`: the optimiser's estimate for the first sampled film.
- median / p95 time for `WHERE movie_id = %s`, without then with an index.
- `by_user_id_pk`: control query on `user_id`, which always uses the
  primary key (leftmost column of `(user_id, movie_id)`), at every size.
- index build time and data/index size in MB.

A full run takes about 5 minutes, most of it the unindexed lookups on
1M rows. For a quick check: `--sizes 1000 10000 100000`.

## Experiment 3: MongoDB indexing

The MongoDB counterpart of experiment 2, and the place to explain the
one index type with no MySQL equivalent. A **multikey** index indexes
every element of an embedded array, so one film with 80 cast members
produces 80 index entries, and `{"cast.person_id": 500}` becomes an
index lookup instead of a collection scan. In MySQL the array is a
separate table (`movie_cast`) with an ordinary index on `person_id`.

For three queries, the index is dropped, the query timed, the index
rebuilt (build time recorded) and the query timed again. The evidence
is the timing plus `COLLSCAN` vs `IXSCAN` and `totalDocsExamined`.

## Experiment 4: throughput

Experiments 1 to 3 measure latency with nothing else happening, which
is not what an application sees. This one runs 1, 2, 4, 8 and 16
threads, each with its own connection, for a few seconds per level, and
reports operations per second alongside median and p95 latency. The
part worth discussing is where throughput flattens while p95 keeps
climbing: that is saturation.

Two caveats to state in the report:

- The read workloads are comparable; the **write** workloads are not.
  An upsert into a constrained, indexed, audited table is doing more
  work than appending a document. Compare each database against itself
  as concurrency rises.
- All the workers are Python threads in one process, so part of the
  ceiling is the client's, not the server's.

## Experiment 5: the cost of a commit

The same N rows written five ways: autocommit per row, one transaction,
`executemany` in batches (MySQL); `insert_one` per document and
`insert_many` in batches (MongoDB). `--journalled` adds MongoDB with
`j=true` for a durability setting closer to MySQL's default.

The gap between variants 1 and 2 is the cost of committing — one disk
sync per row versus one for the lot. The gap between 2 and 3 is the
cost of round trips. Both gaps are usually large enough to make the
point without any comparison between the two databases.

## Experiment 6: load time and storage

`movie_features` is a denormalised copy of what MySQL holds in
`movies` + `people` + `movie_cast` + `movie_crew`. Every actor's name is
stored once in MySQL and again in every film document they appear in.
This experiment puts a figure on that: the storage each design uses,
and how long the same information takes to load through each driver.

`--skip-load` reports storage only and writes nothing.
