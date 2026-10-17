import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

collection = db["movie_features"]


# Find repository root based on this script's location
ROOT_DIR = Path(__file__).resolve().parents[2]

DATA_FILE = ROOT_DIR / "data" / "cleaned" / "movie_features.jsonl"


if not DATA_FILE.exists():
    raise FileNotFoundError(
        f"Could not find dataset:\n{DATA_FILE}"
    )


operations = []
processed = 0

with open(DATA_FILE, "r", encoding="utf-8") as file:
    for line in file:

        line = line.strip()

        if not line:
            continue

        document = json.loads(line)

        movie_id = document["movie_id"]

        operations.append(
            UpdateOne(
                {"movie_id": movie_id},
                {"$set": document},
                upsert=True
            )
        )

        processed += 1


if operations:
    result = collection.bulk_write(operations)

    print("Movie feature import completed.")
    print(f"Documents processed: {processed}")
    print(f"Documents inserted: {result.upserted_count}")
    print(f"Documents modified: {result.modified_count}")

else:
    print("No documents found in dataset.")


print(
    f"Total documents in movie_features: "
    f"{collection.count_documents({})}"
)

client.close()