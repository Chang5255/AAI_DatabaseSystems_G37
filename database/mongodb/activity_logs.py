import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

activity_logs = db["user_activity_logs"]


# CREATE ACTIVITY LOG

def log_activity(
    user_id,
    event_type,
    movie_id=None,
    metadata=None
):
    document = {
        "user_id": user_id,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc),
        "metadata": metadata or {}
    }

    # Only include movie_id if the event relates to a movie
    if movie_id is not None:
        document["movie_id"] = movie_id

    result = activity_logs.insert_one(document)

    return result.inserted_id


# READ USER ACTIVITY
def get_user_activity(user_id, limit=20):
    return list(
        activity_logs.find(
            {"user_id": user_id}
        )
        .sort("timestamp", DESCENDING)
        .limit(limit)
    )


# READ ACTIVITY BY EVENT TYPE

def get_activity_by_type(event_type):
    return list(
        activity_logs.find(
            {"event_type": event_type}
        )
        .sort("timestamp", DESCENDING)
    )


# DEMO
if __name__ == "__main__":

    TEST_USER_ID = 1
    TEST_MOVIE_ID = 19995

    print("\n--- LOG SEARCH EVENT ---")

    search_id = log_activity(
        user_id=TEST_USER_ID,
        event_type="search",
        metadata={
            "query": "science fiction",
            "result_count": 24
        }
    )

    print(f"Created search activity: {search_id}")


    print("\n--- LOG MOVIE VIEW EVENT ---")

    view_id = log_activity(
        user_id=TEST_USER_ID,
        event_type="view_movie",
        movie_id=TEST_MOVIE_ID,
        metadata={
            "source": "search_results"
        }
    )

    print(f"Created movie view activity: {view_id}")


    print("\n--- USER ACTIVITY HISTORY ---")

    activities = get_user_activity(TEST_USER_ID)

    for activity in activities:
        print(activity)


    client.close()