"""
INF2003 Group Project - Team 37
database/mongodb/hybrid_recommendations.py : a recommender that uses
BOTH databases

generate_recommendations.py works from MongoDB alone: it looks at the
films a user VIEWED (user_activity_logs) and finds films sharing their
keywords. Every keyword counts the same, and a view is a weak signal -
you might have hated the film.

This version uses each database for what it is good at:

  MySQL      the ratings. Which films did this user actually LIKE, and
             how much? Ratings have integrity (a composite primary key,
             a CHECK on the range, foreign keys), so they are the
             trustworthy input. MySQL also supplies the user's
             favourite genres (the Q14 query) and the list of films
             they have already rated, which must be excluded.

  MongoDB    the content. The keywords, cast and crew that describe
             what a film IS live in the embedded arrays of
             movie_features, where one document per film means the
             scoring pipeline needs no joins.

Algorithm (recorded as "hybrid_sql_keyword" in the saved document):
  1. MySQL : films this user rated >= min_rating, with the score.
  2. Mongo : weight each keyword of those films by the rating it came
             from, giving a weighted taste profile (a 5.0 film's
             keywords count more than a 4.0 film's).
  3. Mongo : score every unseen film by how much profile weight its
             keywords match.
  4. MySQL : nudge films in the user's favourite genres upwards.
  5. Mongo : store the list in recommendations, with based_on filled in
             so the result can be explained afterwards.

Run the demo from the repo root:
    python database/mongodb/hybrid_recommendations.py --user 1
"""

import argparse
import os
from datetime import datetime, timezone

import mysql.connector
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

ALGORITHM = "hybrid_sql_keyword"
MIN_RATING = 4.0
GENRE_BOOST = 1.15
MIN_MATCHED_KEYWORDS = 2
# More liked films than this and the taste profile stops being a taste:
# only the highest-rated ones are used.
MAX_PROFILE_FILMS = 120


def mysql_connect():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "inf2003"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "moviedb"),
        autocommit=True,
        charset="utf8mb4",
    )


# =====================================================================
# Step 1: MySQL - what this user liked, and what to exclude
# =====================================================================
SQL_LIKED = """
    SELECT movie_id, rating_value
      FROM ratings
     WHERE user_id = %s AND rating_value >= %s
     ORDER BY rating_value DESC, COALESCE(updated_at, rated_at) DESC
     LIMIT %s
"""

SQL_ALREADY_RATED = "SELECT movie_id FROM ratings WHERE user_id = %s"

# The Q14 query from 06_queries.sql, for one user: the genres this user
# rates highest. HAVING COUNT(*) >= 2 keeps a single lucky rating from
# deciding a favourite genre.
SQL_FAVOURITE_GENRES = """
    SELECT g.name
      FROM ratings r
      JOIN movie_genres mg ON mg.movie_id = r.movie_id
      JOIN genres g        ON g.genre_id  = mg.genre_id
     WHERE r.user_id = %s
     GROUP BY g.name
    HAVING COUNT(*) >= 2
     ORDER BY AVG(r.rating_value) DESC, COUNT(*) DESC
     LIMIT 3
"""

SQL_FILMS_IN_GENRES = """
    SELECT DISTINCT mg.movie_id
      FROM movie_genres mg
      JOIN genres g ON g.genre_id = mg.genre_id
     WHERE g.name IN ({placeholders})
"""


def liked_films(cur, user_id, min_rating=MIN_RATING, limit=MAX_PROFILE_FILMS):
    cur.execute(SQL_LIKED, (user_id, min_rating, limit))
    return {int(movie_id): float(rating) for movie_id, rating in cur.fetchall()}


def already_rated(cur, user_id):
    cur.execute(SQL_ALREADY_RATED, (user_id,))
    return [int(row[0]) for row in cur.fetchall()]


def favourite_genres(cur, user_id):
    cur.execute(SQL_FAVOURITE_GENRES, (user_id,))
    return [row[0] for row in cur.fetchall()]


def films_in_genres(cur, genres):
    if not genres:
        return set()
    sql = SQL_FILMS_IN_GENRES.format(placeholders=", ".join(["%s"] * len(genres)))
    cur.execute(sql, tuple(genres))
    return {int(row[0]) for row in cur.fetchall()}


