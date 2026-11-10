# Adding real ratings: which dataset, and why

**This is now done.** The ratings in the project are real MovieLens 25M
data: 1,500 real users, 246,192 real ratings and 72,600 real tags, on
top of the 12 synthetic demo accounts and their 784 ratings, which were
kept. `data/build_movielens_ratings.py` is the code,
`data/cleaned/movielens_report.txt` the audit trail.

This file records **why that dataset and not another**, and the
decisions the import had to make. It was written as research before the
work and is kept as the justification afterwards, which is what the
report needs.

The starting point: the catalogue (films, genres, people, cast, crew)
was always real TMDB data, but the ratings and users were synthetic —
12 users and 784 ratings, because TMDB publishes only an aggregate
`vote_average` and never a per-user rating. 784 rows could not support
the analytics the project is built around, and the rubric rewards
"appropriate & sufficient data sources collected/used".

---

## Recommendation: MovieLens 25M

**Download:** <https://grouplens.org/datasets/movielens/25m/> →
`ml-25m.zip` (250 MB)
**Direct file:** <https://files.grouplens.org/datasets/movielens/ml-25m.zip>
**README:** <https://files.grouplens.org/datasets/movielens/ml-25m-README.html>

### Why this one

1. **It maps onto our films with no guesswork.** The archive includes
   `links.csv`, whose format is `movieId,imdbId,tmdbId`. The `tmdbId`
   column is exactly the key our two databases already share
   (`movies.movie_id` in MySQL, `movie_features.movie_id` in MongoDB),
   so joining is a lookup, not a fuzzy title match.
2. **The rating scale already matches our constraints.** MovieLens uses
   a "5-star scale in half-star increments, from 0.5 to 5.0". Our
   `ratings` table enforces exactly that, with
   `CHECK (rating_value BETWEEN 0.5 AND 5.0)` and
   `CHECK (rating_value * 2 = FLOOR(rating_value * 2))`. The real data
   satisfies the constraints we designed before we had it, which is a
   point worth making in the report.
3. **Size, and the freedom to choose a size.** 25,000,095 ratings from
   162,541 users across 62,423 movies. Filtering to our 4,803 films and
   then sampling users lets us pick any volume we want, which also
   feeds the scaling experiment.
4. **It is the standard.** GroupLens calls it the "latest stable
   version" and recommends it for research, so it needs no defending.

### Licence and citation

The data may be used for research purposes; the University of Minnesota
disclaims any warranty. The README requires this citation, which should
go in the report's reference list:

> F. M. Harper and J. A. Konstan, "The MovieLens Datasets: History and
> Context," *ACM Transactions on Interactive Intelligent Systems*,
> vol. 5, no. 4, pp. 19:1–19:19, 2015. <https://doi.org/10.1145/2827872>

(Only needed if the tag genome files are used as well: Vig, Sen and
Riedl, *ACM TiiS* 2(3), 2012.)

---

## The quick alternative, for testing the pipeline

**MovieLens Latest Small** —
<https://files.grouplens.org/datasets/movielens/ml-latest-small.zip>
(about 1 MB)

100,836 ratings from 610 users across 9,742 movies, and it has the same
`links.csv` with `tmdbId`. Worth using first to prove the import works
before pulling 250 MB. It is not the one to ship: GroupLens says it
"is changed and updated over time", so it is not reproducible, and 610
users is thin for the per-user analytics.

| | ml-latest-small | **ml-25m** | ml-20m |
|---|---|---|---|
| Ratings | 100,836 | **25,000,095** | 20,000,263 |
| Users | 610 | **162,541** | 138,493 |
| Movies | 9,742 | **62,423** | 27,278 |
| `links.csv` with `tmdbId` | yes | **yes** | yes |
| Half-star increments | yes | **yes** | yes |
| Stable / reproducible | no | **yes** | yes |
| Download | 1 MB | **250 MB** | 190 MB |

Datasets to avoid: **ml-100k** and **ml-1m** have no `links.csv`, so
there is no TMDB id to join on, and ml-100k uses whole stars, which our
half-star `CHECK` would accept but which would make the data look odd.
Kaggle re-uploads of "The Movies Dataset" contain the same GroupLens
files second-hand — cite the original instead.

---

## How it would fit our schema

Nothing in `01_schema.sql` has to change. The work is a new cleaning
step producing new `users.csv` and `ratings.csv`.

```
ml-25m/links.csv     movieId -> tmdbId        join key
ml-25m/ratings.csv   userId, movieId, rating, timestamp
                           |
                           v
 keep only rows whose tmdbId is one of our 4,803 films
 sample users down to the target volume
                           |
          +----------------+----------------+
          v                                 v
 data/cleaned/users.csv            data/cleaned/ratings.csv
 user_id   = MovieLens userId      user_id, movie_id = tmdbId,
 username  = "ml_<userId>"         rating_value = rating,
 display_name = "MovieLens user"   rated_at = FROM_UNIXTIME(timestamp)
 created_at = their first rating   updated_at = NULL
```

Points to get right:

- **`movie_id` must be the `tmdbId`, not the `movieId`.** Everything in
  both databases keys on the TMDB id.
