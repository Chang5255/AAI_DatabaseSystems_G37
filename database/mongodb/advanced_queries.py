"""
INF2003 Group Project - Team 37
database/mongodb/advanced_queries.py : the advanced MongoDB features

aggregation_queries.py holds the basic pipelines ($unwind, $group,
$sort). This file holds the four things MongoDB can do that are
awkward, verbose or impossible in our MySQL schema:

  1. $graphLookup   recursive traversal of the co-star graph. In SQL
                    this needs one self-join per hop, written in
                    advance; here the depth is a parameter. Lecture 8
                    puts graph databases in their own NoSQL category -
                    this is that capability inside a document database.

  2. $facet         several independent aggregations over the same
                    input, in ONE pass and ONE round trip. The MySQL
                    equivalent is six separate queries, or one query
                    with WITH ROLLUP that only groups one way.

  3. $lookup        a left outer join between two collections. MongoDB
                    "has no joins" is a half-truth: it has no foreign
                    keys, but $lookup joins at query time. Used here to
                    attach each film's document to the tags users wrote.

  4. $bucket        histogram buckets computed by the server, the
                    counterpart of a CASE expression in GROUP BY.

Every function takes the database handle as its first argument, so the
web app can pass its own pooled connection instead of opening another.

Run the demo from the repo root:
    python database/mongodb/advanced_queries.py
"""

import os
import re

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

# Guard rails for the graph walk: a $in list of 100,000 ids is slower
# than the scan it was meant to avoid, and the frontier of a co-star
# graph grows by roughly 30x per degree.
MAX_FRONTIER = 40000
IN_BATCH = 4000


# =====================================================================
# 1. $graphLookup - the co-star graph
# =====================================================================
def find_people(db, name_fragment, limit=15):
    """Look up people in the graph by part of their name (for the UI).

    re.escape is the NoSQL counterpart of a parameterised query. The
    text comes from a form field, and passing it into $regex unescaped
    would let a visitor send a pattern such as "(a+)+$", which the
    regex engine can take exponential time to evaluate - a denial of
    service rather than an injection, but a vulnerability either way.
    """
    if not name_fragment or len(name_fragment) < 2:
        return []
    return list(db.people_graph.find(
        {"name": {"$regex": re.escape(name_fragment), "$options": "i"}},
        {"_id": 0, "person_id": 1, "name": 1, "film_count": 1, "costar_ids": 1},
    ).sort("film_count", -1).limit(limit))


def costar_network(db, person_id, max_degrees=2, min_films=2):
    """How many people are within max_degrees of this person.

    One $graphLookup does the whole walk inside the server: startWith
    seeds it with the person's own co-stars, then it follows
    costar_ids -> person_id repeatedly until maxDepth. depthField
    records how many hops each person was found at, so the result can
    be grouped into "1 degree away", "2 degrees away", and so on.

    restrictSearchWithMatch keeps the walk to people with at least
    min_films top-billed films. Without it the traversal pulls in
    thousands of one-film actors, and because $graphLookup collects
    every match into an array inside ONE document, the result can hit
    MongoDB's 16 MB document limit. That limit is the honest drawback
    of doing this in a document database rather than in Neo4j.
    """
    pipeline = [
        {"$match": {"person_id": person_id}},
        {"$graphLookup": {
            "from": "people_graph",
            "startWith": "$costar_ids",
            "connectFromField": "costar_ids",
            "connectToField": "person_id",
            "as": "network",
            # depth 0 of the walk is already 1 degree of separation
            "maxDepth": max(0, max_degrees - 1),
            "depthField": "depth",
            "restrictSearchWithMatch": {"film_count": {"$gte": min_films}},
        }},
        {"$unwind": "$network"},
        # Exclude the starting person, who can be reached back again
        {"$match": {"$expr": {"$ne": ["$network.person_id", "$person_id"]}}},
        # Sort before grouping so that $push keeps the busiest actors
        # first and $slice can take the top few as examples. ($topN
        # would be neater but needs MongoDB 5.2+.)
        {"$sort": {"network.film_count": -1}},
        {"$group": {
            "_id": {"$add": ["$network.depth", 1]},
            "people": {"$sum": 1},
            "names": {"$push": "$network.name"},
        }},
        {"$project": {"_id": 0, "degrees": "$_id", "people": 1,
                      "examples": {"$slice": ["$names", 5]}}},
        {"$sort": {"degrees": 1}},
    ]
    return list(db.people_graph.aggregate(pipeline, allowDiskUse=True))


