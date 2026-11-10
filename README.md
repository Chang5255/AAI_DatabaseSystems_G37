# AAI_DatabaseSystems_G37

INF2003 Database Systems group project, Team 37. A movie discovery and
ratings application backed by two databases that share the same ids:

- **MySQL** (`moviedb`): movies, genres, people, users, ratings,
  cast/crew links, six views, five triggers and an audit trail. This is
  where the integrity guarantees and the joins live.
  See `database/mysql/README.md`.
- **MongoDB** (`movie_app`): `movie_features` (one document per film,
  with cast and crew embedded), `user_tags`, `user_activity_logs`,
  `recommendations`, and `people_graph` (a derived co-star graph). This
  is where the flexible, nested and append-only data lives.
  See `database/mongodb/README.md`.

Shared keys: `movies.movie_id` = `movie_features.movie_id`,
`users.user_id` = `user_id` in every MongoDB collection,
`people.person_id` = `cast[].person_id` / `crew[].person_id` /
`people_graph.person_id`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then set MYSQL_PASSWORD
python setup_all.py           # builds both databases, asks for your MySQL root password
python app.py                 # http://127.0.0.1:5000
```

`USER_MANUAL.md` is the full version, including what to install, what
to demonstrate, and what to do when something fails.

## Layout

```
app.py                starts the web application
setup_all.py          one-command setup for both databases
.env.example          settings for both databases
requirements.txt      Python packages for both databases

data/cleaned/         cleaned TMDB CSVs, MovieLens ratings and tags, movie_features.jsonl
data/build_movielens_ratings.py  rebuilds the ratings from ml-25m.zip
database/mysql/       SQL scripts 01-09
database/mongodb/     Python setup, CRUD, aggregations, graph, recommenders
benchmark/            six performance experiments, MySQL vs MongoDB
webapp/               Flask UI over both databases
docs/                 ER diagram, MongoDB schema, dataset research
```

## Web UI

| Page | Database | What it shows |
|---|---|---|
| Browse | MySQL | Search by title, genre, year, language (Q1 on `v_movie_cards`) |
| Discover | MongoDB | Keyword search on `keywords.name`; overview text search (text index) |
| People | MySQL | Every credit for a person (Q2, `UNION ALL`) |
| Film page | both | MySQL: details, ratings CRUD. MongoDB: overview, keywords, cast/crew, tags CRUD |
| Insights | MongoDB | `$facet` (six breakdowns in one pass), `$bucket`, `$lookup` |
| Network | MongoDB | Degrees of separation between two actors, via `$graphLookup` |
| Reports | both | Q4–Q14 from `06_queries.sql` and the MongoDB pipelines, with their code |
| Audit | MySQL | Every change to a rating, written by the triggers in `08_triggers.sql` |
| Users / profile | both | MySQL: user CRUD, ratings, favourite genre, batch import in one transaction. MongoDB: tags, activity log, two recommenders |
| SQL vs NoSQL | both | Benchmark tasks T1–T3 timed live with `benchmark/harness.py` |

Pick a user in the top bar ("Acting as") to rate films, add tags and
have views logged. Every page lists the database calls it made and how
long each took (bar at the bottom).

## What to look at, if you are marking this

| Theme | Where |
|---|---|
| ER design, keys, 1-to-many and many-to-many | `database/mysql/01_schema.sql`, `docs/er_diagram.md` |
| Constraints, and proof they are enforced | `01_schema.sql`, `03_verify.sql` (section D fails on purpose) |
| Non-standard constraints a `CHECK` cannot express | `08_triggers.sql` |
| Views, window functions, CTEs, `ROLLUP` | `04_views.sql`, `06_queries.sql` |
| Query optimisation and `EXPLAIN ANALYZE` | `07_indexes.sql` |
| Transactions, savepoints, isolation, deadlock | `09_transactions.sql`, `webapp/routes.py` (`/import-ratings`) |
| Security: least privilege and view-based access | `01b_create_user.sql` (section D is refused on purpose) |
| NoSQL schema design and validators | `database/mongodb/create_collections.py`, `docs/mongodb_schema.md` |
| Advanced MongoDB: `$graphLookup`, `$facet`, `$bucket`, `$lookup` | `database/mongodb/advanced_queries.py` |
| An algorithm using both databases together | `database/mongodb/hybrid_recommendations.py` |
| Performance analysis, speed and throughput | `benchmark/README.md` and `benchmark/results/` |
| Real data, and an honest line between real and synthetic | `data/build_movielens_ratings.py`, `data/cleaned/movielens_report.txt` |
| The hard part: no transaction spans both databases | `webapp/routes.py`, `delete_user` |

## Data

**Catalogue** — 4,803 films from the TMDB 5000 dataset, cleaned by the
script whose report is in `data/cleaned/cleaning_report.txt`: 104,842
people, 106,257 cast credits, 129,581 crew credits, 12,160 genre links.

**Ratings and tags** — real, from
[MovieLens 25M](https://grouplens.org/datasets/movielens/25m/), joined
to our films on the TMDB id in its `links.csv`:

| | Rows | Source |
|---|---|---|
| `users` | 1,512 | 12 synthetic demo accounts (ids 1–12) + **1,500 real** MovieLens users (ids 1001+) |
| `ratings` | 246,976 | 784 synthetic + **246,192 real**, spanning 1996–2019 |
| `user_tags` | 72,600 | **real** MovieLens tag applications |
| `user_activity_logs` | 9,797 | **synthetic**, derived from the real ratings — no public dataset has page views |

`user_id > 1000` means a real user, so the split is checkable in SQL
(`03_verify.sql` section C5 prints it). MovieLens rates in half-star
steps from 0.5 to 5.0, which is exactly what our `CHECK` constraints
already required, so the schema did not change to accept it.
`data/build_movielens_ratings.py` is the code and
`data/cleaned/movielens_report.txt` the audit trail;
`docs/dataset_sources.md` explains why this dataset and not another.

Cite GroupLens if you use the ratings: F. M. Harper and J. A. Konstan,
"The MovieLens Datasets: History and Context," *ACM TiiS* 5(4), 2015,
<https://doi.org/10.1145/2827872>.

Posters come from TMDB over the internet, so the UI needs a connection
to show them; everything else works offline. This product uses the TMDB
API but is not endorsed or certified by TMDB.
