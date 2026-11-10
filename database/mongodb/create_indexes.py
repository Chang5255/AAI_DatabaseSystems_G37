"""
INF2003 Group Project - Team 37
database/mongodb/create_indexes.py : every index the application needs

MongoDB indexes are B-trees, like MySQL's (lecture 6), with three kinds
this project uses:

  single field / compound   the same idea as MySQL
  MULTIKEY                  an index on a field inside an array. One
                            document produces one index entry per array
                            element, so {"cast.person_id": 500} is an
                            index lookup instead of a collection scan.
                            This has no MySQL equivalent - in MySQL the
                            array is a separate table with its own
                            index (movie_cast.idx_cast_person).
  TEXT                      an inverted word index, the counterpart of
                            MySQL's FULLTEXT index on movies.title.

Three of them also act as constraints or lifecycle rules:

  idx_tags_unique_user_movie_tag   UNIQUE: one user cannot put the same
                                   tag on the same film twice. This is
                                   the only way to get a uniqueness
                                   guarantee in MongoDB - there is no
                                   UNIQUE in the $jsonSchema validator.
  idx_activity_ttl                 TTL: activity documents are deleted
                                   automatically after ACTIVITY_TTL_DAYS.
                                   MySQL has no equivalent; it needs a
                                   scheduled DELETE job.
  idx_movie_id_unique              UNIQUE: movie_features holds exactly
                                   one document per film, which is what
                                   makes movie_id a safe join key to
                                   MySQL.

Re-runnable. create_index is a no-op when an identical index already
exists; the unique index is created only after any existing duplicates
are reported and removed, because MongoDB refuses to build a unique
index over data that already violates it.
"""

import os
from collections import Counter

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, TEXT, MongoClient
from pymongo.errors import OperationFailure

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

# Activity logs older than this are removed by the TTL monitor, which
# MongoDB runs every 60 seconds. 90 days keeps a term's worth of demo
# data; a real service would tune this to its retention policy.
ACTIVITY_TTL_DAYS = 90

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]


# ---------------------------------------------------------------------
# movie_features
# ---------------------------------------------------------------------
def index_movie_features():
    # One document per film: the join key to MySQL movies.movie_id.
    db.movie_features.create_index(
        [("movie_id", ASCENDING)],
        unique=True,
        name="idx_movie_id_unique"
    )

    # Keyword search on the Discover page (multikey, on keywords[].name).
    db.movie_features.create_index(
        [("keywords.name", ASCENDING)],
        name="idx_keywords_name"
    )

    # MULTIKEY on the embedded cast array. Without this, "every film
    # this actor is in" reads all 4,803 documents (COLLSCAN); with it,
    # MongoDB reads only the matching ones. benchmark/mongo_bench.py
    # task T3 and benchmark/mongo_index_bench.py measure the difference.
    db.movie_features.create_index(
        [("cast.person_id", ASCENDING)],
        name="idx_cast_person_id"
    )

    # Same for crew, used by the rename benchmark (T4) and the film page.
    db.movie_features.create_index(
        [("crew.person_id", ASCENDING)],
        name="idx_crew_person_id"
    )

    # TEXT index for the overview search on the Discover page. MongoDB
    # allows only ONE text index per collection, so this covers
    # overview only. The web app also calls create_index for this one
    # at start-up (webapp/db.py ensure_mongo_indexes) with the same
    # name and definition, which makes that call a no-op.
    db.movie_features.create_index(
        [("overview", TEXT)],
        name="idx_overview_text"
    )