def degrees_of_separation(db, person_a, person_b, max_degrees=3):
    """The shortest co-star chain between two people, with the path.

    $graphLookup answers "who is reachable", but it does not record
    WHICH route it took, so it cannot produce the chain itself. This is
    a breadth-first search instead: each round is one indexed find()
    with an $in on person_id, so the work still happens in the server
    and the number of round trips is at most max_degrees.

    Returns {"degrees": n, "path": [{person_id, name}, ...]} or
    {"degrees": None} when no chain was found within max_degrees.
    """
    if person_a == person_b:
        doc = db.people_graph.find_one({"person_id": person_a}, {"_id": 0, "name": 1})
        return {"degrees": 0, "path": [{"person_id": person_a,
                                        "name": (doc or {}).get("name")}],
                "searched": 0, "truncated": False}

    parent = {person_a: None}
    frontier = [person_a]
    truncated = False

    for degree in range(1, max_degrees + 1):
        next_frontier = []
        for start in range(0, len(frontier), IN_BATCH):
            batch = frontier[start:start + IN_BATCH]
            cursor = db.people_graph.find(
                {"person_id": {"$in": batch}},
                {"_id": 0, "person_id": 1, "costar_ids": 1},
            )
            for doc in cursor:
                for neighbour in doc["costar_ids"]:
                    if neighbour not in parent:
                        parent[neighbour] = doc["person_id"]
                        next_frontier.append(neighbour)

        if person_b in parent:
            chain = [person_b]
            while parent[chain[-1]] is not None:
                chain.append(parent[chain[-1]])
            chain.reverse()
            names = {d["person_id"]: d["name"] for d in db.people_graph.find(
                {"person_id": {"$in": chain}}, {"_id": 0, "person_id": 1, "name": 1})}
            return {
                "degrees": degree,
                "path": [{"person_id": pid, "name": names.get(pid)} for pid in chain],
                "searched": len(parent),
                "truncated": truncated,
            }

        if not next_frontier:
            break
        if len(next_frontier) > MAX_FRONTIER:
            next_frontier = next_frontier[:MAX_FRONTIER]
            truncated = True
        frontier = next_frontier

    return {"degrees": None, "path": [], "searched": len(parent),
            "truncated": truncated}


def shared_films(db, person_a, person_b, limit=10):
    """Films both people are billed in: a multikey index lookup with $all."""
    docs = list(db.movie_features.find(
        {"cast.person_id": {"$all": [person_a, person_b]}},
        {"_id": 0, "movie_id": 1, "cast": 1},
    ).limit(limit))
    out = []
    for doc in docs:
        roles = {c["person_id"]: c.get("character")
                 for c in doc.get("cast", [])
                 if c.get("person_id") in (person_a, person_b)}
        out.append({"movie_id": doc["movie_id"],
                    "role_a": roles.get(person_a),
                    "role_b": roles.get(person_b)})
    return out


# =====================================================================
# 2. and 4. $facet with a nested $bucket
# =====================================================================
def catalogue_facets(db, limit=10):
    """Six breakdowns of the catalogue in one pass over the collection.

    Each key of $facet is an independent sub-pipeline fed by the same
    input documents. The same answer in MySQL is six round trips, each
    re-reading or re-joining the data; here the collection is scanned
    once and the server returns one document holding all six results.

    The last two facets use $bucket, which is a server-side histogram:
    it takes boundaries and counts the documents falling between them.
    """
    pipeline = [
        {"$facet": {

            "top_keywords": [
                {"$unwind": "$keywords"},
                {"$group": {"_id": "$keywords.name", "films": {"$sum": 1}}},
                {"$sort": {"films": -1, "_id": 1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "keyword": "$_id", "films": 1}},
            ],

            "top_companies": [
                {"$unwind": "$production_companies"},
                {"$group": {"_id": "$production_companies.name",
                            "films": {"$sum": 1}}},
                {"$sort": {"films": -1, "_id": 1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "company": "$_id", "films": 1}},
            ],

            "top_countries": [
                {"$unwind": "$production_countries"},
                {"$group": {"_id": "$production_countries.name",
                            "films": {"$sum": 1}}},
                {"$sort": {"films": -1, "_id": 1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "country": "$_id", "films": 1}},
            ],

            "top_languages": [
                {"$unwind": "$spoken_languages"},
                {"$group": {"_id": "$spoken_languages.name",
                            "films": {"$sum": 1}}},
                {"$sort": {"films": -1, "_id": 1}},
                {"$limit": limit},
                {"$project": {"_id": 0, "language": "$_id", "films": 1}},
            ],

            # $bucket: how big are the cast lists? This is a question
            # about the document's own shape, which a normalised schema
            # can only answer with a GROUP BY + COUNT subquery.
            "cast_size_buckets": [
                {"$project": {"cast_size": {"$size": {"$ifNull": ["$cast", []]}}}},
                {"$bucket": {
                    "groupBy": "$cast_size",
                    "boundaries": [0, 1, 5, 10, 20, 50, 100, 1000],
                    "default": "unknown",
                    "output": {"films": {"$sum": 1},
                               "avg_cast": {"$avg": "$cast_size"}},
                }},
                {"$project": {"_id": 0, "from_cast_size": "$_id", "films": 1,
                              "avg_cast": {"$round": ["$avg_cast", 1]}}},
            ],

            "crew_departments": [
                {"$unwind": "$crew"},
                {"$group": {"_id": "$crew.department",
                            "credits": {"$sum": 1},
                            "people": {"$addToSet": "$crew.person_id"}}},
                {"$project": {"_id": 0, "department": "$_id", "credits": 1,
                              "distinct_people": {"$size": "$people"}}},
                {"$sort": {"credits": -1}},
                {"$limit": limit},
            ],
        }},
    ]
    result = list(db.movie_features.aggregate(pipeline, allowDiskUse=True))
    return result[0] if result else {}


