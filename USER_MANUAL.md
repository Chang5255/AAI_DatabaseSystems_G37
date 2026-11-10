# User manual

INF2003 Database Systems, Team 37 — Movie Discovery application
(MySQL + MongoDB).

Follow this from top to bottom on a clean machine and the application
will run. Everything after section 4 is optional.

---

## 1. What you need installed

| Software | Version | Why |
|---|---|---|
| Python | 3.10 or later | the application and every script |
| MySQL Community Server | **8.0.19 or later** | `CHECK` constraints are only enforced from 8.0.16, and the upsert uses the row-alias syntax added in 8.0.19 |
| MongoDB Community Server | 5.0 or later | `$facet`, `$graphLookup`, `$bucket`, TTL and multikey indexes |
| MySQL Workbench | any | optional, for running the `.sql` scripts by hand and screenshotting constraint errors |
| MongoDB Compass | any | optional, for looking at the documents |

Check what you have:

```bash
python --version
mysql --version          # must say 8.0.19 or higher, NOT 10.x / MariaDB
mongod --version
```

If `mysql --version` reports `10.x-MariaDB`, MariaDB is answering on
port 3306. Install MySQL on a free port such as 3307 and use that port
everywhere below.

Both servers must be **running** before you start. On Windows they are
usually services (`services.msc` → *MySQL80*, *MongoDB*); on macOS with
Homebrew, `brew services start mysql` and `brew services start mongodb-community`.

---

## 2. Get the code and the Python packages

```bash
cd AAI_DatabaseSystems_G37

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

The data is already in `data/cleaned/` (12 files, about 55 MB), so
there is nothing to download — including the real MovieLens ratings and
tags. To rebuild those from the original archive instead, see
`data/build_movielens_ratings.py`; it needs
[ml-25m.zip](https://files.grouplens.org/datasets/movielens/ml-25m.zip)
(250 MB) and takes about a minute.

---

## 3. Settings

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

Open `.env` and set **`MYSQL_PASSWORD`** to a password of your choice.
That is the password the application account will be created with — not
your MySQL root password. If MySQL is on a different port, change
`MYSQL_PORT` too.

`.env` is listed in `.gitignore` and must never be committed.

---

## 4. Build both databases (one command)

```bash
python setup_all.py
```

It asks for your MySQL **root** password, warns you that it is about to
drop any existing `moviedb`, and then:

- runs `database/mysql/01_schema.sql` (database, 8 tables, keys, CHECKs)
- runs `01b_create_user.sql`, filling in the passwords from `.env`
- runs `02_load.sql`, filling in the path to `data/cleaned` itself, and
  enabling `local_infile` for the bulk load
- runs `04_views.sql`, `07_indexes.sql` and `08_triggers.sql`
- runs `database/mongodb/setup_mongodb.py`, which creates the
  collections and validators, imports 4,803 film documents and the
  poster paths, builds the co-star graph, and creates every index
- prints row counts, and the number of views, triggers, constraints and
  indexes it found

Expected output at the end:

```
    ok movies            4,803  (expected 4,803)
    ok people          104,842  (expected 104,842)
    ok movie_cast      106,257  (expected 106,257)
    ok movie_crew      129,581  (expected 129,581)
    ok users             1,512  (expected 1,512)
    ok ratings         246,976  (expected 246,976)
    ...
    ok movie_features     4,803 documents, 6 index(es)
    ok user_tags         72,600 documents, 5 index(es)
    ok user_activity_logs 9,797 documents, 5 index(es)
