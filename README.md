# AAI_DatabaseSystems_G37

Movie app backed by two databases that share the same ids:

- **MySQL** (`moviedb`): movies, genres, people, users, ratings, cast/crew links. See `database/mysql/README.md`.
- **MongoDB** (`movie_app`): `movie_features` (embedded cast/crew), `user_tags`, `user_activity_logs`, `recommendations`. See `database/mongodb/README.md`.

Shared keys: `movies.movie_id` = `movie_features.movie_id`, `users.user_id` = `user_id` in every MongoDB collection, `people.person_id` = `cast[].person_id` / `crew[].person_id`.

## Layout

```
data/cleaned/        cleaned TMDB CSVs + movie_features.jsonl (used by both databases)
database/mysql/      SQL scripts 01-07
database/mongodb/    Python setup, CRUD, aggregation and recommendation scripts
benchmark/           MySQL vs MongoDB timing experiments
app.py, webapp/      Flask web UI over both databases
.env.example         settings for both databases
requirements.txt     Python packages for both databases
```

## Setup

1. `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in your MySQL password (never commit `.env`).
3. MySQL: follow `database/mysql/README.md` (scripts 01 to 07).
4. MongoDB: follow `database/mongodb/README.md`.
5. Posters: the MongoDB setup loads `data/cleaned/posters.csv` into `movie_features.poster_path`
   (no key needed; the images load from TMDB, so the UI needs internet to show them).
   To refresh the file from TMDB, put a free API key in `.env` as `TMDB_API_KEY` and run
   `python database/mongodb/fetch_posters.py`.
6. Benchmarks: see `benchmark/README.md`. Run scripts from the repo root.
7. Web UI: with both databases loaded and the virtual environment active, run `python app.py`
   and open http://127.0.0.1:5000. Stop it with Ctrl+C.

## Web UI

| Page | Database | What it shows |
|---|---|---|
| Browse | MySQL | Search by title, genre, year, language (Q1 on `v_movie_cards`) |
| Discover | MongoDB | Keyword search on `keywords.name`; overview text search (text index) |
| People | MySQL | Every credit for a person (Q2, `UNION ALL`) |
| Film page | Both | MySQL: details, ratings CRUD. MongoDB: overview, keywords, cast/crew, tags CRUD |
| Users / profile | Both | MySQL: user CRUD, ratings, favourite genre. MongoDB: tags, activity log, recommendations |
| Reports | Both | Queries Q4-Q14 from `06_queries.sql` and the MongoDB aggregation pipelines, with their code |
| SQL vs NoSQL | Both | Benchmark tasks T1-T3 timed live with `benchmark/harness.py` |

Pick a user in the top bar ("Acting as") to rate films, add tags and have views logged.
Every page lists the database calls it made and how long each took (bar at the bottom).

On first start the UI adds one MongoDB index, `idx_overview_text` (a text index on
`movie_features.overview`), which the overview search needs.