# =====================================================================
# 3. $lookup - joining two collections at query time
# =====================================================================
def tagged_films(db, limit=20):
    """The most-tagged films, joined to their film document.

    $lookup is a left outer join: user_tags (the "many" side) is
    grouped by film first, so the join runs once per film rather than
    once per tag. localField/foreignField are movie_id on both sides -
    the same key MySQL uses, but with no foreign key behind it, so a
    tag can point at a film that does not exist. The $match on the
    joined array is how we detect that.
    """
    pipeline = [
        {"$group": {
            "_id": "$movie_id",
            "tags": {"$addToSet": "$tag"},
            "tag_count": {"$sum": 1},
            "taggers": {"$addToSet": "$user_id"},
        }},
        {"$sort": {"tag_count": -1, "_id": 1}},
        {"$limit": limit},
        {"$lookup": {
            "from": "movie_features",
            "localField": "_id",
            "foreignField": "movie_id",
            "as": "film",
            # A sub-pipeline would also work here; localField /
            # foreignField uses idx_movie_id_unique directly.
        }},
        {"$project": {
            "_id": 0,
            "movie_id": "$_id",
            "tags": 1,
            "tag_count": 1,
            "distinct_taggers": {"$size": "$taggers"},
            # $arrayElemAt turns the one-element join result into a field
            "overview": {"$arrayElemAt": ["$film.overview", 0]},
            "poster_path": {"$arrayElemAt": ["$film.poster_path", 0]},
            "keywords": {"$slice": [
                {"$ifNull": [{"$arrayElemAt": ["$film.keywords.name", 0]}, []]}, 6]},
            # True when the tag points at a film with no document:
            # the integrity check a FOREIGN KEY would have made for us.
            "orphan": {"$eq": [{"$size": "$film"}, 0]},
        }},
    ]
    return list(db.user_tags.aggregate(pipeline))


def tag_vocabulary(db, limit=25):
    """$sortByCount: the shorthand for $group + $sort on one field."""
    return list(db.user_tags.aggregate([
        {"$sortByCount": "$tag"},
        {"$limit": limit},
        {"$project": {"_id": 0, "tag": "$_id", "uses": "$count"}},
    ]))


# =====================================================================
# Demo
# =====================================================================
if __name__ == "__main__":
    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]

    if "people_graph" not in db.list_collection_names():
        print("people_graph is missing. Run build_costar_graph.py first.\n")
    else:
        print("--- $graphLookup: the network around one actor ---")
        start = db.people_graph.find_one(sort=[("film_count", -1)])
        print(f"Starting from {start['name']} "
              f"({start['film_count']} films, {len(start['costar_ids'])} co-stars)")
        for row in costar_network(db, start["person_id"], max_degrees=2):
            print(f"  {row['degrees']} degree(s): {row['people']:>6} people  "
                  f"e.g. {', '.join(row['examples'][:3])}")

        print("\n--- Breadth-first search: the shortest chain ---")
        other = db.people_graph.find_one(
            {"person_id": {"$nin": [start["person_id"]] + start["costar_ids"]}},
            sort=[("film_count", -1)])
        if other:
            result = degrees_of_separation(db, start["person_id"], other["person_id"])
            if result["degrees"]:
                chain = " -> ".join(p["name"] for p in result["path"])
                print(f"  {result['degrees']} degrees: {chain}")
                print(f"  (examined {result['searched']} people)")
            else:
                print(f"  no chain within 3 degrees ({result['searched']} examined)")

    print("\n--- $facet + $bucket: six breakdowns, one pass ---")
    facets = catalogue_facets(db, limit=5)
    for name, rows in facets.items():
        print(f"  {name}")
        for row in rows:
            print(f"    {row}")

    print("\n--- $lookup: most-tagged films joined to their documents ---")
    tagged = tagged_films(db, limit=5)
    if tagged:
        for row in tagged:
            overview = (row.get("overview") or "")[:60]
            print(f"  film {row['movie_id']}: {row['tag_count']} tags "
                  f"{row['tags'][:3]} orphan={row['orphan']}  {overview}...")
    else:
        print("  no tags yet: add some on a film page first")

    client.close()
