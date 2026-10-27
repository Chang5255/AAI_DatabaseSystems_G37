# Performance benchmarks

Two experiments, both run from Python so MySQL and MongoDB are timed the
same way (`harness.py`).

| Experiment | Question | Script |
|---|---|---|
| 1. SQL vs MongoDB (main) | Same cast/crew data, normalised tables vs embedded arrays: how do common operations compare? | `mysql_bench.py` + MongoDB counterpart |
| 2. MySQL indexing (optional) | How does a ratings lookup scale from 1k to 1M rows, with and without an index? | `ratings_scaling.py` |

## Setup

1. MySQL loaded with `database/mysql/01` to `07`.
2. Install the MySQL driver (add it to `requirements.txt` with the version
   `pip freeze` reports):
   ```bash
   pip install mysql-connector-python
   ```
3. In the repo-root `.env`:
   ```
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_USER=inf2003
   MYSQL_PASSWORD=your_password
   MYSQL_DATABASE=moviedb
   ```
4. Experiment 2 also needs (run once as root):
   ```sql
   GRANT ALL PRIVILEGES ON moviedb_bench.* TO 'inf2003'@'localhost';
   ```

Run every script from the repo root, e.g. `python benchmark/mysql_bench.py`.
Results go to `benchmark/results/`.

## How timing works (both databases)

- Connection opened once, outside the timed code.
- Each timed call includes fetching all results (and decoding JSON on the
  SQL side, as pymongo decodes BSON).
- 1 untimed warm-up call per id, then 5 timed calls (configurable).
- Reported in milliseconds: mean, median, 95th percentile (nearest rank),
  min, max, standard deviation.
- The plan each database actually used is saved alongside the timings:
  `EXPLAIN ANALYZE` for MySQL, `.explain("executionStats")` for MongoDB.

Use **median** as the headline figure; it is not distorted by the
occasional slow outlier. Use **p95** to talk about worst-case behaviour.

## Experiment 1: SQL vs MongoDB on cast and crew

`sample_ids.json` holds the ids both sides must use: 300 movies with at
least one cast credit and 300 actors with at least 3 credits, drawn with
seed 37. It is committed so both benchmarks test exactly the same ids.
`mysql_bench.py --resample` draws a new set; if you do that, rerun the
MongoDB side too.

| Task | MySQL (`mysql_bench.py`) | MongoDB equivalent (to implement in `mongo_bench.py`) | Expected to favour |
|---|---|---|---|
| T1 one movie's cast + crew | 2 queries: cast join, crew join | `find_one({"movie_id": id}, {"cast": 1, "crew": 1})` | MongoDB |
| T2 same, one round trip | 1 statement returning JSON | same as T1 | MongoDB, closer |
| T3 all films for one actor | `WHERE person_id = %s` on `movie_cast` (indexed) | `find({"cast.person_id": pid}, {"movie_id": 1, "cast.$": 1})` with an index on `cast.person_id` | close |
| T4 rename one person | `UPDATE people` (1 row) | `update_many` on every movie containing them (below) | MySQL |

MongoDB T4 (one statement updating both arrays):
```python
coll.update_many(
    {"$or": [{"cast.person_id": pid}, {"crew.person_id": pid}]},
    {"$set": {"cast.$[c].name": new_name, "crew.$[w].name": new_name}},
    array_filters=[{"c.person_id": pid}, {"w.person_id": pid}],
)
```
Time only the rename; restore the original name afterwards, untimed.
Record `modified_count`: the number of documents touched is part of the
result (MySQL always touches 1 row).

The MongoDB snippets above are a starting point and have not been run
against our MongoDB instance; check field names against
`movie_features` first.

So the two summaries can be merged into one table for the report, the
MongoDB script should write `results/mongo_experiment1_summary.csv` with
the same columns as `results/mysql_experiment1_summary.csv`:

```
database, task, description, n, mean_ms, median_ms, p95_ms, min_ms, max_ms, stdev_ms, avg_rows
```
using `database = MongoDB` and the same task names (`T1_movie_cast_crew_2q`,
`T2_movie_cast_crew_json`, `T3_films_by_actor`, `T4_rename_person`).
For MongoDB, T1 and T2 are the same operation; record it under both names
or only under T1.

## Experiment 2: ratings lookup scaling (MySQL only)

Ratings exist only in MySQL, so this is MySQL compared with itself, not
with MongoDB. It runs in a separate database, `moviedb_bench`, which is
created, filled with synthetic ratings and dropped at the end; the
application database is only read.

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