# =====================================================================
# Step 2: MongoDB - a rating-weighted keyword profile
# =====================================================================
def keyword_profile(db, liked):
    """Keyword -> weight, where the weight comes from MySQL ratings.

    The per-film weight is injected into the pipeline with $switch, the
    aggregation framework's CASE expression: one branch per liked film.
    The alternative would be fetching every document and summing in
    Python, which moves far more data over the wire.
    """
    if not liked:
        return {}

    branches = [{"case": {"$eq": ["$movie_id", movie_id]}, "then": rating}
                for movie_id, rating in liked.items()]

    pipeline = [
        {"$match": {"movie_id": {"$in": list(liked)}}},
        {"$addFields": {"film_weight": {"$switch": {"branches": branches,
                                                    "default": 1.0}}}},
        {"$unwind": "$keywords"},
        {"$group": {"_id": "$keywords.name",
                    "weight": {"$sum": "$film_weight"},
                    "films": {"$sum": 1}}},
        # A keyword from a single film is noise, unless the profile is
        # tiny; keep everything when the user has rated very little.
        {"$match": {"films": {"$gte": 2 if len(liked) >= 5 else 1}}},
        {"$sort": {"weight": -1}},
        {"$limit": 400},
    ]
    return {row["_id"]: float(row["weight"])
            for row in db.movie_features.aggregate(pipeline, allowDiskUse=True)}


# =====================================================================
# Step 3: MongoDB - score every unseen film against the profile
# =====================================================================
def score_candidates(db, profile, exclude_ids, limit=10):
    """Rank unseen films by matched keyword weight.

    The profile is passed as two parallel literal arrays. For each
    keyword of each candidate film, $indexOfArray finds its position in
    the names array and $arrayElemAt reads the matching weight. That
    keeps the whole calculation server-side with no per-keyword branch,
    so a 400-keyword profile costs the same as a 10-keyword one.
    """
    if not profile:
        return []

    names = list(profile.keys())
    weights = [profile[name] for name in names]

    pipeline = [
        {"$match": {"movie_id": {"$nin": list(exclude_ids)},
                    "keywords.name": {"$in": names}}},
        {"$unwind": "$keywords"},
        {"$match": {"keywords.name": {"$in": names}}},
        # $literal wraps the two arrays so the aggregation framework
        # treats them as data. Without it, any element that happened to
        # begin with "$" would be read as a field path, and keyword
        # names come from TMDB, not from us.
        {"$addFields": {"kw_weight": {"$let": {
            "vars": {"i": {"$indexOfArray": [{"$literal": names},
                                             "$keywords.name"]}},
            "in": {"$cond": [{"$gte": ["$$i", 0]},
                             {"$arrayElemAt": [{"$literal": weights}, "$$i"]},
                             0]},
        }}}},
        {"$group": {"_id": "$movie_id",
                    "matched_weight": {"$sum": "$kw_weight"},
                    "matched_keywords": {"$addToSet": "$keywords.name"}}},
        {"$addFields": {"matched_count": {"$size": "$matched_keywords"}}},
        {"$match": {"matched_count": {"$gte": MIN_MATCHED_KEYWORDS}}},
        {"$sort": {"matched_weight": -1, "matched_count": -1, "_id": 1}},
        {"$limit": limit * 3},        # room for the genre boost to reorder
        {"$project": {"_id": 0, "movie_id": "$_id", "matched_weight": 1,
                      "matched_keywords": 1, "matched_count": 1}},
    ]
    return list(db.movie_features.aggregate(pipeline, allowDiskUse=True))


