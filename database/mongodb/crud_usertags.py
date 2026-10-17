import os
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

tags = db["user_tags"]


# CREATE

def create_tag(user_id, movie_id, tag):
    document = {
        "user_id": user_id,
        "movie_id": movie_id,
        "tag": tag,
        "created_at": datetime.now(timezone.utc)
    }

    result = tags.insert_one(document)

    return result.inserted_id


# READ

def get_tags_for_movie(movie_id):
    return list(
        tags.find(
            {"movie_id": movie_id}
        )
    )


def get_tags_by_user(user_id):
    return list(
        tags.find(
            {"user_id": user_id}
        )
    )


# UPDATE

def update_tag(tag_id, new_tag):
    result = tags.update_one(
        {"_id": ObjectId(tag_id)},
        {
            "$set": {
                "tag": new_tag
            }
        }
    )

    return result.modified_count


# DELETE

def delete_tag(tag_id):
    result = tags.delete_one(
        {"_id": ObjectId(tag_id)}
    )

    return result.deleted_count


# CRUD DEMO
if __name__ == "__main__":

    TEST_USER_ID = 1
    TEST_MOVIE_ID = 19995

    print("\n--- CREATE ---")

    tag_id = create_tag(
        TEST_USER_ID,
        TEST_MOVIE_ID,
        "great visuals"
    )

    print(f"Created tag with ID: {tag_id}")


    print("\n--- READ ---")

    movie_tags = get_tags_for_movie(TEST_MOVIE_ID)

    for tag in movie_tags:
        print(tag)


    print("\n--- UPDATE ---")

    modified = update_tag(
        str(tag_id),
        "amazing visuals"
    )

    print(f"Documents modified: {modified}")


    print("\n--- READ AFTER UPDATE ---")

    updated_tag = tags.find_one(
        {"_id": tag_id}
    )

    print(updated_tag)


    print("\n--- DELETE ---")

    deleted = delete_tag(str(tag_id))

    print(f"Documents deleted: {deleted}")


    print("\n--- FINAL CHECK ---")

    final_tag = tags.find_one(
        {"_id": tag_id}
    )

    print(final_tag)


    client.close()