"""
INF2003 Group Project - Team 37
database/mongodb/import_user_data.py : load user_tags and activity logs

Two collections, two very different kinds of data, and the difference
matters enough to keep them in one script where it can be stated:

  user_tags            REAL. 72,600 tag applications written by the
                       1,500 MovieLens users who are also in MySQL's
                       users table. MovieLens publishes tags.csv, so
                       these are genuine free-text labels real people
                       put on real films. created_at is their real
                       timestamp (2005-2019).

  user_activity_logs   SYNTHETIC, and declared as such everywhere it
                       appears. No public dataset contains page views
                       or searches, so this stream is generated from
                       each user's own real ratings: films they rated
                       4.0 or better become views, some of those views
                       become ratings, and a search precedes them.

TIMESTAMPS ARE RELATIVE, ON PURPOSE
The activity file stores "seconds_ago", not a date, and this script
turns each one into an absolute time counted back from the moment of
import. That is not a shortcut - user_activity_logs carries a TTL index
with a 90-day window (create_indexes.py), so a file of fixed historical
dates would be deleted by MongoDB's TTL monitor within a minute of
being loaded. Relative offsets mean the stream is always inside the
window, no matter how long after the data was built the database is
built.

IDEMPOTENT
  * tags are upserted on (user_id, movie_id, tag), the same key as the
    unique index, so a second run changes nothing
  * activity documents carry metadata.source = "imported_history", and
    every document with that marker is removed before the insert, so a
    second run replaces the stream instead of doubling it. Events
    created by using the web application have a different source and
    are never touched.

Usage (from the repo root, MongoDB running):
    python database/mongodb/import_user_data.py
    python database/mongodb/import_user_data.py --skip-activity
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, OperationFailure

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

ROOT_DIR = Path(__file__).resolve().parents[2]
CLEANED = ROOT_DIR / "data" / "cleaned"
TAGS_FILE = CLEANED / "user_tags.jsonl"
ACTIVITY_FILE = CLEANED / "user_activity_logs.jsonl"

IMPORT_MARKER = "imported_history"
BATCH = 2000


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def parse_timestamp(text):
    """'2018-11-15 19:19:55' -> an aware datetime, stored as a BSON date.

    The validator requires bsonType "date", and pymongo maps a Python
    datetime to that. The build script wrote these in UTC.
    """
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)


# =====================================================================
# user_tags: real data, upserted
# =====================================================================
def ensure_tag_index(db):
    """Build the (user_id, movie_id, tag) index BEFORE the upserts.

    This is not a tidiness nicety, it is the difference between seconds
    and hours. Every upsert below filters on exactly these three
    fields. Without an index MongoDB answers each filter with a
    collection scan, so importing 72,600 tags means 72,600 scans over a
    collection that is itself growing to 72,600 documents - billions of
    document reads.

    create_indexes.py builds the same index (same name, same keys, same
    options), which is the right place for it in general: indexing
    after a bulk load is normally faster than maintaining an index
    during one. That rule does not apply when the load itself has to
    look rows up, as an upsert does. Creating it here first is
    idempotent - create_indexes.py will find it already present.
    """
    keys = [("user_id", ASCENDING), ("movie_id", ASCENDING), ("tag", ASCENDING)]
    try:
        db.user_tags.create_index(keys, unique=True,
                                  name="idx_tags_unique_user_movie_tag")
    except OperationFailure as exc:
        # Duplicates already in the collection: fall back to a
        # non-unique index, which still makes the upserts fast, and
        # leave the clean-up to create_indexes.py.
        print(f"  unique index refused ({exc.code}); using a non-unique index "
              f"for the import. Run create_indexes.py afterwards to "
              f"deduplicate and make it unique.")
        db.user_tags.create_index(keys, name="idx_tags_user_movie_tag_import")


def import_tags(db):
    if not TAGS_FILE.exists():
        print(f"  {TAGS_FILE.name} not found; skipping tags")
        return

    ensure_tag_index(db)

    operations, total = [], 0
    inserted = updated = 0

    def flush():
        nonlocal operations, inserted, updated
        if not operations:
            return
        try:
            result = db.user_tags.bulk_write(operations, ordered=False)
            inserted += result.upserted_count
            updated += result.modified_count
        except BulkWriteError as exc:
            # Surfacing this rather than swallowing it: a write error
            # here means a document failed the collection's validator
            # or the unique index, and that is worth knowing about.
            errors = exc.details.get("writeErrors", [])
            print(f"  {len(errors)} document(s) refused by MongoDB; first: "
                  f"{errors[0].get('errmsg', '')[:160]}")
            inserted += exc.details.get("nUpserted", 0)
            updated += exc.details.get("nModified", 0)
        operations = []

    for document in read_jsonl(TAGS_FILE):
        total += 1
        operations.append(UpdateOne(
            {"user_id": document["user_id"],
             "movie_id": document["movie_id"],
             "tag": document["tag"]},
            {"$set": {"user_id": document["user_id"],
                      "movie_id": document["movie_id"],
                      "tag": document["tag"],
                      "created_at": parse_timestamp(document["created_at"])}},
            upsert=True,
        ))
        if len(operations) >= BATCH:
            flush()
            if total % 20000 < BATCH:
                print(f"    {total:,} tags processed ...", flush=True)
    flush()

    print(f"  user_tags: {total:,} in the file, {inserted:,} inserted, "
          f"{updated:,} updated")
    print(f"  user_tags now holds {db.user_tags.count_documents({}):,} documents")


# =====================================================================
# user_activity_logs: synthetic, replaced
# =====================================================================
def import_activity(db):
    if not ACTIVITY_FILE.exists():
        print(f"  {ACTIVITY_FILE.name} not found; skipping activity")
        return

    removed = db.user_activity_logs.delete_many(
        {"metadata.source": IMPORT_MARKER}).deleted_count
    if removed:
        print(f"  removed {removed:,} document(s) from a previous import")

    now = datetime.now(timezone.utc)
    documents, kinds = [], Counter()
    inserted = 0

    def flush():
        nonlocal documents, inserted
        if not documents:
            return
        try:
            result = db.user_activity_logs.insert_many(documents, ordered=False)
            inserted += len(result.inserted_ids)
        except BulkWriteError as exc:
            errors = exc.details.get("writeErrors", [])
            print(f"  {len(errors)} document(s) refused by MongoDB; first: "
                  f"{errors[0].get('errmsg', '')[:160]}")
            inserted += exc.details.get("nInserted", 0)
        documents = []

    for record in read_jsonl(ACTIVITY_FILE):
        document = {
            "user_id": record["user_id"],
            "event_type": record["event_type"],
            # The whole point of the relative offset: the newest event
            # is always "just now", so the TTL index keeps the stream.
            "timestamp": now - timedelta(seconds=record["seconds_ago"]),
            "metadata": record.get("metadata", {}),
        }
        if record.get("movie_id") is not None:
            document["movie_id"] = record["movie_id"]
        kinds[record["event_type"]] += 1
        documents.append(document)
        if len(documents) >= BATCH:
            flush()
    flush()

    oldest = now - timedelta(seconds=max(
        (r["seconds_ago"] for r in read_jsonl(ACTIVITY_FILE)), default=0))
    print(f"  user_activity_logs: {inserted:,} inserted  {dict(sorted(kinds.items()))}")
    print(f"  stream covers {oldest:%Y-%m-%d} to {now:%Y-%m-%d} "
          f"(inside the 90-day TTL window)")
    print(f"  user_activity_logs now holds "
          f"{db.user_activity_logs.count_documents({}):,} documents")


# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-tags", action="store_true")
    parser.add_argument("--skip-activity", action="store_true")
    args = parser.parse_args()

    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]

    print("Importing user data into MongoDB")
    if not args.skip_tags:
        print("\nReal tags from MovieLens 25M")
        import_tags(db)
    if not args.skip_activity:
        print("\nSynthetic activity stream (declared synthetic in the report)")
        import_activity(db)

    client.close()


if __name__ == "__main__":
    main()
