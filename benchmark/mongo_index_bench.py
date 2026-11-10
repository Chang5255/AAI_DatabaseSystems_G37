"""
INF2003 Group Project - Team 37
benchmark/mongo_index_bench.py : Experiment 3, indexing in MongoDB

The MongoDB counterpart of the MySQL indexing work. 07_indexes.sql
shows a table scan turning into an index lookup with EXPLAIN ANALYZE,
and ratings_scaling.py shows how that gap grows with table size. This
script asks the same question of MongoDB:

    how much does the MULTIKEY index on cast.person_id matter?

A multikey index is the feature with no MySQL equivalent: it indexes
every element of an embedded array, so one film document with 80 cast
members produces 80 index entries. Without it, "every film this actor
appears in" is a COLLSCAN - MongoDB reads all 4,803 documents and
inspects every cast array. With it, the query reads only the documents
that match.

For each of the three queries below, the index is dropped, the query
is timed, the index is rebuilt (build time recorded) and the query is
timed again. The evidence for the report is not only the timing but
the explain output:

    COLLSCAN + totalDocsExamined = 4803   -> read everything
    IXSCAN   + totalDocsExamined = n      -> read only the matches

Dropping and rebuilding an index changes the database, so the script
restores exactly the indexes it found, and refuses to touch anything
else. It never writes a document.

Usage (from the repo root):
    python benchmark/mongo_index_bench.py
    python benchmark/mongo_index_bench.py --samples 50 --repeats 10

Outputs in benchmark/results/:
    mongo_index_summary.csv   one row per query x index state
    mongo_index_plans.txt     the explain output behind every row
"""

import argparse
import os
import platform
import time
from datetime import datetime

import harness
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient

load_dotenv(harness.REPO_ROOT / ".env")

def explain_find(coll, filter_doc, projection=None, limit=0):
    """
    Ask MongoDB for the plan AND the execution counters for one find.

    Written against the explain *database command* rather than
    Cursor.explain("executionStats"), because PyMongo's Cursor.explain()
    takes no arguments and raises

        TypeError: Cursor.explain() takes 1 positional argument but 2 were given

    Verbosity has to be asked for at the command level. The command form
    has accepted it since MongoDB 3.0 and behaves identically on every
    driver version, so this benchmark does not depend on which PyMongo
    happens to be installed.

    executionStats, not the default queryPlanner, is the whole point
    here: totalDocsExamined and totalKeysExamined are the numbers that
    show what the index actually saved. queryPlanner alone would say
    which plan was chosen but not what it cost.
    """
    find_cmd = {"find": coll.name, "filter": filter_doc}
    if projection:
        find_cmd["projection"] = projection
    if limit:
        find_cmd["limit"] = limit
    return coll.database.command("explain", find_cmd,
                                 verbosity="executionStats")


# (index name, key spec, the query it serves)
EXPERIMENTS = [
    {
        "name": "films_by_actor",
        "index": "idx_cast_person_id",
        "keys": [("cast.person_id", ASCENDING)],
        "description": "All films one actor appears in (multikey on cast.person_id)",
        "sample_key": "actor_person_ids",
        "query": lambda coll, pid: list(coll.find(
            {"cast.person_id": pid}, {"_id": 0, "movie_id": 1, "cast.$": 1})),
        "explain": lambda coll, pid: explain_find(
            coll, {"cast.person_id": pid},
            {"_id": 0, "movie_id": 1, "cast.$": 1}),
    },
    {
        "name": "film_by_id",
        "index": "idx_movie_id_unique",
        "keys": [("movie_id", ASCENDING)],
        "unique": True,
        "description": "One film by its id (the join key to MySQL)",
        "sample_key": "movie_ids",
        "query": lambda coll, mid: list(coll.find(
            {"movie_id": mid}, {"_id": 0, "cast": 1, "crew": 1}).limit(1)),
        "explain": lambda coll, mid: explain_find(
            coll, {"movie_id": mid}, {"_id": 0, "cast": 1, "crew": 1},
            limit=1),
    },
    {
        "name": "films_by_keyword",
        "index": "idx_keywords_name",
        "keys": [("keywords.name", ASCENDING)],
        "description": "Films carrying one keyword (multikey on keywords.name)",
        "sample_key": "keywords",
        "query": lambda coll, kw: list(coll.find(
            {"keywords.name": kw}, {"_id": 0, "movie_id": 1}).limit(100)),
        "explain": lambda coll, kw: explain_find(
            coll, {"keywords.name": kw}, {"_id": 0, "movie_id": 1},
            limit=100),
    },
]


def plan_of(explain):
    """Flatten the winning plan into one readable line."""
    node = explain.get("queryPlanner", {}).get("winningPlan", {})
    # MongoDB 6.0+ answers some queries with the slot-based engine, and
    # there the stage tree sits one level down under "queryPlan" beside a
    # "slotBasedPlan" sibling. Classic-engine finds keep "stage" at the
    # top. Without this the plan column would read "?" on a modern
    # server, which would make the whole before/after table useless.
    if "stage" not in node and "queryPlan" in node:
        node = node["queryPlan"]
    stages = []
    while node:
        name = node.get("stage", "?")
        if "indexName" in node:
            name += f" ({node['indexName']})"
        if node.get("isMultiKey"):
            name += " multikey"
        stages.append(name)
        node = node.get("inputStage") or (node.get("inputStages") or [None])[0]
    return " <- ".join(stages)


