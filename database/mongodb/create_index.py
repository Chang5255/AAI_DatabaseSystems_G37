import os

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]


# movie_features

db.movie_features.create_index(
    [("movie_id", ASCENDING)],
    unique=True,
    name="idx_movie_id_unique"
)

db.movie_features.create_index(
    [("keywords.name", ASCENDING)],
    name="idx_keywords_name"
)


# user_tags

db.user_tags.create_index(
    [("movie_id", ASCENDING)],
    name="idx_tags_movie_id"
)

db.user_tags.create_index(
    [("user_id", ASCENDING)],
    name="idx_tags_user_id"
)

db.user_tags.create_index(
    [
        ("user_id", ASCENDING),
        ("movie_id", ASCENDING)
    ],
    name="idx_tags_user_movie"
)


# user_activity_logs

db.user_activity_logs.create_index(
    [
        ("user_id", ASCENDING),
        ("timestamp", DESCENDING)
    ],
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


# recommendations
db.recommendations.create_index(
    [
        ("user_id", ASCENDING),
        ("generated_at", DESCENDING)
    ],
    name="idx_recommendations_user_generated"
)


print("MongoDB indexes created successfully.")

for collection_name in [
    "movie_features",
    "user_tags",
    "user_activity_logs",
    "recommendations"
]:
    print(f"\n{collection_name}")

    for index in db[collection_name].list_indexes():
        print(
            f"  {index['name']}: "
            f"{dict(index['key'])}"
        )


client.close()