# ---------------------------------------------------------------------
# user_tags
# ---------------------------------------------------------------------
def find_duplicate_tags():
    """Groups of (user_id, movie_id, tag) that appear more than once."""
    return list(db.user_tags.aggregate([
        {"$group": {
            "_id": {"user_id": "$user_id", "movie_id": "$movie_id", "tag": "$tag"},
            "ids": {"$push": "$_id"},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]))


def index_user_tags(remove_duplicates=True):
    db.user_tags.create_index([("movie_id", ASCENDING)], name="idx_tags_movie_id")
    db.user_tags.create_index([("user_id", ASCENDING)], name="idx_tags_user_id")
    db.user_tags.create_index(
        [("user_id", ASCENDING), ("movie_id", ASCENDING)],
        name="idx_tags_user_movie"
    )

    duplicates = find_duplicate_tags()
    if duplicates:
        print(f"  {len(duplicates)} duplicate (user, film, tag) group(s) found:")
        for group in duplicates[:5]:
            key = group["_id"]
            print(f"    user {key['user_id']} film {key['movie_id']} "
                  f"tag '{key['tag']}' x{group['count']}")
        if not remove_duplicates:
            print("  skipping the unique index (re-run without --keep-duplicates)")
            return
        removed = 0
        for group in duplicates:
            # Keep the oldest document of each group, drop the rest.
            for extra_id in sorted(group["ids"])[1:]:
                db.user_tags.delete_one({"_id": extra_id})
                removed += 1
        print(f"  removed {removed} duplicate tag document(s)")

    # UNIQUE: the constraint a $jsonSchema validator cannot express.
    db.user_tags.create_index(
        [("user_id", ASCENDING), ("movie_id", ASCENDING), ("tag", ASCENDING)],
        unique=True,
        name="idx_tags_unique_user_movie_tag"
    )


# ---------------------------------------------------------------------
# user_activity_logs
# ---------------------------------------------------------------------
def index_activity_logs():
    # The profile page reads one user's newest events: compound index,
    # descending on time so the sort is served by the index.
    db.user_activity_logs.create_index(
        [("user_id", ASCENDING), ("timestamp", DESCENDING)],
        name="idx_activity_user_timestamp"
    )
    db.user_activity_logs.create_index(
        [("event_type", ASCENDING)],
        name="idx_activity_event_type"
    )
    db.user_activity_logs.create_index(
        [("movie_id", ASCENDING)],
        name="idx_activity_movie_id"
    )

    # TTL: MongoDB deletes documents once timestamp is older than
    # expireAfterSeconds. If the index already exists with a different
    # TTL, collMod is the only way to change it.
    ttl_seconds = ACTIVITY_TTL_DAYS * 24 * 60 * 60
    try:
        db.user_activity_logs.create_index(
            [("timestamp", ASCENDING)],
            expireAfterSeconds=ttl_seconds,
            name="idx_activity_ttl"
        )
    except OperationFailure:
        db.command("collMod", "user_activity_logs", index={
            "name": "idx_activity_ttl",
            "expireAfterSeconds": ttl_seconds,
        })
        print(f"  idx_activity_ttl updated to {ACTIVITY_TTL_DAYS} days")


# ---------------------------------------------------------------------
# recommendations and people_graph
# ---------------------------------------------------------------------
def index_recommendations():
    # "the newest list for this user" - find_one with a sort.
    db.recommendations.create_index(
        [("user_id", ASCENDING), ("generated_at", DESCENDING)],
        name="idx_recommendations_user_generated"
    )
    # Comparing algorithms over time in the reports.
    db.recommendations.create_index(
        [("algorithm", ASCENDING), ("generated_at", DESCENDING)],
        name="idx_recommendations_algorithm"
    )


def index_people_graph():
    """Indexes for the $graphLookup traversal in advanced_queries.py.

    connectToField is person_id, so that field must be indexed or every
    hop of the traversal becomes a collection scan.
    """
    if "people_graph" not in db.list_collection_names():
        print("  people_graph does not exist yet "
              "(run build_costar_graph.py); skipping")
        return
    db.people_graph.create_index(
        [("person_id", ASCENDING)], unique=True, name="idx_graph_person_id"
    )
    db.people_graph.create_index([("name", ASCENDING)], name="idx_graph_name")
    db.people_graph.create_index(
        [("film_count", DESCENDING)], name="idx_graph_film_count"
    )


# ---------------------------------------------------------------------
def report():
    print("\nIndexes now in place")
    for collection_name in ("movie_features", "user_tags", "user_activity_logs",
                            "recommendations", "people_graph"):
        if collection_name not in db.list_collection_names():
            continue
        print(f"\n{collection_name}")
        for index in db[collection_name].list_indexes():
            flags = []
            if index.get("unique"):
                flags.append("UNIQUE")
            if "expireAfterSeconds" in index:
                flags.append(f"TTL {index['expireAfterSeconds'] // 86400}d")
            keys = dict(index["key"])
            kinds = Counter(type(v).__name__ for v in keys.values())
            if "str" in kinds:
                flags.append("TEXT")
            suffix = ("  [" + ", ".join(flags) + "]") if flags else ""
            print(f"  {index['name']}: {keys}{suffix}")

    stats = db.command("collStats", "movie_features")
    print(f"\nmovie_features: {stats['count']} documents, "
          f"data {stats['size'] / 1e6:.1f} MB, "
          f"indexes {stats['totalIndexSize'] / 1e6:.1f} MB")


if __name__ == "__main__":
    print("movie_features")
    index_movie_features()
    print("user_tags")
    index_user_tags()
    print("user_activity_logs")
    index_activity_logs()
    print("recommendations")
    index_recommendations()
    print("people_graph")
    index_people_graph()

    report()
    print("\nMongoDB indexes created successfully.")
    client.close()
