# Relational database (MySQL)

MySQL **8.0.16 or later** is required (CHECK constraints are only enforced
from 8.0.16). Developed and tested on MySQL 8.0.46.

## Files

| File | Purpose |
|---|---|
| `01_schema.sql` | Creates `moviedb` and the 8 tables with keys, foreign keys and CHECK constraints. Drops and recreates the database, so it is safe to re-run. |
| `01b_create_user.sql` | Creates the `inf2003` application account with access to `moviedb` only. Run after `01`. |
| `02_load.sql` | Bulk-loads the cleaned CSVs with `LOAD DATA LOCAL INFILE`, parents before children. |
| `03_verify.sql` | Proves the constraints exist, checks integrity (all counts must be 0), and contains deliberate-violation tests. |
| `04_views.sql` | Six views used by browse, reports and analytics. |
| `05_crud.sql` | Create, read, update and delete for users and ratings. Cleans up after itself. |
| `06_queries.sql` | Search, movie detail, reports and analytics queries. Read-only. |
| `07_indexes.sql` | Indexing experiments with `EXPLAIN ANALYZE`; adds two composite indexes and a FULLTEXT index. Rerun after any rebuild. |

## Connection details

Everyone runs MySQL on their own machine, so each person sets their own
passwords. No real password is ever committed.

| Setting | Value |
|---|---|
| Host | `127.0.0.1` |
| Port | `3306` (MySQL default). If another server such as MariaDB already uses 3306, install MySQL on a free port (e.g. `3307`) and use that everywhere below. |
| Admin account | `root`, password set in the MySQL installer. Used in Workbench only. |
| Application account | `inf2003`, created by `01b_create_user.sql`. Used by Python scripts and the back end. Can access `moviedb` and `moviedb_bench` only. |
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
MYSQL_PASSWORD=<password you set in 01b_create_user.sql>
MYSQL_DATABASE=moviedb
```

## Setup (MySQL Workbench)

1. Enable local file loading once per server start:
   `SET GLOBAL local_infile = 1;`
   and add `OPT_LOCAL_INFILE=1` under the connection's **Advanced** tab.
2. Run `01_schema.sql`.
3. Run `01b_create_user.sql` after replacing `CHANGE_ME`.
4. In `02_load.sql`, replace every `CHANGE/ME/cleaned` with the full path
   to this repo's `data/cleaned` folder, using forward slashes
   (e.g. `C:/projects/AAI_DatabaseSystems_G37/data/cleaned`), then run it. All 8 tables must show
   `rows_loaded = expected`.
5. Run `03_verify.sql`, then `04_views.sql`.
6. `05_crud.sql` and `06_queries.sql` can be run at any time after that.
7. Run `07_indexes.sql` last. It keeps the indexes it adds.

To start again from scratch, run `01`, `02`, `04`, then `07`.

Performance benchmarks are in `benchmark/` (see `benchmark/README.md`).

## Shared keys with MongoDB

- `movies.movie_id` = TMDB id = `movie_features.movie_id`
- `users.user_id` = `user_id` in every MongoDB collection
- `people.person_id` = TMDB person id = `cast[].person_id` / `crew[].person_id`
