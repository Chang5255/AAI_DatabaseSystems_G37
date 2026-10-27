import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]


def get_viewed_movie_ids(user_id):
    """
    Return unique movie IDs that the user has viewed.
    """

    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "event_type": "view_movie",
                "movie_id": {"$exists": True}
            }
        },
        {
            "$group": {
                "_id": "$movie_id"
            }
        }
    ]

    results = db.user_activity_logs.aggregate(pipeline)

    return [result["_id"] for result in results]


def get_user_keywords(viewed_movie_ids):
    """
    Find all unique keywords belonging to movies
    previously viewed by the user.
    """

    pipeline = [
        {
            "$match": {
                "movie_id": {
                    "$in": viewed_movie_ids
                }
            }
        },
        {
            "$unwind": "$keywords"
        },
        {
            "$group": {
                "_id": None,
                "keywords": {
                    "$addToSet": "$keywords.name"
                }
            }
        }
    ]

    results = list(
        db.movie_features.aggregate(pipeline)
    )

    if not results:
        return []

    return results[0]["keywords"]


def generate_recommendations(user_id, limit=10):

    # Step 1: Determine which movies the user has viewed

    viewed_movie_ids = get_viewed_movie_ids(user_id)

    if not viewed_movie_ids:
        print(
            f"User {user_id} has no movie viewing history."
        )
        return []


    # Step 2: Get the user's preferred keywords

    user_keywords = get_user_keywords(
        viewed_movie_ids
    )

    if not user_keywords:
        print(
            f"No keywords found for user {user_id}."
        )
        return []


    print(
        f"User {user_id} has viewed "
        f"{len(viewed_movie_ids)} movie(s)."
    )

    print(
        f"Found {len(user_keywords)} unique keywords."
    )

    # Step 3: Find candidate movies with matching keywords

    pipeline = [

        # Don't recommend movies already viewed
        {
            "$match": {
                "movie_id": {
                    "$nin": viewed_movie_ids
                }
            }
        },

        # Expand keywords array
        {
            "$unwind": "$keywords"
        },

        # Keep only matching keywords
        {
            "$match": {
                "keywords.name": {
                    "$in": user_keywords
                }
            }
        },

        # Group matches back into one movie
        {
            "$group": {
                "_id": "$movie_id",

                "matched_keywords": {
                    "$addToSet": "$keywords.name"
                },

                "matched_keyword_count": {
                    "$sum": 1
                }
            }
        },

        # Calculate similarity score
        {
            "$addFields": {
                "score": {
                    "$divide": [
                        "$matched_keyword_count",
                        len(user_keywords)
                    ]
                }
            }
        },

        # Best matches first
        {
            "$sort": {
                "score": -1,
                "matched_keyword_count": -1
            }
        },

        {
            "$limit": limit
        },

        {
            "$project": {
                "_id": 0,
                "movie_id": "$_id",
                "score": 1,
                "matched_keywords": 1
            }
        }
    ]

    results = list(
        db.movie_features.aggregate(pipeline)
    )

    # Step 4: Format recommendation documents

    recommendations = []

    for result in results:

        recommendation = {
            "movie_id": result["movie_id"],
            "score": float(result["score"]),
            "matched_keywords": result[
                "matched_keywords"
            ],
            "reason": (
                "Shares keywords with movies "
                "previously viewed by the user"
            )
        }

        recommendations.append(
            recommendation
        )


    return recommendations


def save_recommendations(user_id, recommendations):

    if not recommendations:
        return None

    document = {
        "user_id": user_id,
        "generated_at": datetime.now(
            timezone.utc
        ),
        "algorithm": "keyword_similarity",
        "recommendations": recommendations
    }

    result = db.recommendations.insert_one(
        document
    )

    return result.inserted_id


if __name__ == "__main__":

    TEST_USER_ID = 1

    print("\n--- GENERATING RECOMMENDATIONS ---")

    recommendations = generate_recommendations(
        TEST_USER_ID,
        limit=10
    )


    print("\n--- RESULTS ---")

    for recommendation in recommendations:
        print(recommendation)


    if recommendations:

        recommendation_id = save_recommendations(
            TEST_USER_ID,
            recommendations
        )

        print(
            "\nSaved recommendation document:"
        )

        print(recommendation_id)


    client.close()