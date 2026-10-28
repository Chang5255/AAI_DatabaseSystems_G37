import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne


# Loads data/cleaned/posters.csv (written by fetch_posters.py) into
# movie_features.poster_path, so posters work without a TMDB key.
# Run after import_movie_features.py; setup_mongodb.py does this.

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

collection = db["movie_features"]


# Find repository root based on this script's location
ROOT_DIR = Path(__file__).resolve().parents[2]

DATA_FILE = ROOT_DIR / "data" / "cleaned" / "posters.csv"


if not DATA_FILE.exists():
    # Posters are optional: the UI shows placeholders without them
    print(f"No poster file at {DATA_FILE}; skipping posters.")
    client.close()
    raise SystemExit(0)


operations = []

with open(DATA_FILE, "r", encoding="utf-8", newline="") as file:
    for row in csv.DictReader(file):

        poster_path = row["poster_path"]

        operations.append(
            UpdateOne(
                {"movie_id": int(row["movie_id"])},
                {"$set": {"poster_path": None if poster_path == "\\N" else poster_path}}
            )
        )


if operations:
    result = collection.bulk_write(operations)

    print("Poster import completed.")
    print(f"Rows processed: {len(operations)}")
    print(f"Documents matched: {result.matched_count}")
    print(f"Documents modified: {result.modified_count}")

else:
    print("No rows found in poster file.")


print(
    f"Films with a poster in movie_features: "
    f"{collection.count_documents({'poster_path': {'$ne': None}})}"
)

client.close()