# =====================================================================
# Steps 4 and 5: boost, normalise, store
# =====================================================================
def generate(db, cur, user_id, limit=10, min_rating=MIN_RATING):
    liked = liked_films(cur, user_id, min_rating)
    if not liked:
        return [], {"reason": f"no ratings of {min_rating} or above in MySQL"}

    profile = keyword_profile(db, liked)
    if not profile:
        return [], {"reason": "the liked films have no keywords in MongoDB"}

    exclude = already_rated(cur, user_id)
    candidates = score_candidates(db, profile, exclude, limit)
    if not candidates:
        return [], {"reason": "no unseen film matched enough keywords"}

    genres = favourite_genres(cur, user_id)
    boosted_ids = films_in_genres(cur, genres)

    for candidate in candidates:
        boost = GENRE_BOOST if candidate["movie_id"] in boosted_ids else 1.0
        candidate["boosted"] = boost > 1.0
        candidate["final_weight"] = candidate["matched_weight"] * boost

    candidates.sort(key=lambda c: (-c["final_weight"], -c["matched_count"],
                                   c["movie_id"]))
    candidates = candidates[:limit]

    # Normalise against the best match so the top film scores 1.0 and
    # the rest are relative to it. The validator in
    # create_collections.py requires a double between 0 and 1.
    best = max(c["final_weight"] for c in candidates) or 1.0

    recommendations = []
    for candidate in candidates:
        matched = sorted(candidate["matched_keywords"],
                         key=lambda name: -profile.get(name, 0))[:8]
        reason = (f"Matches {candidate['matched_count']} keywords from films you "
                  f"rated {min_rating}+")
        if candidate["boosted"]:
            reason += f"; also in your favourite genre ({', '.join(genres[:1])})"
        recommendations.append({
            "movie_id": int(candidate["movie_id"]),
            "score": round(float(candidate["final_weight"] / best), 4),
            "matched_keywords": matched,
            "reason": reason,
        })

    based_on = {
        "source": "MySQL ratings + MongoDB movie_features",
        "min_rating": float(min_rating),
        "liked_films": len(liked),
        "profile_keywords": len(profile),
        "excluded_films": len(exclude),
        "favourite_genres": genres,
        "genre_boost": GENRE_BOOST,
    }
    return recommendations, based_on


def save(db, user_id, recommendations, based_on):
    if not recommendations:
        return None
    result = db.recommendations.insert_one({
        "user_id": int(user_id),
        "generated_at": datetime.now(timezone.utc),
        "algorithm": ALGORITHM,
        "based_on": based_on,
        "recommendations": recommendations,
    })
    return result.inserted_id


def generate_and_save(db, user_id, limit=10, min_rating=MIN_RATING):
    """Convenience wrapper: opens its own MySQL connection."""
    conn = mysql_connect()
    try:
        cur = conn.cursor()
        try:
            recommendations, based_on = generate(db, cur, user_id, limit, min_rating)
        finally:
            cur.close()
    finally:
        conn.close()
    if recommendations:
        save(db, user_id, recommendations, based_on)
    return recommendations, based_on


# =====================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hybrid recommender demo")
    parser.add_argument("--user", type=int, default=1, help="user_id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-rating", type=float, default=MIN_RATING)
    parser.add_argument("--save", action="store_true",
                        help="store the list in the recommendations collection")
    args = parser.parse_args()

    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]
    conn = mysql_connect()
    cur = conn.cursor()

    cur.execute("SELECT username FROM users WHERE user_id = %s", (args.user,))
    row = cur.fetchone()
    print(f"User {args.user}" + (f" ({row[0]})" if row else " (not in MySQL!)"))

    recommendations, based_on = generate(db, cur, args.user, args.limit,
                                         args.min_rating)
    print(f"\nbased_on: {based_on}")

    if recommendations:
        ids = [r["movie_id"] for r in recommendations]
        placeholders = ", ".join(["%s"] * len(ids))
        cur.execute(f"SELECT movie_id, title FROM movies "
                    f"WHERE movie_id IN ({placeholders})", tuple(ids))
        titles = dict(cur.fetchall())

        print(f"\n{len(recommendations)} recommendations")
        for rank, rec in enumerate(recommendations, start=1):
            print(f"  {rank:>2}. {titles.get(rec['movie_id'], rec['movie_id'])}")
            print(f"      score {rec['score']:.3f} · "
                  f"{', '.join(rec['matched_keywords'][:5])}")

        if args.save:
            print(f"\nSaved as {save(db, args.user, recommendations, based_on)}")
        else:
            print("\n(not saved; pass --save to store it)")
    else:
        print("\nNo recommendations.")

    cur.close()
    conn.close()
    client.close()