Setup finished.
```

The load takes a few seconds longer than it used to: `ratings.csv` is
now 247,000 rows of real MovieLens data rather than 784 synthetic ones,
and `user_tags.jsonl` is 72,600 real tag applications.

Useful variations:

```bash
python setup_all.py --with-demos    # also run 03, 05, 06, 09 and print their output
python setup_all.py --mongo-only    # rebuild MongoDB only
python setup_all.py --mysql-only    # rebuild MySQL only
python setup_all.py -y              # do not ask for confirmation
```

If it fails, see section 9.

---

## 5. Run the application

```bash
python app.py
```

Open <http://127.0.0.1:5000>. Stop it with `Ctrl+C`.

First thing to do: pick a user in the **Acting as** box at the top
right. Without one you can browse, but you cannot rate or tag, and
nothing is logged.

### The pages

| Page | Database | What to look at |
|---|---|---|
| **Browse** | MySQL | Search by title, genre, year and language — one query against the `v_movie_cards` view |
| **Discover** | MongoDB | Keyword search (multikey index) and full-text search on the overview (text index) |
| **People** | MySQL | Every credit for one person, cast and crew stacked with `UNION ALL` |
| **Film page** | both | MySQL gives the details and the ratings; MongoDB gives the overview, keywords, cast, crew and tags. Rate it, change the rating, add a tag |
| **Insights** | MongoDB | Six breakdowns from one `$facet` pass, a `$bucket` histogram, and a `$lookup` join between two collections |
| **Network** | MongoDB | Degrees of separation between two actors, via `$graphLookup` over the derived `people_graph` collection |
| **Reports** | both | 14 SQL queries and 6 MongoDB pipelines, each shown with its source |
| **Audit** | MySQL | Every change ever made to a rating, written by triggers — including changes made in Workbench |
| **SQL vs NoSQL** | both | The benchmark tasks, timed live in the browser |
| **Users** | both | Create, edit and delete users; import a batch of ratings in one transaction |

Every page has a bar at the bottom showing each database call it made
and how long it took.

### Five things worth trying

1. **A constraint refusing a write.** On any film, rate it, then try to
   create a user called `ab` on the Users page: the `CHECK` on username
   length refuses it, and the error from MySQL is shown as-is.
2. **The audit trail.** Rate a film 4 stars, change it to 2, then
   delete the rating, and open **Audit**: three rows, with the old and
   new values and the account that made each change.
3. **Savepoints.** On a profile, follow *Import a batch* and paste:
   ```
   19995, 4.5
   155, 5.0
   27205, 3.7
   999999999, 4.0
   603, 3.5
   ```
   Lines 3 and 4 are refused (not a half-star step; no such film) and
   the other three are committed. Then choose *All or nothing* and
   import the same text: nothing is kept.
4. **Two recommenders.** On a profile, press *From views* (MongoDB
   only) and then *Hybrid* (MySQL ratings + MongoDB keywords) and
   compare the lists and the explanation under each one.
5. **Degrees of separation.** On **Network**, try `Tom Hanks` and
   `Scarlett Johansson`.
6. **Real data at the edges.** On **Users**, filter to *Real MovieLens
   users only* and open the busiest one: hundreds of real ratings going
   back to the 1990s, a favourite genre computed from them, and real
   tags other people wrote on the same films. Then open **Insights** to
   see what 72,600 real tags look like when grouped.

---

## 6. Running the SQL scripts by hand (for the demo video)

Several scripts are written to be read and screenshotted, not just run.
In MySQL Workbench, connect as **root**, and for `02_load.sql` add
`OPT_LOCAL_INFILE=1` under the connection's *Advanced* → *Others* tab.

| Script | Run it to show |
|---|---|
| `01_schema.sql` | the schema: keys, foreign keys with `CASCADE` vs `RESTRICT`, `CHECK` constraints |
| `01b_create_user.sql` | three accounts with different privileges. **Section D** has statements that must be refused — run them in a connection opened as `inf2003` or `inf2003_report` and screenshot each error |
| `03_verify.sql` | proof the constraints exist, integrity checks that must all return 0, and **section D**, deliberate violations: run those one at a time and screenshot the errors |
| `04_views.sql` | the six views, including a Bayesian weighted score and `RANK() OVER (PARTITION BY ...)` |
| `05_crud.sql` | create, read, update, delete for users and ratings; cleans up after itself |
| `06_queries.sql` | the 14 reporting and analytics queries (`UNION ALL`, `EXISTS`, correlated subquery, self-join, `WITH ROLLUP`, `LAG`, `DENSE_RANK`, CTEs) |
| `07_indexes.sql` | `EXPLAIN ANALYZE` before and after each index; read the text cell with right-click → *Open Value in Viewer* |
| `08_triggers.sql` | the audit table, the trigger that enforces a cross-table rule a `CHECK` cannot, and the documented hole where `ON DELETE CASCADE` bypasses triggers |
| `09_transactions.sql` | atomicity, `SAVEPOINT`, isolation levels and a deadlock. **Sections D and E need two connections** — the script says which block to run in which one |

MongoDB scripts, each of which prints a demo when run directly:

```bash
python database/mongodb/create_collections.py     # validators, then tries to insert 5 bad documents
python database/mongodb/import_user_data.py       # real tags + the synthetic activity stream
python database/mongodb/crud_user_tags.py         # tag CRUD
python database/mongodb/activity_logs.py          # append-only logging
python database/mongodb/aggregation_queries.py    # the basic pipelines
python database/mongodb/advanced_queries.py       # $graphLookup, $facet, $bucket, $lookup
python database/mongodb/generate_recommendations.py          # MongoDB-only recommender
python database/mongodb/hybrid_recommendations.py --user 1   # both databases
python database/mongodb/test_mongodb.py           # verification tests
```

---

## 7. Running the benchmarks

```bash
python benchmark/mysql_bench.py          # writes sample_ids.json; run this first
python benchmark/mongo_bench.py
python benchmark/mongo_index_bench.py
python benchmark/ratings_scaling.py      # about 5 minutes
python benchmark/throughput_bench.py
python benchmark/txn_cost_bench.py
python benchmark/load_storage_bench.py
python benchmark/merge_results.py        # builds results/ALL_RESULTS.md
```

Results land in `benchmark/results/`. See `benchmark/README.md` for what
each experiment measures and what it does and does not prove. Every
script cleans up after itself; none of them changes the catalogue or any
real user's ratings.

---

## 8. Project layout

```
app.py                  starts the web application
setup_all.py            one-command setup for both databases
.env                    your settings (never committed)
data/cleaned/           the cleaned TMDB data used by both databases
database/mysql/         SQL scripts 01 to 09
database/mongodb/       Python setup, CRUD, aggregations, graph, recommenders
benchmark/              the six performance experiments
webapp/                 Flask application (routes, queries, templates)
docs/                   ER diagram and MongoDB schema diagram
```

---

## 9. When something goes wrong

**`Access denied for user 'inf2003'@'localhost'`**
The password in `.env` is not the one the account was created with.
Re-run `python setup_all.py`, which recreates the account from `.env`.

**`Unknown database 'moviedb'`**
`01_schema.sql` has not run. `python setup_all.py`.

**`Table 'moviedb.rating_audit' doesn't exist`** (Audit page)
`08_triggers.sql` has not run. `python setup_all.py`, or run that script
in Workbench as root.

