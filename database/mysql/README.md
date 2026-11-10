# Relational database (MySQL)

MySQL **8.0.19 or later** is required: `CHECK` constraints are only
enforced from 8.0.16, and the upsert in `05_crud.sql` and the web app
uses the row-alias syntax (`VALUES (...) AS new`) added in 8.0.19.
Developed and tested on MySQL 8.0.46.

The fastest way to build all of this is `python setup_all.py` from the
repo root, which runs the scripts in order and fills in the passwords
and file paths itself. The steps below are for running them by hand in
Workbench, which is what you want for the demo video.

## Files

| File | Purpose |
|---|---|
| `01_schema.sql` | Creates `moviedb` and the 8 tables with keys, foreign keys and CHECK constraints. Drops and recreates the database, so it is safe to re-run. |
| `01b_create_user.sql` | Creates three accounts with different privileges: `inf2003` (the app: reads everything, writes only `users` and `ratings`, no DDL), `inf2003_report` (SELECT on the six views only), `inf2003_bench` (owns the `moviedb_bench` scratch database). Section D holds statements that must be refused. **Run after `04_views.sql`**: a `GRANT` on a view that does not exist yet fails with error 1146. |
| `02_load.sql` | Bulk-loads the cleaned CSVs with `LOAD DATA LOCAL INFILE`, parents before children. |
| `03_verify.sql` | Proves the constraints exist, checks integrity (all counts must be 0), and contains deliberate-violation tests. |
| `04_views.sql` | Six views used by browse, reports and analytics. |
| `05_crud.sql` | Create, read, update and delete for users and ratings. Cleans up after itself. |
| `06_queries.sql` | Search, movie detail, reports and analytics queries. Read-only. |
| `07_indexes.sql` | Indexing experiments with `EXPLAIN ANALYZE`; adds two composite indexes and a FULLTEXT index. Rerun after any rebuild. |
| `08_triggers.sql` | The `rating_audit` table and five triggers: three that record every change to a rating, and two that enforce a cross-table rule a `CHECK` cannot express. |
| `09_transactions.sql` | Atomicity, `SAVEPOINT`, isolation levels, locking and deadlock, and optimistic concurrency. Sections D and E need two connections. |

## Connection details

Everyone runs MySQL on their own machine, so each person sets their own
passwords. No real password is ever committed.

| Setting | Value |
|---|---|
| Host | `127.0.0.1` |
| Port | `3306` (MySQL default). If another server such as MariaDB already uses 3306, install MySQL on a free port (e.g. `3307`) and use that everywhere below. |
| Admin account | `root`, password set in the MySQL installer. Used in Workbench and by `setup_all.py` only. |
| Application account | `inf2003`, created by `01b_create_user.sql`. Used by the Python scripts and the back end. |
| Reporting account | `inf2003_report`. SELECT on the views only; cannot read the base tables. |
| Benchmark account | `inf2003_bench`. Owns `moviedb_bench`, reads `moviedb`. |
| Database | `moviedb` |

**Workbench connection:** Home → **+** next to *MySQL Connections* →
Hostname `127.0.0.1`, Port as above, Username `root` → **Advanced** tab →
in *Others* add `OPT_LOCAL_INFILE=1` → Test Connection.
Run `SELECT VERSION();` after connecting: it must start with `8.0`
(a `10.x` or `11.x-MariaDB` result means you are on the wrong port).

**`.env`** (repo root, listed in `.gitignore`, never committed):
```
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=inf2003
MYSQL_PASSWORD=<the password you want the account to have>
MYSQL_DATABASE=moviedb
```

`setup_all.py` reads that password and substitutes it into
`01b_create_user.sql` in place of `CHANGE_ME_APP`, so the two can never
drift apart.

## Setup (MySQL Workbench, by hand)

1. Enable local file loading once per server start:
   `SET GLOBAL local_infile = 1;`
   and add `OPT_LOCAL_INFILE=1` under the connection's **Advanced** tab.
2. Run `01_schema.sql`.
3. In `02_load.sql`, replace every `CHANGE/ME/cleaned` with the full
   path to this repo's `data/cleaned` folder, using forward slashes
   (e.g. `C:/projects/AAI_DatabaseSystems_G37/data/cleaned`), then run
   it. All 8 tables must show `rows_loaded = expected`.
4. Run `03_verify.sql`, then `04_views.sql`.
5. Run `01b_create_user.sql` after replacing the three `CHANGE_ME_*`
   placeholders. It has to come after step 4 because it grants `SELECT`
   on the views.
6. `05_crud.sql` and `06_queries.sql` can be run at any time after that.
7. Run `07_indexes.sql`. It keeps the indexes it adds.
8. Run `08_triggers.sql`, then `09_transactions.sql`.

To start again from scratch: `01`, `02`, `04`, `01b`, `07`, `08` — or
just `python setup_all.py` from the repo root, which does exactly that
and fills in the passwords and paths for you.

Performance benchmarks are in `benchmark/` (see `benchmark/README.md`).

## What each script is there to demonstrate

- **`01_schema.sql`** — the design: two many-to-many relationships
  (`movie_genres`, and cast/crew through `movie_cast` / `movie_crew`),
  composite primary keys used as business rules, and `CASCADE` versus
  `RESTRICT` chosen per relationship rather than uniformly.
- **`03_verify.sql`** — that the constraints are real. Section D is a
  list of statements that must each fail; run them one at a time and
  screenshot the errors.
- **`04_views.sql`** — window functions (`RANK() OVER (PARTITION BY)`),
  CTEs, and a Bayesian weighted score so that one 10/10 vote cannot top
  the chart.
- **`06_queries.sql`** — `UNION ALL`, `EXISTS`, a correlated subquery, a
  self-join, `GROUP BY ... WITH ROLLUP`, `DENSE_RANK`, `ROW_NUMBER`,
  `LAG`, and `JSON_OBJECT` / `JSON_ARRAYAGG`.
- **`07_indexes.sql`** — the same query before and after an index, read
  from `EXPLAIN ANALYZE`: table scan versus index lookup versus
  covering index, and `LIKE '%x%'` versus a prefix `LIKE` versus
  `FULLTEXT`.
- **`08_triggers.sql`** — an audit trail the application cannot forge
  (it has no privilege on the table, but the triggers run as their
  definer), and the rule "you cannot rate a film before it is
  released", which needs another table and therefore cannot be a
  `CHECK`. It also documents an honest limitation: MySQL does not fire
  triggers for rows removed by `ON DELETE CASCADE`.
- **`09_transactions.sql`** — why `autocommit` is not enough, what
  `SAVEPOINT` buys, the same query returning two different answers
  under two isolation levels, a deadlock produced on purpose, and the
  optimistic alternative to locking.

## Shared keys with MongoDB

- `movies.movie_id` = TMDB id = `movie_features.movie_id`
- `users.user_id` = `user_id` in every MongoDB collection
- `people.person_id` = TMDB person id = `cast[].person_id` /
  `crew[].person_id` / `people_graph.person_id`

None of these is a foreign key: MongoDB cannot check them and MySQL
cannot see them, so the application is responsible. `webapp/routes.py`
(`delete_user`) is where that responsibility is handled explicitly.
