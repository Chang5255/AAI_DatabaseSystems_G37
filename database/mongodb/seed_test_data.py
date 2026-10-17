import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

activity_logs = db["user_activity_logs"]


TEST_USER_ID = 1


# Remove previous test activity for this user
activity_logs.delete_many({
    "user_id": TEST_USER_ID
})


base_time = datetime.now(timezone.utc)


test_activities = [

    {
        "user_id": TEST_USER_ID,
        "event_type": "search",
        "timestamp": base_time,
        "metadata": {
            "query": "science fiction",
            "result_count": 20
        }
    },

    {
        "user_id": TEST_USER_ID,
        "event_type": "view_movie",
        "movie_id": 19995,
        "timestamp": base_time + timedelta(seconds=1),
        "metadata": {
            "source": "search_results"
        }
    },

    {
        "user_id": TEST_USER_ID,
        "event_type": "view_movie",
        "movie_id": 285,
        "timestamp": base_time + timedelta(seconds=2),
        "metadata": {
            "source": "recommendations"
        }
    },

    {
        "user_id": TEST_USER_ID,
        "event_type": "view_movie",
        "movie_id": 206647,
        "timestamp": base_time + timedelta(seconds=3),
        "metadata": {
            "source": "browse"
        }
    }
]


result = activity_logs.insert_many(
    test_activities
)

print(
    f"Inserted {len(result.inserted_ids)} "
    f"test activity records."
)

client.close()