def measure(coll, experiment, ids, repeats, warmup):
    """Time the query over every sample id and collect explain stats."""
    samples_ms, rows, docs_examined, keys_examined = [], [], [], []
    for sample_id in ids:
        timings, result = harness.time_call(
            lambda: experiment["query"](coll, sample_id), repeats, warmup)
        samples_ms += timings
        rows.append(len(result))
        stats = experiment["explain"](coll, sample_id).get("executionStats", {})
        docs_examined.append(stats.get("totalDocsExamined", 0))
        keys_examined.append(stats.get("totalKeysExamined", 0))

    example = experiment["explain"](coll, ids[0])
    return {
        **harness.summarise(samples_ms),
        "avg_rows": round(sum(rows) / len(rows), 1),
        "avg_docs_examined": round(sum(docs_examined) / len(docs_examined), 1),
        "avg_keys_examined": round(sum(keys_examined) / len(keys_examined), 1),
        "plan": plan_of(example),
    }, example


def sample_keywords(coll, count):
    """Keywords that several films share, for the keyword experiment."""
    rows = coll.aggregate([
        {"$unwind": "$keywords"},
        {"$group": {"_id": "$keywords.name", "films": {"$sum": 1}}},
        {"$match": {"films": {"$gte": 5}}},
        {"$sort": {"films": -1}},
        {"$limit": count},
    ])
    return [row["_id"] for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", type=int, default=50,
                        help="ids per query (default 50; the unindexed runs are slow)")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()

    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
    coll = db.movie_features
    version = client.server_info()["version"]

    total_docs = coll.count_documents({})
    if total_docs == 0:
        print("movie_features is empty. Run database/mongodb/setup_mongodb.py first.")
        client.close()
        return

    if harness.SAMPLE_FILE.exists():
        samples = harness.load_samples()
    else:
        print("sample_ids.json is missing; run mysql_bench.py first.")
        client.close()
        return
    samples["keywords"] = sample_keywords(coll, args.samples)

    existing = {index["name"] for index in coll.list_indexes()}
    print(f"MongoDB {version} | movie_features: {total_docs} documents\n")

    rows, plans = [], []
    for experiment in EXPERIMENTS:
        ids = samples[experiment["sample_key"]][: args.samples]
        if not ids:
            continue
        print(f"{experiment['name']} ({len(ids)} ids)")

        # ---- without the index -------------------------------------
        had_index = experiment["index"] in existing
        if had_index:
            coll.drop_index(experiment["index"])
        stats_without, plan_without = measure(coll, experiment, ids,
                                              args.repeats, args.warmup)
        print(f"  without index: median {stats_without['median_ms']:>8.2f} ms, "
              f"{stats_without['avg_docs_examined']:>8.1f} docs examined  "
              f"{stats_without['plan']}")

        # ---- rebuild and time the build ----------------------------
        build_started = time.perf_counter()
        coll.create_index(experiment["keys"], name=experiment["index"],
                          unique=experiment.get("unique", False))
        build_ms = (time.perf_counter() - build_started) * 1000.0

        stats_with, plan_with = measure(coll, experiment, ids,
                                        args.repeats, args.warmup)
        print(f"  with index:    median {stats_with['median_ms']:>8.2f} ms, "
              f"{stats_with['avg_docs_examined']:>8.1f} docs examined  "
              f"{stats_with['plan']}")

        speedup = (stats_without["median_ms"] / stats_with["median_ms"]
                   if stats_with["median_ms"] else float("nan"))
        print(f"  index built in {build_ms:.0f} ms, speed-up {speedup:.1f}x\n")

        for state, stats in (("no index", stats_without), ("indexed", stats_with)):
            rows.append({
                "database": "MongoDB",
                "query": experiment["name"],
                "description": experiment["description"],
                "index_state": state,
                "index_name": experiment["index"] if state == "indexed" else "",
                "collection_docs": total_docs,
                **stats,
                "index_build_ms": round(build_ms, 1) if state == "indexed" else "",
                "speedup_vs_no_index": round(speedup, 2) if state == "indexed" else "",
            })

        plans.append(f"########## {experiment['name']}: {experiment['description']}\n\n"
                     f"----- WITHOUT {experiment['index']} -----\n"
                     f"{plan_without.get('queryPlanner', {})}\n\n"
                     f"executionStats: {plan_without.get('executionStats', {})}\n\n"
                     f"----- WITH {experiment['index']} (built in {build_ms:.0f} ms) -----\n"
                     f"{plan_with.get('queryPlanner', {})}\n\n"
                     f"executionStats: {plan_with.get('executionStats', {})}\n")

        if not had_index:
            # Leave the database as it was found.
            coll.drop_index(experiment["index"])
            print(f"  (dropped {experiment['index']} again: it was not there before)\n")

    stats = db.command("collStats", "movie_features")
    footer = (f"\nmovie_features storage: data {stats['size'] / 1e6:.1f} MB, "
              f"indexes {stats['totalIndexSize'] / 1e6:.1f} MB\n"
              f"index sizes: {ic_sizes(stats)}\n")
    print(footer)

    header = (f"MongoDB {version}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"samples={args.samples} repeats={args.repeats} warmup={args.warmup}\n"
              f"collection: movie_features, {total_docs} documents\n\n")
    harness.write_text("mongo_index_plans.txt", header + "\n".join(plans) + footer)
    path = harness.write_csv("mongo_index_summary.csv", rows)
    print(f"Results written to {path.parent}")
    client.close()


def ic_sizes(stats):
    sizes = stats.get("indexSizes", {})
    return ", ".join(f"{name} {size / 1e6:.2f} MB" for name, size in sizes.items())


if __name__ == "__main__":
    main()
