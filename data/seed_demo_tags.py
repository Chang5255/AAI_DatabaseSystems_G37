"""
INF2003 Group Project - Team 37
data/seed_demo_tags.py : give the 12 demo profiles some tags

WHY THIS EXISTS
The 72,600 tags in user_tags are real, and they belong to the 500
MovieLens users who actually wrote them (user ids 1001 and up). The 12
demo accounts - cinephile_amy, scifi_sam and the rest - have none,
because nobody ever wrote any.

That is a problem only for the demonstration: those twelve are the
accounts the web UI is shown with, so the MongoDB "Tags" panel on a
profile, and the edit/delete half of the tag CRUD, both open empty on
exactly the accounts a marker will click. This script fills that in.

WHAT IT WRITES, AND HOW TO TELL
Every document it creates carries "seeded": true. Real MovieLens tags
do not have that field, so the two are never confused:

    db.user_tags.countDocuments({seeded: true})    the demo tags
    db.user_tags.countDocuments({seeded: {$exists: false}})   the real ones

The report should say the same thing: tags for users 1-12 are seeded
for the demo, everything else is real MovieLens data.

WHERE THE TAG TEXT COMES FROM
Two sources, mixed:
  * the film's OWN keywords, read from movie_features in MongoDB, so a
    tag on The Dark Knight might be "organized crime" rather than
    something invented
  * a short list of the kinds of things viewers actually write
    ("rewatchable", "great soundtrack", "slow burn")

Films are drawn from what each user actually rated 4.0 or better in
MySQL, so a demo user never tags a film they have not seen. That makes
this another small two-database operation: MySQL decides which films,
MongoDB supplies the words and stores the result.

IDEMPOTENT
Every run deletes its own previous output ("seeded": true) and writes
again from the same seed, so running it twice changes nothing. Real
tags are never touched. Tags added by hand through the web UI are not
touched either, because they have no "seeded" field.

Usage (from the repo root, both databases running):
    python data/seed_demo_tags.py
    python data/seed_demo_tags.py --per-user 12
    python data/seed_demo_tags.py --remove      # undo, leave nothing behind
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import BulkWriteError

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

# Demo accounts are the only ones below this id (see
# data/build_movielens_ratings.py).
DEMO_MAX_USER_ID = 1000
SEED = 37
MIN_TAGS, MAX_TAGS = 5, 12
MIN_RATING = 4.0

# What people actually type into a tag box.
GENERIC_TAGS = [
    "rewatchable", "great soundtrack", "underrated", "slow burn",
    "feel-good", "plot twist", "visually stunning", "comfort watch",
    "strong ending", "great cast", "atmospheric", "thought-provoking",
    "one more time", "holiday watch", "best in the series", "quotable",
]

# A couple of tags that suit each demo persona, so the seeded data at
# least agrees with the usernames the team chose.
PERSONA_TAGS = {
    "cinephile_amy": ["film club pick", "criterion-worthy"],
    "reelrahul": ["weekend watch", "big screen"],
    "popcornpete": ["popcorn film", "crowd pleaser"],
    "noir_nadia": ["noir", "shadows and rain"],
    "scifi_sam": ["hard sci-fi", "worldbuilding"],
    "docu_dan": ["based on real events", "educational"],
    "indie_iris": ["indie gem", "small budget"],
    "blockbuster_ben": ["blockbuster", "spectacle"],
    "kdrama_kelly": ["emotional", "great chemistry"],
    "classic_carl": ["classic", "stands the test of time"],
    "thriller_tina": ["edge of seat", "kept me guessing"],
    "anim_alex": ["animation", "beautiful art"],
}


def mysql_connect():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "inf2003"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "moviedb"),
        autocommit=True, charset="utf8mb4",
    )


SQL_DEMO_USERS = """
    SELECT user_id, username
      FROM users
     WHERE user_id <= %s
     ORDER BY user_id
"""

SQL_LIKED = """
    SELECT movie_id, rating_value, rated_at
      FROM ratings
     WHERE user_id = %s AND rating_value >= %s
     ORDER BY rating_value DESC, rated_at DESC
     LIMIT 40
