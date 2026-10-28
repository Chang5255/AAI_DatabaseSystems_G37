"""
Fetch poster image paths from TMDB into movie_features.

The cleaned dataset has no images, but every movie_id is a real TMDB id,
so each film's poster path can be looked up and stored on its document:

    poster_path: "/abc123.jpg"   shown via https://image.tmdb.org/t/p/w342/abc123.jpg
    poster_path: null            TMDB has no poster (or no longer lists the film)

movie_features has no fixed schema for extra fields, so this adds a field
without any migration. Films that already have poster_path are skipped,
so the script can be stopped and re-run; use --refresh to fetch all again.

At the end the paths are also written to data/cleaned/posters.csv, which
is committed, so import_posters.py can load them without a TMDB key.

Needs TMDB_API_KEY in .env: the API key or the read access token from
https://www.themoviedb.org/settings/api

Run from the repo root:
    python database/mongodb/fetch_posters.py
"""

import csv
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

ROOT_DIR = Path(__file__).resolve().parents[2]
CSV_FILE = ROOT_DIR / "data" / "cleaned" / "posters.csv"

API_URL = "https://api.themoviedb.org/3/movie/{movie_id}"
WORKERS = 6        # parallel requests; TMDB allows roughly 40 per second
BATCH_SIZE = 200   # save to MongoDB every 200 films, so progress survives a stop
MAX_TRIES = 5


class KeyRejected(Exception):
    pass


def fetch_poster_path(movie_id):
    """Ask TMDB for one film and return its poster_path (or None)."""
    url = API_URL.format(movie_id=movie_id)
    headers = {"Accept": "application/json"}

    # A read access token is a JWT (starts with "eyJ"); an API key goes in the URL.
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        url += f"?api_key={TMDB_API_KEY}"

    request = urllib.request.Request(url, headers=headers)

    for attempt in range(MAX_TRIES):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response).get("poster_path")

        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise KeyRejected("TMDB rejected the key (401). Check TMDB_API_KEY in .env.")
            if err.code == 404:
                return None   # film is no longer on TMDB
            if err.code != 429 and err.code < 500:
                raise
            # Rate limited or a TMDB server error: wait, then try again
            wait = float(err.headers.get("Retry-After") or 2 ** attempt)

        except urllib.error.URLError:
            wait = 2 ** attempt

        time.sleep(wait)

    raise RuntimeError(f"gave up after {MAX_TRIES} tries")


def export_csv(collection):
    """Write every looked-up poster_path to posters.csv (\\N = no poster, as in the other CSVs)."""
    documents = collection.find(
        {"poster_path": {"$exists": True}},
        {"_id": 0, "movie_id": 1, "poster_path": 1}
    ).sort("movie_id", 1)

    rows = 0
    with open(CSV_FILE, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["movie_id", "poster_path"])
        for document in documents:
            writer.writerow([document["movie_id"], document["poster_path"] or "\\N"])
            rows += 1

    print(f"Wrote {rows} rows to {CSV_FILE.relative_to(ROOT_DIR)}")


def lookup(movie_id):
    """Returns (movie_id, poster_path, error) so one bad film does not stop the run."""
    try:
        return movie_id, fetch_poster_path(movie_id), None
    except KeyRejected:
        raise
    except Exception as err:
        return movie_id, None, err


if not TMDB_API_KEY:
    sys.exit(
        "TMDB_API_KEY is not set.\n"
        "Get a free key at https://www.themoviedb.org/settings/api "
        "and add it to .env (see .env.example)."
    )

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]

collection = db["movie_features"]


query = {} if "--refresh" in sys.argv else {"poster_path": {"$exists": False}}

movie_ids = [
    document["movie_id"]
    for document in collection.find(query, {"_id": 0, "movie_id": 1}).sort("movie_id", 1)
]

print(f"Films to look up on TMDB: {len(movie_ids)}")

if not movie_ids:
    print("Every film already has poster_path. Use --refresh to fetch them all again.")
    export_csv(collection)
    client.close()
    sys.exit()


# Look up the first film on its own, so a bad key fails straight away
try:
    first = lookup(movie_ids[0])
except KeyRejected as err:
    client.close()
    sys.exit(str(err))


operations = []
found = missing = failed = 0


def save():
    if operations:
        collection.bulk_write(operations, ordered=False)
        operations.clear()


pool = ThreadPoolExecutor(WORKERS)

try:
    results = itertools.chain([first], pool.map(lookup, movie_ids[1:]))

    for done, (movie_id, poster_path, error) in enumerate(results, start=1):

        if error is not None:
            # No poster_path is written, so a re-run tries this film again
            failed += 1
            print(f"  movie {movie_id}: {error}")
            continue

        operations.append(
            UpdateOne(
                {"movie_id": movie_id},
                {"$set": {"poster_path": poster_path}}
            )
        )

        if poster_path:
            found += 1
        else:
            missing += 1

        if len(operations) >= BATCH_SIZE:
            save()
            print(f"  {done} / {len(movie_ids)} looked up")

except (KeyboardInterrupt, KeyRejected) as err:
    pool.shutdown(wait=False, cancel_futures=True)
    save()
    print(f"\nStopped ({err or 'Ctrl+C'}). Progress is saved; run the script again to continue.")
    client.close()
    sys.exit(1)

pool.shutdown()
save()


print("\nPoster lookup completed.")
print(f"Posters found: {found}")
print(f"No poster on TMDB: {missing}")
print(f"Failed (re-run to retry): {failed}")

print(
    f"Films with a poster in movie_features: "
    f"{collection.count_documents({'poster_path': {'$ne': None}})}"
)

export_csv(collection)

client.close()
