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
.env.example         settings for both databases
requirements.txt     Python packages for both databases
```

## Setup

1. `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in your MySQL password (never commit `.env`).
3. MySQL: follow `database/mysql/README.md` (scripts 01 to 07).
4. MongoDB: follow `database/mongodb/README.md`.
5. Benchmarks: see `benchmark/README.md`. Run scripts from the repo root.
