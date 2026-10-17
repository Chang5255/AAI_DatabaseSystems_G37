import os

from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

# 1. MOST COMMON MOVIE KEYWORDS

def get_top_keywords(limit=10):

    pipeline = [
        {
            "$unwind": "$keywords"
        },
        {
            "$group": {
                "_id": "$keywords.name",
                "count": {
                    "$sum": 1
                }
            }
        },
        {
            "$sort": {
                "count": -1
            }
        },
        {
            "$limit": limit
        },
        {
            "$project": {
                "_id": 0,
                "keyword": "$_id",
                "count": 1
            }
        }
    ]

    return list(
        db.movie_features.aggregate(pipeline)
    )


# 2. MOST COMMON ACTIVITY TYPES

def get_activity_counts():

    pipeline = [
        {
            "$group": {
                "_id": "$event_type",
                "count": {
                    "$sum": 1
                }
            }
        },
        {
            "$sort": {
                "count": -1
            }
        },
        {
            "$project": {
                "_id": 0,
                "event_type": "$_id",
                "count": 1
            }
        }
    ]

    return list(
        db.user_activity_logs.aggregate(pipeline)
    )

# 3. MOST VIEWED MOVIES

def get_most_viewed_movies(limit=10):

    pipeline = [
        {
            "$match": {
                "event_type": "view_movie",
                "movie_id": {
                    "$exists": True
                }
            }
        },
        {
            "$group": {
                "_id": "$movie_id",
                "views": {
                    "$sum": 1
                }
            }
        },
        {
            "$sort": {
                "views": -1
            }
        },
        {
            "$limit": limit
        },
        {
            "$project": {
                "_id": 0,
                "movie_id": "$_id",
                "views": 1
            }
        }
    ]

    return list(
        db.user_activity_logs.aggregate(pipeline)
    )

# DEMONSTRATION

if __name__ == "__main__":

    print("\n--- TOP MOVIE KEYWORDS ---")

    for result in get_top_keywords(10):
        print(result)


    print("\n--- ACTIVITY COUNTS ---")

    for result in get_activity_counts():
        print(result)


    print("\n--- MOST VIEWED MOVIES ---")

    for result in get_most_viewed_movies(10):
        print(result)


    client.close()