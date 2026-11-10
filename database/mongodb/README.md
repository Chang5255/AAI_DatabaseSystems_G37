# Non-relational database (MongoDB)

MongoDB Community Server **5.0 or later**. The project uses `$facet`,
`$graphLookup`, `$bucket`, multikey indexes, TTL indexes and
`$jsonSchema` validators.

The whole MongoDB side builds with one command from the repo root:

```bash
python database/mongodb/setup_mongodb.py
```

(or `python setup_all.py`, which does MySQL as well). Default
connection: `mongodb://localhost:27017/`, database `movie_app`.

## Collections

| Collection | Documents | Design scheme (lecture 8) | What it holds |
|---|---|---|---|
| `movie_features` | 4,803 | **denormalisation** | one document per film: overview, keywords, cast, crew, companies, countries, languages, poster path |
| `user_tags` | 72,600 | **designed from scratch** | free-text labels a user puts on a film. **Real** MovieLens 25M tag applications |
| `user_activity_logs` | 9,797 | **designed from scratch** | append-only behaviour stream; `metadata` differs per event type. **Synthetic**, derived from the real ratings |
| `recommendations` | grows | **designed from scratch** | one document per generated list, kept as history |
| `people_graph` | ~48,000 | **derived** | the co-star adjacency list that `$graphLookup` walks |

`docs/mongodb_schema.md` has the full document shapes, the index list
and a side-by-side comparison with the MySQL design.

## Files

| File | Purpose |
|---|---|
| `event_types.py` | The allowed values of `event_type`, in one place. Imported by the validator **and** by the web app, so they cannot disagree. |
| `create_collections.py` | Creates the five collections with `$jsonSchema` validators (`validationLevel="strict"`, `validationAction="error"`). Run directly and it also tries to insert five bad documents to prove they are refused. Re-runnable: uses `collMod`. |
| `import_movie_features.py` | Imports `data/cleaned/movie_features.jsonl` with `bulk_write` upserts. |
| `import_posters.py` | Adds `poster_path` from `data/cleaned/posters.csv`. |
| `import_user_data.py` | Loads `user_tags.jsonl` (72,600 **real** MovieLens tags, upserted on the unique key) and `user_activity_logs.jsonl` (9,797 **synthetic** events, whose timestamps are made relative to the moment of import so the TTL index does not delete them). Idempotent. |
| `fetch_posters.py` | Refreshes that CSV from the TMDB API (needs `TMDB_API_KEY` in `.env`). Optional. |
| `build_costar_graph.py` | Derives `people_graph` from the `cast` arrays, using the top 10 billed cast of each film. |
| `create_indexes.py` | Every index: multikey, compound, text, one **unique** and one **TTL**. Removes duplicate tags first, because MongoDB refuses to build a unique index over data that already violates it. |
| `crud_user_tags.py` | Create, read, update, delete for tags. |
| `activity_logs.py` | Writing and reading the activity stream. |
| `aggregation_queries.py` | The basic pipelines: `$unwind`, `$group`, `$sort`, `$limit`. |
| `advanced_queries.py` | `$graphLookup` (co-star network), breadth-first search for the actual chain, `$facet` with a nested `$bucket`, `$lookup`, `$sortByCount`. |
| `generate_recommendations.py` | MongoDB-only recommender: keywords of the films a user viewed. |
| `hybrid_recommendations.py` | Recommender that reads MySQL ratings and favourite genres and scores candidates in MongoDB. |
| `recommendations.py` | Storing and reading recommendation documents. |
| `seed_test_data.py` | A few activity documents for user 1, for testing. |
| `test_connection.py`, `test_mongodb.py` | Verification. |

## Setup order, and why it matters

```
1. create_collections.py      validators first, so everything inserted
                              afterwards is validated on the way in
2. import_movie_features.py   4,803 film documents
3. import_posters.py          poster paths
4. import_user_data.py        72,600 real tags + 9,797 activity events
5. build_costar_graph.py      needs the films to exist
6. create_indexes.py          last: indexing after a bulk load is
                              faster than maintaining indexes during
                              it, and the unique tag index has to see
                              the data to check it
```

`setup_mongodb.py` runs exactly that sequence and stops on the first
failure. `--skip-posters`, `--skip-user-data` and `--skip-graph` skip
steps 3, 4 and 5.

## Constraints in a schema-less database

MongoDB will accept any document unless it is told otherwise. This
project tells it otherwise in three different ways, and the report
should name all three:

| Rule | How |
|---|---|
| required fields, types, ranges, string lengths | `$jsonSchema` in `create_collections.py` |
| `event_type` must be one of a fixed list | `enum` in the same validator, fed from `event_types.py` |
| one user cannot tag the same film twice | **unique index** on `(user_id, movie_id, tag)` — a validator cannot express uniqueness |
| activity logs expire after 90 days | **TTL index** on `timestamp` — MySQL needs a scheduled job for this. It is also why `user_activity_logs.jsonl` stores offsets rather than dates: fixed historical timestamps would be deleted within a minute of import |
| `user_id` must be a real user | **not possible.** No foreign keys. The application does it. |

That last row is the honest one. A tag can point at a film or a user
that does not exist, and the `$lookup` on the Insights page exists
partly to find those orphans — the integrity check MySQL would have
made for us at write time.

## Running the demos

Each of these prints something useful when run directly from the repo
root:

```bash
python database/mongodb/create_collections.py       # validators + 5 rejected documents
python database/mongodb/create_indexes.py           # every index, with its kind and size
python database/mongodb/import_user_data.py         # real tags + synthetic activity
python database/mongodb/crud_user_tags.py           # tag CRUD
python database/mongodb/activity_logs.py            # append-only logging
python database/mongodb/aggregation_queries.py      # the basic pipelines
python database/mongodb/advanced_queries.py         # $graphLookup, $facet, $bucket, $lookup
python database/mongodb/build_costar_graph.py       # graph stats and storage cost
python database/mongodb/generate_recommendations.py             # MongoDB only
python database/mongodb/hybrid_recommendations.py --user 1      # both databases
python database/mongodb/test_mongodb.py             # verification
```

## Shared keys with MySQL

- `movie_features.movie_id` = `movies.movie_id` (the TMDB id)
- `user_id` in every collection = `users.user_id`
- `cast[].person_id` / `crew[].person_id` / `people_graph.person_id` =
  `people.person_id`