**`The used command is not allowed with this MySQL version`** during the
load. `local_infile` is off. `setup_all.py` turns it on; by hand it is
`SET GLOBAL local_infile = 1;` as root, plus `OPT_LOCAL_INFILE=1` in
Workbench's *Advanced* tab.

**`Check constraint ... is violated`** when loading.
The server is older than 8.0.16, or the CSVs have been edited. Check
`SELECT VERSION();`.

**`ServerSelectionTimeoutError`** from any MongoDB script.
`mongod` is not running, or `MONGODB_URI` is wrong.

**The Network page says the graph is not built.**
`python database/mongodb/build_costar_graph.py`.

**Posters do not appear.**
They load from TMDB over the internet, so the machine needs a
connection. Everything else works without one.

**`sample_ids.json is missing`** from a benchmark.
Run `python benchmark/mysql_bench.py` first; it draws the ids that every
benchmark shares.

**Port 5000 is in use** (macOS AirPlay often holds it).
`python app.py` then open the port Flask prints, or set
`FLASK_RUN_PORT=5001`.

---

## 10. Starting over

```bash
python setup_all.py -y        # rebuilds MySQL and MongoDB from scratch
```

To remove everything instead, as root in MySQL:

```sql
DROP DATABASE IF EXISTS moviedb;
DROP DATABASE IF EXISTS moviedb_bench;
DROP USER IF EXISTS 'inf2003'@'localhost',
                    'inf2003_report'@'localhost',
                    'inf2003_bench'@'localhost';
```

and in `mongosh`:

```javascript
use movie_app
db.dropDatabase()
```
