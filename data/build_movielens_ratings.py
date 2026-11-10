"""
INF2003 Group Project - Team 37
data/build_movielens_ratings.py : replace the synthetic ratings with real ones

WHY THIS SCRIPT EXISTS
The catalogue (4,803 films, 104,842 people, 235,838 credits) is real
TMDB data. The ratings were not: the first cleaning pass generated 12
users and 784 ratings, because TMDB publishes only an aggregate
vote_average and never a per-user rating. 784 rows is too few to
demonstrate the analytics the project is built around - an index on
ratings.movie_id cannot pay for itself over 784 rows, and a "favourite
genre" computed from 65 ratings is noise.

This script brings in the real thing. MovieLens 25M is the standard
public source of per-user film ratings, and it ships links.csv, which
maps every MovieLens film id to a TMDB id - the same key our two
databases already share. So the real ratings can be attached to the
catalogue we already have, with no fuzzy title matching and no change
to the schema.

    F. M. Harper and J. A. Konstan, "The MovieLens Datasets: History
    and Context", ACM TiiS 5(4), 2015. https://doi.org/10.1145/2827872

WHAT IT PRODUCES
    data/cleaned/users.csv                MySQL  users
    data/cleaned/ratings.csv              MySQL  ratings
    data/cleaned/user_tags.jsonl          MongoDB user_tags
    data/cleaned/user_activity_logs.jsonl MongoDB user_activity_logs
    data/cleaned/movielens_report.txt     the audit trail for the report

WHAT IT KEEPS AND WHAT IT CHANGES
  * The 12 original synthetic users (ids 1-12) and their 784 ratings are
    kept exactly as they are. They are the accounts the web UI demos
    with, the MongoDB test scripts reference user 1, and they were
    designed to exercise the constraints. Real users are added
    alongside them, with ids from 1001 up, so "user_id >= 1000 means a
    real MovieLens user" is a rule anyone can check.
  * Nothing about the catalogue is touched. movies, genres, people,
    movie_cast, movie_crew, movie_genres and movie_features.jsonl are
    not read for content and never rewritten.
  * The schema does not change. Every value written here satisfies the
    constraints that were already in 01_schema.sql.

THE SAMPLE, AND WHY IT IS THE SHAPE IT IS
17.3 million of MovieLens's 25 million ratings fall on our 4,803 films.
Loading all of them would make every page of the application wait on a
17-million-row aggregate for no extra marks, so a sample is taken. It
is drawn in two strata, with a fixed seed so the result is reproducible:

  A  1,000 users drawn uniformly from everyone with at least 20 ratings
     on our films. Uniform, so the distribution stays realistic: mostly
     ordinary users, a few heavy ones. (20 is MovieLens's own inclusion
     threshold, and our Q14 "favourite genre" query needs at least 3
     ratings in a genre before it will report one.)

  B    500 users drawn from those who also wrote at least 5 tags on our
     films. The MongoDB user_tags collection is where the NoSQL CRUD
     demo writes, and a representative sample alone would have supplied
     almost no tags: only 12,530 of 162,532 users ever tag anything.
     This stratum is declared rather than hidden, because it means the
     tag data is NOT a uniform sample of MovieLens taggers.

Usage (from the repo root, with ml-25m unzipped somewhere):
    python data/build_movielens_ratings.py --ml-dir /path/to/ml-25m
    python data/build_movielens_ratings.py --ml-dir ... --raters 2000 --taggers 800
    python data/build_movielens_ratings.py --ml-dir ... --dry-run
"""

import argparse
import csv
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLEANED = REPO_ROOT / "data" / "cleaned"

# Defaults, chosen in the docstring above
RATERS = 1000
TAGGERS = 500
MIN_RATINGS = 20
MIN_TAGS = 5
SEED = 37

# Real users start here, so the 12 synthetic demo accounts keep ids 1-12
REAL_USER_ID_BASE = 1000

