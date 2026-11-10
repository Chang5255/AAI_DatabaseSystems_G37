"""
INF2003 Group Project - Team 37
database/mongodb/build_costar_graph.py : build the co-star graph

WHY A SECOND, DERIVED COLLECTION
Lecture 8 lists graph databases (Neo4j) as a NoSQL category of their
own, because some questions are about paths rather than about rows:
"how are these two actors connected?" is a traversal, and in SQL it
needs one self-join per hop, with the number of joins unknown in
advance.

MongoDB's $graphLookup can walk a graph, but it needs an adjacency
list: a collection where each document points at its neighbours. Our
movie_features documents are film-centric, so this script turns them
into a person-centric collection:

    { person_id: 500,
      name: "Tom Cruise",
      film_count: 34,
      costar_ids: [ 1100, 2231, ... ] }   <- the adjacency list

That is denormalisation on purpose (lecture 8, scheme 2): the same
facts already exist in MySQL's movie_cast table and in the cast arrays,
stored a third time in the shape this one question needs. The cost is
honest and worth stating in the report: extra storage, and the graph
is stale until this script is run again.

Only the top TOP_BILLED cast members of each film are counted as
co-stars. A film's cast array can hold 100+ names, and including every
extra would connect almost everyone to almost everyone, making the
degrees of separation meaningless (and the graph enormous).

Usage (from the repo root):
    python database/mongodb/build_costar_graph.py
    python database/mongodb/build_costar_graph.py --top-billed 5
"""

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, UpdateOne

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

TOP_BILLED = 10


def build(db, top_billed=TOP_BILLED):
    """Read every film's cast and collect each person's co-stars."""

    names = {}
    films = {}
    costars = {}

    cursor = db.movie_features.find(
        {"cast": {"$exists": True, "$ne": []}},
        {"_id": 0, "movie_id": 1, "cast": 1},
    )

    films_seen = 0
    for doc in cursor:
        films_seen += 1

        billed = [c for c in doc.get("cast", [])
                  if c.get("person_id") is not None
                  and (c.get("order") is None or c["order"] < top_billed)]

        # One person can hold two credits on the same film, so de-duplicate.
        people = {}
        for member in billed:
            people.setdefault(int(member["person_id"]), member.get("name"))

        for person_id, name in people.items():
            if name:
                names[person_id] = name
            films[person_id] = films.get(person_id, 0) + 1
            neighbours = costars.setdefault(person_id, set())
            neighbours.update(pid for pid in people if pid != person_id)

    print(f"Read {films_seen} films, found {len(costars)} top-billed people")

    operations = []
    now = datetime.now(timezone.utc)
    for person_id, neighbours in costars.items():
        operations.append(UpdateOne(
            {"person_id": person_id},
            {"$set": {
                "person_id": person_id,
                "name": names.get(person_id, f"person {person_id}"),
                "film_count": int(films[person_id]),
                "costar_ids": sorted(neighbours),
                "built_at": now,
            }},
            upsert=True,
        ))

    return operations, costars


def write(db, operations):
    """Replace the graph, in batches (one bulk_write of 48k ops is large)."""

    db.people_graph.delete_many({})

    batch_size = 2000
    inserted = 0
    for start in range(0, len(operations), batch_size):
        result = db.people_graph.bulk_write(operations[start:start + batch_size],
                                            ordered=False)
        inserted += result.upserted_count + result.modified_count

    db.people_graph.create_index([("person_id", ASCENDING)], unique=True,
                                 name="idx_graph_person_id")
    db.people_graph.create_index([("name", ASCENDING)], name="idx_graph_name")
    return inserted


def summarise(db, costars):
    degrees = sorted((len(v) for v in costars.values()), reverse=True)
    if not degrees:
        print("No cast data found: is movie_features loaded?")
        return

    middle = degrees[len(degrees) // 2]
    print(f"\npeople_graph: {db.people_graph.count_documents({})} documents")
    print(f"  co-stars per person: max {degrees[0]}, median {middle}, min {degrees[-1]}")
    print(f"  edges (directed):    {sum(degrees):,}")

    print("\n  Most connected people")
    for doc in db.people_graph.find({}, {"_id": 0, "name": 1, "film_count": 1,
                                         "costar_ids": 1}).sort(
            "film_count", -1).limit(5):
        print(f"    {doc['name']:<28} {doc['film_count']:>3} films, "
              f"{len(doc['costar_ids']):>4} co-stars")

    stats = db.command("collStats", "people_graph")
    print(f"\n  storage: data {stats['size'] / 1e6:.2f} MB, "
          f"indexes {stats['totalIndexSize'] / 1e6:.2f} MB "
          f"(the price of denormalising for one question)")


def main():
    parser = argparse.ArgumentParser(description="Build the co-star graph")
    parser.add_argument("--top-billed", type=int, default=TOP_BILLED,
                        help=f"cast positions counted as co-stars (default {TOP_BILLED})")
    args = parser.parse_args()

    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]

    if db.movie_features.count_documents({}, limit=1) == 0:
        print("movie_features is empty. Run import_movie_features.py first.")
        client.close()
        return

    print(f"Building the co-star graph from the top {args.top_billed} "
          f"billed cast members of each film")

    operations, costars = build(db, args.top_billed)
    if operations:
        written = write(db, operations)
        print(f"Wrote {written} people_graph documents")
        summarise(db, costars)

    client.close()


if __name__ == "__main__":
    main()