"""


def keywords_for(db, movie_ids):
    """film -> its own keywords, from the MongoDB document."""
    rows = db.movie_features.find(
        {"movie_id": {"$in": list(movie_ids)}},
        {"_id": 0, "movie_id": 1, "keywords.name": 1},
    )
    out = {}
    for row in rows:
        names = [k["name"].strip() for k in row.get("keywords", [])
                 if k.get("name") and 2 <= len(k["name"].strip()) <= 40]
        out[row["movie_id"]] = names
    return out


def build(db, cur, per_user_cap, rng):
    cur.execute(SQL_DEMO_USERS, (DEMO_MAX_USER_ID,))
    users = cur.fetchall()
    if not users:
        return [], []

    documents, summary = [], []
    now = datetime.now(timezone.utc)

    for user_id, username in users:
        cur.execute(SQL_LIKED, (user_id, MIN_RATING))
        liked = cur.fetchall()
        if not liked:
            # Nothing rated 4.0+; fall back so no profile is left bare.
            cur.execute(SQL_LIKED, (user_id, 3.5))
            liked = cur.fetchall()
        if not liked:
            summary.append((username, 0, "no ratings to tag"))
            continue

        wanted = min(rng.randint(MIN_TAGS, MAX_TAGS), per_user_cap, len(liked) * 2)
        films = [row[0] for row in liked]
        rated_at = {row[0]: row[2] for row in liked}
        keywords = keywords_for(db, films)

        persona = PERSONA_TAGS.get(username, [])
        pool = list(GENERIC_TAGS) + persona * 3      # persona tags, weighted
        used = set()

        # A pool of the user's best-liked films, but never so narrow
        # that a user with few 4.0+ ratings ends up with two tags.
        pool_films = films[: max(10, len(films) // 2)]

        for _ in range(wanted * 12):                 # tries, not results
            if len(used) >= wanted:
                break
            movie_id = rng.choice(pool_films)

            own = keywords.get(movie_id) or []
            if own and rng.random() < 0.6:
                tag = rng.choice(own)
            else:
                tag = rng.choice(pool)

            key = (movie_id, tag)
            if key in used:
                continue
            used.add(key)

            # Tagged some time after watching, never in the future.
            when = rated_at.get(movie_id)
            if when is None:
                created = now - timedelta(days=rng.randint(1, 60))
            else:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                created = min(when + timedelta(hours=rng.randint(1, 240)), now)

            documents.append({
                "user_id": int(user_id),
                "movie_id": int(movie_id),
                "tag": tag,
                "created_at": created,
                "seeded": True,
            })

        summary.append((username, len(used), ""))

    return documents, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--per-user", type=int, default=MAX_TAGS,
                        help=f"most tags per demo user (default {MAX_TAGS})")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--remove", action="store_true",
                        help="delete the seeded tags and write nothing")
    args = parser.parse_args()

    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]

    existing = db.user_tags.count_documents({"seeded": True})
    real = db.user_tags.count_documents({"seeded": {"$exists": False}})
    print(f"user_tags before: {real:,} real, {existing:,} seeded")

    removed = db.user_tags.delete_many({"seeded": True}).deleted_count
    if removed:
        print(f"removed {removed:,} previously seeded tag(s)")
    if args.remove:
        print("--remove given: nothing written.")
        client.close()
        return 0

    try:
        conn = mysql_connect()
    except mysql.connector.Error as exc:
        print(f"Could not connect to MySQL: {exc}")
        client.close()
        return 1

    cur = conn.cursor()
    try:
        documents, summary = build(db, cur, args.per_user,
                                   random.Random(args.seed))
    finally:
        cur.close()
        conn.close()

    if not documents:
        print("No demo users with ratings were found; nothing to do.")
        client.close()
        return 1

    try:
        inserted = len(db.user_tags.insert_many(documents, ordered=False)
                       .inserted_ids)
    except BulkWriteError as exc:
        # The unique index refused something: report it rather than hide
        # it, since that index is part of what the project demonstrates.
        errors = exc.details.get("writeErrors", [])
        inserted = exc.details.get("nInserted", 0)
        print(f"{len(errors)} document(s) refused; first: "
              f"{errors[0].get('errmsg', '')[:150]}")

    print(f"\nSeeded {inserted} tag(s) across {len(summary)} demo profile(s)")
    for username, count, note in summary:
        print(f"  {username:<18} {count:>3} tags  {note}")

    print(f"\nuser_tags now: "
          f"{db.user_tags.count_documents({'seeded': {'$exists': False}}):,} real, "
          f"{db.user_tags.count_documents({'seeded': True}):,} seeded")
    print("\nThe seeded ones are the demo profiles' (user_id <= 1000) and carry\n"
          '"seeded": true. Everything else is real MovieLens 25M data.')
    print("Re-run this after any rebuild of MongoDB (setup_mongodb.py drops nothing,\n"
          "but a fresh database starts without these).")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