# user_tags: the validator in create_collections.py allows 1 to 100
# characters. 100 is not arbitrary - it is what webapp/routes.py already
# truncates a typed tag to, and it keeps 99.994% of the real tags.
TAG_MIN_LEN = 1
TAG_MAX_LEN = 100

# Synthetic activity stream (see build_activity below)
ACTIVITY_USERS = 800
ACTIVITY_DAYS = 30


def log(message):
    print(message, flush=True)


# =====================================================================
# Inputs
# =====================================================================
def read_our_films(report):
    """movie_id -> title, from the cleaned catalogue we already have."""
    path = CLEANED / "movies.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        films = {int(row["movie_id"]): row["title"]
                 for row in csv.DictReader(handle)}
    report.append(f"  our catalogue                      : {len(films):>10,} films")
    return films


def read_genres():
    path = CLEANED / "genres.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return [row["name"] for row in csv.DictReader(handle)]


def read_links(ml_dir, our_films, report):
    """MovieLens movieId -> TMDB movie_id, for films we hold.

    Two things have to be handled here or the load fails later:
      * 107 rows of links.csv have no tmdbId at all; they are skipped.
      * four of our films appear under TWO MovieLens ids each (a film
        entered twice on MovieLens and later merged). Both ids map to
        the same TMDB id, so a user who rated both would produce two
        rows with the same (user_id, movie_id) and violate the
        composite primary key. The pair is kept in the mapping and the
        duplicate is resolved per user, in read_ratings.
    """
    mapping, blank = {}, 0
    reverse = defaultdict(list)
    with (ml_dir / "links.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row["tmdbId"] or "").strip()
            if not raw:
                blank += 1
                continue
            tmdb_id = int(float(raw))
            if tmdb_id in our_films:
                mapping[int(row["movieId"])] = tmdb_id
                reverse[tmdb_id].append(int(row["movieId"]))

    collisions = {k: v for k, v in reverse.items() if len(v) > 1}
    report.append(f"  links.csv rows with no tmdbId      : {blank:>10,}  (skipped)")
    report.append(f"  MovieLens ids mapping to our films : {len(mapping):>10,}")
    report.append(f"  our films reachable from MovieLens : {len(reverse):>10,} "
                  f"of {len(our_films):,}")
    report.append(f"  our films with two MovieLens ids   : {len(collisions):>10,}  "
                  f"{sorted(collisions)}")
    return mapping


def count_ratings_per_user(ml_dir, mapping, report):
    """First pass: how many ratings does each user have on OUR films.

    ratings.csv is 25 million rows and 650 MB, so it is read as plain
    text and split by hand rather than through csv.DictReader. The file
    has no quoted fields, so this is safe and about ten times faster.
    """
    started = time.perf_counter()
    per_user = Counter()
    total = kept = 0
    with (ml_dir / "ratings.csv").open(encoding="utf-8") as handle:
        handle.readline()
        for line in handle:
            total += 1
            user_part, movie_part, _ = line.split(",", 2)
            if int(movie_part) in mapping:
                per_user[int(user_part)] += 1
                kept += 1

    counts = sorted(per_user.values())
    report.append(f"  MovieLens ratings, all films       : {total:>10,}")
    report.append(f"  ... on our films                   : {kept:>10,}  "
                  f"({kept / total:.1%})")
    report.append(f"  users rating our films             : {len(per_user):>10,}")
    report.append(f"  ratings per user on our films      : "
                  f"max {counts[-1]:,}, median {statistics.median(counts):.0f}")
    log(f"  pass 1 over {total:,} ratings in {time.perf_counter() - started:.0f}s")
    return per_user


def count_tags_per_user(ml_dir, mapping):
    tags_per_user = Counter()
    with (ml_dir / "tags.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["movieId"]) in mapping and (row["tag"] or "").strip():
                tags_per_user[int(row["userId"])] += 1
    return tags_per_user


# =====================================================================
# The sample
# =====================================================================
def choose_users(per_user, tags_per_user, args, report):
    rng = random.Random(args.seed)

    eligible = sorted(u for u, c in per_user.items() if c >= args.min_ratings)
    raters = set(rng.sample(eligible, min(args.raters, len(eligible))))

    tagger_pool = sorted(u for u in eligible
                         if tags_per_user.get(u, 0) >= args.min_tags
                         and u not in raters)
    taggers = set(rng.sample(tagger_pool, min(args.taggers, len(tagger_pool))))

    chosen = sorted(raters | taggers)
    report.append("")
    report.append(f"  stratum A, uniform (>= {args.min_ratings} ratings)       : "
                  f"{len(raters):>10,} of {len(eligible):,} eligible")
    report.append(f"  stratum B, taggers (>= {args.min_tags} tags)          : "
                  f"{len(taggers):>10,} of {len(tagger_pool):,} eligible")
    report.append(f"  users selected                     : {len(chosen):>10,}")
    report.append(f"  seed                               : {args.seed:>10}")

    # user_id is assigned in MovieLens id order, so re-running with the
    # same seed gives byte-identical output.
    return {movielens_id: REAL_USER_ID_BASE + 1 + index
            for index, movielens_id in enumerate(chosen)}


def read_ratings(ml_dir, mapping, user_ids, report):
    """Second pass: every selected user's ratings on our films.

    Returns {our_user_id: {movie_id: (rating, epoch_seconds)}}, which
    makes the (user_id, movie_id) primary key structurally impossible
    to violate: the dict cannot hold the same film twice for one user.
    When the two MovieLens ids of one film collide, the EARLIER rating
    is kept and the later one counted as a collision.
    """
    started = time.perf_counter()
    by_user = defaultdict(dict)
    collisions = off_scale = 0
    with (ml_dir / "ratings.csv").open(encoding="utf-8") as handle:
        handle.readline()
        for line in handle:
            user_part, movie_part, rating_part, stamp_part = line.rstrip("\n").split(",")
            movielens_user = int(user_part)
            our_user = user_ids.get(movielens_user)
            if our_user is None:
                continue
            movie_id = mapping.get(int(movie_part))
            if movie_id is None:
                continue

            rating = float(rating_part)
            # The CHECK constraints in 01_schema.sql: 0.5 to 5.0, in
            # half-star steps. MovieLens uses exactly that scale, so
            # this should never fire - it is here to prove it did not.
            if not (0.5 <= rating <= 5.0) or (rating * 2) % 1 != 0:
                off_scale += 1
                continue

            stamp = int(stamp_part)
            existing = by_user[our_user].get(movie_id)
            if existing is None:
                by_user[our_user][movie_id] = (rating, stamp)
            else:
                collisions += 1
                if stamp < existing[1]:
                    by_user[our_user][movie_id] = (rating, stamp)

    total = sum(len(films) for films in by_user.values())
    report.append("")
    report.append(f"  real ratings collected             : {total:>10,}")
    report.append(f"  duplicate (user, film) resolved    : {collisions:>10,}  "
                  f"(two MovieLens ids, one TMDB film; earliest kept)")
    report.append(f"  ratings off the 0.5-5.0 half-star  : {off_scale:>10,}")
    log(f"  pass 2 in {time.perf_counter() - started:.0f}s")
    return by_user


def read_tags(ml_dir, mapping, user_ids, report):
    """Every selected user's tags on our films, deduplicated.

    MongoDB enforces "one user cannot put the same tag on the same film
    twice" with a unique index on (user_id, movie_id, tag), so exact
    repeats have to go before the index can be built. Length is bounded
    to match the collection's validator.
    """
    rows = {}
    seen = exact_dupes = too_long = too_short = 0
    with (ml_dir / "tags.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            our_user = user_ids.get(int(row["userId"]))
            if our_user is None:
                continue
            movie_id = mapping.get(int(row["movieId"]))
            if movie_id is None:
                continue
            seen += 1

            tag = (row["tag"] or "").strip()
            if len(tag) < TAG_MIN_LEN:
                too_short += 1
                continue
            if len(tag) > TAG_MAX_LEN:
                too_long += 1
                continue

            key = (our_user, movie_id, tag)
            stamp = int(row["timestamp"])
            if key in rows:
                exact_dupes += 1
                rows[key] = min(rows[key], stamp)
            else:
                rows[key] = stamp

    report.append("")
    report.append(f"  tag applications by these users     : {seen:>10,}")
    report.append(f"  ... empty after trimming           : {too_short:>10,}  (dropped)")
    report.append(f"  ... longer than {TAG_MAX_LEN} characters     : {too_long:>10,}  (dropped)")
    report.append(f"  ... exact (user, film, tag) repeats: {exact_dupes:>10,}  "
                  f"(earliest kept)")
    report.append(f"  tags written                       : {len(rows):>10,}")
    return rows


# =====================================================================
# Existing synthetic data, kept as it is
# =====================================================================
def read_existing(report):
    with (CLEANED / "users.csv").open(encoding="utf-8", newline="") as handle:
        users = [row for row in csv.DictReader(handle)
                 if int(row["user_id"]) <= REAL_USER_ID_BASE]
    with (CLEANED / "ratings.csv").open(encoding="utf-8", newline="") as handle:
        ratings = [row for row in csv.DictReader(handle)
                   if int(row["user_id"]) <= REAL_USER_ID_BASE]
    report.append("")
    report.append(f"  synthetic users kept (ids 1-{REAL_USER_ID_BASE})   : {len(users):>10,}")
    report.append(f"  their ratings kept                 : {len(ratings):>10,}")
    return users, ratings


# =====================================================================
# The synthetic activity stream
# =====================================================================
def build_activity(by_user, existing_ratings, films, genres, args, report):
    """A recent browsing history, derived from the real ratings.

    This is the one part of the output that is NOT real data, and it
    cannot be: MovieLens publishes ratings and tags, never page views
    or searches. It is generated rather than left empty because three
    features read it - the "most viewed films" aggregation, the
    MongoDB-only recommender, and the activity feed on a profile - and
    all three are dead without it.

    It is derived from each user's own real ratings (the films they
    actually liked), so it is at least consistent with them, and it is
    declared as synthetic here, in the report and in the README.

    Timestamps are stored as seconds_ago, not as dates. The import
    script turns them into absolute times relative to the moment of
    import, which keeps the stream inside the 90-day window of the TTL
    index on user_activity_logs however long after this build the
    database is actually set up.
    """
    rng = random.Random(args.seed + 1)

    liked = {user_id: sorted((m for m, (rating, _) in films_rated.items()
                              if rating >= 4.0))
             for user_id, films_rated in by_user.items()}

    for row in existing_ratings:
        if float(row["rating_value"]) >= 4.0:
            liked.setdefault(int(row["user_id"]), []).append(int(row["movie_id"]))

    # Every synthetic demo account, plus a sample of the real ones.
    demo_users = sorted(u for u in liked if u <= REAL_USER_ID_BASE)
    real_users = sorted(u for u in liked if u > REAL_USER_ID_BASE and liked[u])
    chosen = demo_users + rng.sample(real_users,
                                     min(args.activity_users, len(real_users)))

    window = args.activity_days * 24 * 3600
    documents = []
    for user_id in sorted(chosen):
        pool = liked.get(user_id) or []
        if not pool:
            continue
        watched = rng.sample(pool, min(len(pool), rng.randint(3, 12)))

        # One or two searches that could plausibly have led there.
        for _ in range(rng.randint(1, 2)):
            if rng.random() < 0.5:
                query = rng.choice(genres)
            else:
                title = films.get(rng.choice(watched), "")
                words = [w for w in title.split() if len(w) > 3]
                query = rng.choice(words) if words else rng.choice(genres)
            documents.append({
                "user_id": user_id,
                "event_type": "search",
                "seconds_ago": rng.randrange(window),
                "metadata": {"query": query,
                             "result_count": rng.randint(1, 60),
                             "source": "imported_history"},
            })

        for movie_id in watched:
            seconds_ago = rng.randrange(window)
            documents.append({
                "user_id": user_id,
                "event_type": "view_movie",
                "movie_id": movie_id,
                "seconds_ago": seconds_ago,
                "metadata": {"source": "imported_history"},
            })
            # Some of those views ended in a rating. The score is the
            # real one, read back out of the ratings we just built.
            if rng.random() < 0.45:
                rated = by_user.get(user_id, {}).get(movie_id)
                value = rated[0] if rated else 4.0
                documents.append({
                    "user_id": user_id,
                    "event_type": "rate_movie",
                    "movie_id": movie_id,
                    "seconds_ago": max(0, seconds_ago - rng.randint(30, 900)),
                    "metadata": {"rating": float(value),
                                 "source": "imported_history"},
                })

    documents.sort(key=lambda d: (-d["seconds_ago"], d["user_id"]))
    kinds = Counter(d["event_type"] for d in documents)
    report.append("")
    report.append(f"  SYNTHETIC activity documents       : {len(documents):>10,}  "
                  f"for {len(set(d['user_id'] for d in documents)):,} users")
    report.append(f"  ... event types                    : "
                  f"{dict(sorted(kinds.items()))}")
    report.append(f"  ... spread over the last           : {args.activity_days:>10} days "
                  f"(relative to import, not to this build)")
    return documents


# =====================================================================
# Output
# =====================================================================
def write_csv(path, header, rows):
    """Write in exactly the dialect 02_load.sql expects.

    LOAD DATA is configured with LINES TERMINATED BY '\\n', so the
    lineterminator has to be forced: csv.writer defaults to '\\r\\n',
    which would leave a stray carriage return in the last column of
    every row.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path.stat().st_size


def write_jsonl(path, documents):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for document in documents:
            handle.write(json.dumps(document, ensure_ascii=False) + "\n")
    return path.stat().st_size


def iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


# =====================================================================
# Validation: the same checks 03_verify.sql makes, before loading
# =====================================================================
def validate(user_rows, rating_rows, tag_rows, films, report):
    checks = []

    user_ids = [int(r[0]) for r in user_rows]
    usernames = [r[1] for r in user_rows]
    checks.append(("users.user_id duplicated",
                   len(user_ids) - len(set(user_ids))))
    checks.append(("users.username duplicated",
                   len(usernames) - len(set(usernames))))
    checks.append(("users.username shorter than 3",
                   sum(1 for u in usernames if len(u.strip()) < 3)))

    pairs = [(int(r[0]), int(r[1])) for r in rating_rows]
    checks.append(("ratings duplicate primary key",
                   len(pairs) - len(set(pairs))))
    checks.append(("ratings orphan movie_id",
                   sum(1 for _, m in pairs if m not in films)))
    known_users = set(user_ids)
    checks.append(("ratings orphan user_id",
                   sum(1 for u, _ in pairs if u not in known_users)))

    values = [float(r[2]) for r in rating_rows]
    checks.append(("ratings outside 0.5-5.0",
                   sum(1 for v in values if not 0.5 <= v <= 5.0)))
    checks.append(("ratings not on a half-star step",
                   sum(1 for v in values if (v * 2) % 1 != 0)))
    checks.append(("ratings with updated_at before rated_at",
                   sum(1 for r in rating_rows
                       if r[4] != "\\N" and r[4] < r[3])))

    tag_keys = [(d["user_id"], d["movie_id"], d["tag"]) for d in tag_rows]
    checks.append(("user_tags duplicate (user, film, tag)",
                   len(tag_keys) - len(set(tag_keys))))
    checks.append(("user_tags orphan movie_id",
                   sum(1 for _, m, _ in tag_keys if m not in films)))
    checks.append(("user_tags orphan user_id",
                   sum(1 for u, _, _ in tag_keys if u not in known_users)))
    checks.append((f"user_tags outside {TAG_MIN_LEN}-{TAG_MAX_LEN} characters",
                   sum(1 for _, _, t in tag_keys
                       if not TAG_MIN_LEN <= len(t) <= TAG_MAX_LEN)))

    report.append("")
    report.append("VALIDATION  (every value must be 0)")
    report.append("-" * 68)
    failed = 0
    for name, count in checks:
        mark = "OK  " if count == 0 else "FAIL"
        report.append(f"  [{mark}] {name:<46} {count}")
        failed += 1 if count else 0
    report.append("-" * 68)
    report.append(f"  {len(checks) - failed}/{len(checks)} checks passed")
    return failed == 0


# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ml-dir", required=True,
                        help="the unzipped ml-25m folder")
    parser.add_argument("--raters", type=int, default=RATERS)
    parser.add_argument("--taggers", type=int, default=TAGGERS)
    parser.add_argument("--min-ratings", type=int, default=MIN_RATINGS)
    parser.add_argument("--min-tags", type=int, default=MIN_TAGS)
    parser.add_argument("--activity-users", type=int, default=ACTIVITY_USERS)
    parser.add_argument("--activity-days", type=int, default=ACTIVITY_DAYS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen, write nothing")
    args = parser.parse_args()

    ml_dir = Path(args.ml_dir).expanduser().resolve()
    for name in ("links.csv", "ratings.csv", "tags.csv"):
        if not (ml_dir / name).exists():
            sys.exit(f"{ml_dir / name} not found. Unzip ml-25m.zip first:\n"
                     f"  https://files.grouplens.org/datasets/movielens/ml-25m.zip")

    report = [
        "=" * 68,
        "INF2003 Team 37 - real ratings from MovieLens 25M",
        f"run at {datetime.now().isoformat(timespec='seconds')}",
        "=" * 68,
        "",
        "SOURCE",
        "  MovieLens 25M (GroupLens, University of Minnesota), joined to our",
        "  TMDB catalogue on links.csv tmdbId. Research use; cite",
        "  Harper and Konstan, ACM TiiS 5(4), 2015, doi 10.1145/2827872.",
        "",
        "INPUT",
    ]

    films = read_our_films(report)
    genres = read_genres()
    mapping = read_links(ml_dir, films, report)

    log("Counting ratings per user (pass 1 of 2) ...")
    per_user = count_ratings_per_user(ml_dir, mapping, report)
    log("Counting tags per user ...")
    tags_per_user = count_tags_per_user(ml_dir, mapping)

    report.append("")
    report.append("SAMPLE")
    user_ids = choose_users(per_user, tags_per_user, args, report)

    log("Collecting those users' ratings (pass 2 of 2) ...")
    by_user = read_ratings(ml_dir, mapping, user_ids, report)
    log("Collecting those users' tags ...")
    tags = read_tags(ml_dir, mapping, user_ids, report)

    existing_users, existing_ratings = read_existing(report)

    # ---- users.csv --------------------------------------------------
    first_rating = {our: min(stamp for _, stamp in films_rated.values())
                    for our, films_rated in by_user.items() if films_rated}
    user_rows = [[r["user_id"], r["username"],
                  r["display_name"] or "\\N", r["created_at"]]
                 for r in existing_users]
    for movielens_id, our_id in sorted(user_ids.items(), key=lambda kv: kv[1]):
        if our_id not in by_user or not by_user[our_id]:
            continue
        user_rows.append([
            our_id,
            f"ml_{movielens_id}",
            "\\N",                      # MovieLens publishes no names
            iso(first_rating[our_id]),  # joined when they first rated
        ])

    # ---- ratings.csv ------------------------------------------------
    rating_rows = [[r["user_id"], r["movie_id"], r["rating_value"],
                    r["rated_at"], r["updated_at"] or "\\N"]
                   for r in existing_ratings]
    for our_id in sorted(by_user):
        for movie_id, (value, stamp) in sorted(by_user[our_id].items()):
            rating_rows.append([our_id, movie_id, f"{value:.1f}",
                                iso(stamp), "\\N"])

    # ---- the MongoDB files ------------------------------------------
    tag_documents = [{"user_id": user_id, "movie_id": movie_id, "tag": tag,
                      "created_at": iso(stamp)}
                     for (user_id, movie_id, tag), stamp in sorted(tags.items())]
    activity = build_activity(by_user, existing_ratings, films, genres,
                              args, report)

    # ---- summary ----------------------------------------------------
    per_film = Counter(int(r[1]) for r in rating_rows)
    dense = sum(1 for c in per_film.values() if c >= 20)
    report.append("")
    report.append("OUTPUT")
    report.append(f"  users        {len(user_rows):>10,}  "
                  f"({len(existing_users)} synthetic + "
                  f"{len(user_rows) - len(existing_users):,} real)")
    report.append(f"  ratings      {len(rating_rows):>10,}  "
                  f"({len(existing_ratings)} synthetic + "
                  f"{len(rating_rows) - len(existing_ratings):,} real)")
    report.append(f"  user_tags    {len(tag_documents):>10,}  (real)")
    report.append(f"  activity     {len(activity):>10,}  (synthetic, see above)")
    report.append("")
    report.append(f"  films with at least one rating     : {len(per_film):>10,} "
                  f"of {len(films):,}")
    report.append(f"  films with at least 20 ratings     : {dense:>10,}  "
                  f"(03_verify.sql section C2 wants >= 1,000)")
    report.append(f"  ratings per film, median           : "
                  f"{statistics.median(per_film.values()):>10.0f}")
    report.append(f"  ratings per user, median           : "
                  f"{statistics.median(Counter(int(r[0]) for r in rating_rows).values()):>10.0f}")

    ok = validate(user_rows, rating_rows, tag_documents, films, report)

    if args.dry_run:
        report.append("")
        report.append("DRY RUN: nothing was written.")
    elif not ok:
        report.append("")
        report.append("VALIDATION FAILED: nothing was written.")
    else:
        sizes = [
            ("users.csv", write_csv(CLEANED / "users.csv",
                                    ["user_id", "username", "display_name",
                                     "created_at"], user_rows), len(user_rows)),
            ("ratings.csv", write_csv(CLEANED / "ratings.csv",
                                      ["user_id", "movie_id", "rating_value",
                                       "rated_at", "updated_at"], rating_rows),
             len(rating_rows)),
            ("user_tags.jsonl", write_jsonl(CLEANED / "user_tags.jsonl",
                                            tag_documents), len(tag_documents)),
            ("user_activity_logs.jsonl",
             write_jsonl(CLEANED / "user_activity_logs.jsonl", activity),
             len(activity)),
        ]
        report.append("")
        report.append("FILES WRITTEN")
        for name, size, rows in sizes:
            report.append(f"  {name:<26} {rows:>9,} rows   "
                          f"{size / 1024:>9,.0f} KB")
        report.append("")
        report.append("NEXT")
        report.append("  python setup_all.py          rebuild both databases")
        report.append("  (02_load.sql, 03_verify.sql and 05_crud.sql carry the")
        report.append("   expected row counts; they were updated with this change)")

    text = "\n".join(report) + "\n"
    print("\n" + text)
    if not args.dry_run:
        (CLEANED / "movielens_report.txt").write_text(text, encoding="utf-8")
        print(f"Report written to {CLEANED / 'movielens_report.txt'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
