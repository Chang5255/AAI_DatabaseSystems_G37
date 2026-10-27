import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

recommendations_collection = db["recommendations"]

# SAVE RECOMMENDATIONS

def save_recommendations(user_id, algorithm, recommendations):
    document = {
        "user_id": user_id,
        "generated_at": datetime.now(timezone.utc),
        "algorithm": algorithm,
        "recommendations": recommendations
    }

    result = recommendations_collection.insert_one(document)

    return result.inserted_id


# GET LATEST RECOMMENDATIONS FOR USER

def get_latest_recommendations(user_id):
    return recommendations_collection.find_one(
        {"user_id": user_id},
        sort=[("generated_at", DESCENDING)]
    )


# GET RECOMMENDATION HISTORY

def get_recommendation_history(user_id, limit=10):
    return list(
        recommendations_collection.find(
            {"user_id": user_id}
        )
        .sort("generated_at", DESCENDING)
        .limit(limit)
    )


# DEMO

if __name__ == "__main__":

    TEST_USER_ID = 1

    sample_recommendations = [
        {
            "movie_id": 285,
            "score": 0.87,
            "matched_keywords": [
                "adventure",
                "ocean"
            ],
            "reason": "Shares keywords with movies viewed by the user"
        },
        {
            "movie_id": 206647,
            "score": 0.74,
            "matched_keywords": [
                "action"
            ],
            "reason": "Similar content to movies viewed by the user"
        }
    ]

    print("\n--- SAVE RECOMMENDATIONS ---")

    recommendation_id = save_recommendations(
        user_id=TEST_USER_ID,
        algorithm="keyword_similarity",
        recommendations=sample_recommendations
    )

    print(
        f"Created recommendation document: "
        f"{recommendation_id}"
    )


    print("\n--- LATEST RECOMMENDATIONS ---")

    latest = get_latest_recommendations(TEST_USER_ID)

    print(latest)


    print("\n--- RECOMMENDATION HISTORY ---")

    history = get_recommendation_history(TEST_USER_ID)

    for recommendation in history:
        print(recommendation)


    client.close()