- **Drop rows with an empty `tmdbId`** (a few hundred films in
  `links.csv` have none) and rows whose `tmdbId` is not in our 4,803.
- **Sample whole users, not random ratings.** Keeping a random 1% of
  ratings would give every user a handful and destroy the per-user
  analytics (favourite genre, the hybrid recommender). Pick N users and
  keep all of their ratings on our films.
- **Keep users with at least ~5 ratings on our films**, or the Q14
  favourite-genre query (`HAVING COUNT(*) >= 3`) returns nothing for
  most of them.
- **`username` must pass the constraints**: `UNIQUE`, and
  `CHAR_LENGTH(TRIM(username)) >= 3`. `ml_123` is fine.
- **Timestamps** are epoch seconds (UTC); convert to `DATETIME`.
  MovieLens activity runs 1995–2019, which fits `TIMESTAMP`.
- **`updated_at` stays NULL**: MovieLens records one rating per
  (user, film), with no edit history, and the `CHECK` requires
  `updated_at >= rated_at` when it is set.
- The composite primary key `(user_id, movie_id)` is already satisfied:
  MovieLens has one rating per user per film.

### Volume actually used

1,500 users and 246,192 real ratings, from the middle row below. The
sample is drawn in two strata with a fixed seed (37), so it is
reproducible:

* **1,000 users** drawn uniformly from the 142,023 with at least 20
  ratings on our films. Uniform, so the distribution stays realistic.
* **500 users** drawn from the 5,980 who also wrote at least 5 tags,
  because `user_tags` is where the MongoDB CRUD demo writes and a
  uniform sample would have supplied almost no tags: only 12,530 of
  162,532 MovieLens users ever tag anything. This stratum means the tag
  data is **not** a uniform sample of MovieLens taggers, which is
  stated rather than hidden.

Result: 4,169 of 4,803 films have at least one rating, 2,201 have at
least 20, and the median user has 78 ratings. The browse page renders
in about 350 ms against it.

### Volume options that were considered

| Target | Users | Ratings | Why |
|---|---|---|---|
| Demo-sized | ~2,000 | ~200,000 | CSV around 6 MB, loads in seconds, every analytics query returns something interesting |
| **Recommended** | ~5,000 | ~500,000 | CSV around 15 MB, still comfortably inside the submission limit, and enough that an index on `ratings.movie_id` visibly matters |
| Stress | ~20,000 | ~2,000,000 | good for the scaling experiment, but slow to load on a laptop and a large file to submit |

`benchmark/ratings_scaling.py` already generates synthetic volumes up
to 1M rows in its own scratch database, so the application data does
not have to be huge to show scaling behaviour.

### Keep the synthetic 12 users as well

Not as data, but as a point in the report: the synthetic users were
designed to exercise the constraints (one rating per user per film,
half-star steps, cascade on delete), while the MovieLens users supply
realistic distribution — a long tail of films with one or two ratings
and a few with thousands. Loading both means
`data/cleaned/users.csv` keeps ids 1–12 and the MovieLens ids start
above them, so the existing demo accounts in the UI still work.

---

## What it actually unlocked

1. **The weighted-score argument became ours.** `v_movie_stats`
   computes a Bayesian average because a raw average over few votes
   misleads. With only 784 ratings that view can only demonstrate it on
   TMDB's vote counts; with real ratings it demonstrates it on our own.
2. **The index experiments got honest.** An index on
   `ratings.movie_id` does nothing measurable over 784 rows. Over
   247,000 it is the difference between a scan and a lookup, in the
   application database rather than a scratch one.
3. **The recommenders started working properly.** The hybrid
   recommender builds a taste profile from films a user rated 4.0+.
   Real users have dozens to hundreds of those, instead of a handful.

4. **One thing it did not solve.** `user_activity_logs` is still
   synthetic, because no public dataset contains page views or
   searches. It is generated from each user's own real ratings and
   declared as synthetic in the README, the cleaning report and the
   code that writes it. Its timestamps are stored as offsets and made
   absolute at import time, so the 90-day TTL index never deletes it.

## Three things that had to change to make it fit

Worth a paragraph in the report, because each was a real decision:

1. **Four of our films have two MovieLens ids each.** A film entered
   twice on MovieLens and later merged still has both ids in
   `links.csv`, both pointing at one TMDB id. A user who rated both
   would produce two rows with the same `(user_id, movie_id)` and
   violate the composite primary key. Five such pairs occurred in the
   sample; the earlier rating is kept.
2. **The user table went from 12 rows to 1,512.** Three places in the
   UI had quietly assumed a tiny user table: the "Acting as" dropdown
   loaded every user on every page, the Users page listed all of them,
   and two reports had no `LIMIT`. All three are now paged or capped.
   This is the kind of thing that only shows up when the data grows.
3. **The MongoDB tag validator was rewritten by the data.** The
   original bound was 2–40 characters, chosen by eye. Real tags run
   from 1 to 230 characters, so the bound is now 1–100 — which keeps
   99.994% of them and matches what the web form already truncates to.
   The constraint is now derived from the data rather than guessed.
