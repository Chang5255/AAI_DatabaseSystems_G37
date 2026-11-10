"""
INF2003 Group Project - Team 37
benchmark/mongo_bench.py : Experiment 1, MongoDB side

The counterpart of mysql_bench.py. Same four tasks, the same ids from
sample_ids.json, the same timing code (harness.time_call), so the two
summary CSVs can be put side by side in the report.

  T1 movie_cast_crew_2q    MySQL: two queries, each a 3-table join.
                           MongoDB: find_one with a projection to
                           {cast, crew} - one document, no join.
  T2 movie_cast_crew_json  MySQL: ONE statement building a JSON
                           document with JSON_ARRAYAGG.
                           MongoDB: the same find_one WITHOUT a
                           projection, i.e. the whole stored document.
                           This is not a second way of answering T1 -
                           MongoDB has only one - so the slot is used
                           to measure what the projection saves.
  T3 films_by_actor        MySQL: WHERE person_id = ? on movie_cast,
                           which has an index on person_id.
                           MongoDB: find on cast.person_id, which needs
                           the MULTIKEY index created by
                           create_indexes.py, plus the cast.$ positional
                           projection so only the matching array
                           element comes back.
  T4 rename_person         MySQL: UPDATE people - one row, because the
                           name is stored once.
                           MongoDB: the name is copied into every film
                           the person worked on, so every one of those
                           documents must be rewritten. This is the
                           update anomaly from lecture 4, measured.

T1 and T2 favour the embedded design; T3 and T4 favour the normalised
one. All four are reported, which is the only way the comparison is
fair.

Usage (from the repo root, MongoDB running, .env filled in):
    python benchmark/mongo_bench.py
    python benchmark/mongo_bench.py --samples 100 --repeats 10

Outputs in benchmark/results/:
    mongo_experiment1_summary.csv   same columns as the MySQL summary
    mongo_experiment1_raw.csv       every timed call
    mongo_experiment1_plans.txt     explain("executionStats") per task
"""

import argparse
import os
import platform
import time
from datetime import datetime

import harness
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(harness.REPO_ROOT / ".env")

BENCH_TAG = " [bench]"

TASKS = {
    "T1_movie_cast_crew_2q": "One movie's cast + crew, find_one with a projection",
    "T2_movie_cast_crew_json": "One movie's whole document, find_one with no projection",
    "T3_films_by_actor": "All films for one actor",
    "T4_rename_person": "Rename one person (every embedded copy)",
}


def mongo_collection():
    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
    return client, db.movie_features


# ---------------------------------------------------------------------
# Tasks. Each returns a row count so the summary can report avg_rows,
# and each fetches everything, as the MySQL side does.
# ---------------------------------------------------------------------
def t1(coll, movie_id):
    """Projection to the two arrays we actually need."""
    doc = coll.find_one({"movie_id": movie_id}, {"_id": 0, "cast": 1, "crew": 1}) or {}
    return len(doc.get("cast", [])) + len(doc.get("crew", []))


def t2(coll, movie_id):
    """The whole document: overview, keywords, companies, countries too."""
    doc = coll.find_one({"movie_id": movie_id}) or {}
    return len(doc.get("cast", [])) + len(doc.get("crew", []))


def t3(coll, person_id):
    """cast.$ returns only the matching array element, like the SQL row."""
    docs = list(coll.find({"cast.person_id": person_id},
                          {"_id": 0, "movie_id": 1, "cast.$": 1}))
    return len(docs)


# ---------------------------------------------------------------------
# T4: the rename, with its own loop so each timed rename is followed by
# an untimed restore (mirrors mysql_bench.run_rename).
# ---------------------------------------------------------------------
def rename(coll, person_id, new_name):
    """Rewrite this person's name wherever it is embedded.

    Two statements rather than one. A single update_many with
    arrayFilters for both arrays fails on films that have no crew array
    at all ("the path 'crew' must exist in the document in order to
    apply array updates"), and 28 of our films are in that state. Each
    call here is guarded by a match on the array it updates, so the
    array is guaranteed to exist.
    """
    cast_result = coll.update_many(
        {"cast.person_id": person_id},
        {"$set": {"cast.$[c].name": new_name}},
        array_filters=[{"c.person_id": person_id}],
    )
    crew_result = coll.update_many(
        {"crew.person_id": person_id},
        {"$set": {"crew.$[w].name": new_name}},
        array_filters=[{"w.person_id": person_id}],
    )
    return cast_result.modified_count + crew_result.modified_count


def original_name(coll, person_id):
    """The person's name as stored, or None if the copies disagree.

    Embedded copies are meant to be identical. If they are not, this
    benchmark would restore the wrong name, so that person is skipped -
    and a disagreement is itself worth reporting, because it is exactly
    the inconsistency that duplicated data invites.
    """
    names = coll.aggregate([
        {"$match": {"cast.person_id": person_id}},
        {"$unwind": "$cast"},
        {"$match": {"cast.person_id": person_id}},
        {"$group": {"_id": "$cast.name"}},
        {"$limit": 5},
    ])
    found = [row["_id"] for row in names]
    if len(found) == 1:
        return found[0]
    return None


def run_rename(coll, person_ids, repeats, warmup):
    raw, skipped, inconsistent = [], 0, []
    for person_id in person_ids:
        name = original_name(coll, person_id)
        if name is None:
            skipped += 1
            inconsistent.append(person_id)
            continue
        try:
            for run in range(warmup + repeats):
                t0 = time.perf_counter()
                modified = rename(coll, person_id, name + BENCH_TAG)
                ms = (time.perf_counter() - t0) * 1000.0
                rename(coll, person_id, name)              # restore, untimed
                if run >= warmup:
                    raw.append({"task": "T4_rename_person", "sample_id": person_id,
                                "run": run - warmup + 1, "ms": round(ms, 4),
                                "rows": modified})
        finally:
            rename(coll, person_id, name)
    return raw, skipped, inconsistent


def cleanup_tags(coll):
    """Undo any rename left behind by an interrupted run."""
    pattern = {"$regex": r"\[bench\]$"}
    stale_cast = list(coll.find({"cast.name": pattern}, {"_id": 1}))
    stale_crew = list(coll.find({"crew.name": pattern}, {"_id": 1}))
    if not stale_cast and not stale_crew:
        return 0

    fixed = 0
    for doc in coll.find({"$or": [{"cast.name": pattern}, {"crew.name": pattern}]},
                         {"cast.name": 1, "crew.name": 1}):
        updates = {}
        for field in ("cast", "crew"):
            for index, member in enumerate(doc.get(field, [])):
                name = member.get("name", "")
                if name.endswith(BENCH_TAG):
                    updates[f"{field}.{index}.name"] = name[: -len(BENCH_TAG)]
        if updates:
            coll.update_one({"_id": doc["_id"]}, {"$set": updates})
            fixed += 1
    return fixed


def run_task(coll, task, fn, ids, repeats, warmup):
    raw = []
    for sample_id in ids:
        samples, rows = harness.time_call(lambda: fn(coll, sample_id), repeats, warmup)
        for run, ms in enumerate(samples, start=1):
            raw.append({"task": task, "sample_id": sample_id, "run": run,
                        "ms": round(ms, 4), "rows": rows})
    return raw


# ---------------------------------------------------------------------
# Plans: MongoDB's answer to EXPLAIN ANALYZE
# ---------------------------------------------------------------------
def explain_find(coll, filter_doc, projection=None, limit=0):
    """
    Ask MongoDB for the plan AND the execution counters for one find.

    Written against the explain *database command* rather than
    Cursor.explain("executionStats"), because PyMongo's Cursor.explain()
    takes no arguments in several 4.x releases and raises

        TypeError: Cursor.explain() takes 1 positional argument but 2 were given

    The command form has accepted a verbosity argument since MongoDB 3.0
    and behaves the same on every driver version, so the benchmark does
    not depend on which PyMongo the marker happens to have installed.
    """
    find_cmd = {"find": coll.name, "filter": filter_doc}
    if projection:
        find_cmd["projection"] = projection
    if limit:
        find_cmd["limit"] = limit
    return coll.database.command("explain", find_cmd,
                                 verbosity="executionStats")


def capture_plans(coll, movie_id, person_id):
    out = []

    def describe(label, explain):
        stats = explain.get("executionStats", {})
        winning = explain.get("queryPlanner", {}).get("winningPlan", {})
        # MongoDB 6.0+ runs most of these through the slot-based engine,
        # and there winningPlan holds the stage tree one level down under
        # "queryPlan" with a "slotBasedPlan" sibling. Without this step
        # the plan line would read "?" on a modern server.
        if "stage" not in winning and "queryPlan" in winning:
            winning = winning["queryPlan"]
        stages = []
        node = winning
        while node:
            name = node.get("stage", "?")
            if "indexName" in node:
                name += f" ({node['indexName']})"
            stages.append(name)
            node = node.get("inputStage") or (node.get("inputStages") or [None])[0]
        out.append(
            f"===== {label} =====\n"
            f"plan            : {' <- '.join(stages)}\n"
            f"nReturned       : {stats.get('nReturned')}\n"
            f"docsExamined    : {stats.get('totalDocsExamined')}\n"
            f"keysExamined    : {stats.get('totalKeysExamined')}\n"
            f"executionTimeMs : {stats.get('executionTimeMillis')}\n"
        )

    describe("T1 find_one, projection {cast, crew}",
             explain_find(coll, {"movie_id": movie_id},
                          {"_id": 0, "cast": 1, "crew": 1}, limit=1))
    describe("T2 find_one, no projection",
             explain_find(coll, {"movie_id": movie_id}, limit=1))
    describe("T3 find on cast.person_id (multikey), projection cast.$",
             explain_find(coll, {"cast.person_id": person_id},
                          {"_id": 0, "movie_id": 1, "cast.$": 1}))

    # How many documents one rename has to touch, which is the whole
    # point of T4. The equivalent MySQL number is always 1.
    touched_cast = coll.count_documents({"cast.person_id": person_id})
    touched_crew = coll.count_documents({"crew.person_id": person_id})
    out.append(
        f"===== T4 rename reach =====\n"
        f"documents containing this person in cast : {touched_cast}\n"
        f"documents containing this person in crew : {touched_crew}\n"
        f"documents MySQL must update              : 1 (people.name)\n"
    )

    out.append("===== indexes on movie_features =====\n" + "\n".join(
        f"  {index['name']}: {dict(index['key'])}"
        f"{'  [unique]' if index.get('unique') else ''}"
        for index in coll.list_indexes()) + "\n")
    return "\n".join(out)


# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", type=int, default=300,
                        help="ids per task (default 300)")
    parser.add_argument("--repeats", type=int, default=5,
                        help="timed runs per id (default 5)")
    parser.add_argument("--warmup", type=int, default=1,
                        help="untimed runs per id (default 1)")
    parser.add_argument("--skip-rename", action="store_true",
                        help="skip T4, which writes to the database")
    args = parser.parse_args()

    if not harness.SAMPLE_FILE.exists():
        print(f"{harness.SAMPLE_FILE} is missing. Run mysql_bench.py first: it "
              f"draws the sample ids that both benchmarks must share.")
        return

    client, coll = mongo_collection()
    version = client.server_info()["version"]

    if coll.count_documents({}, limit=1) == 0:
        print("movie_features is empty. Run database/mongodb/setup_mongodb.py first.")
        client.close()
        return

    index_names = {index["name"] for index in coll.list_indexes()}
    if "idx_cast_person_id" not in index_names:
        print("WARNING: idx_cast_person_id is missing, so T3 will scan the whole\n"
              "         collection. Run database/mongodb/create_indexes.py for a\n"
              "         fair comparison with MySQL's indexed lookup.\n")

    fixed = cleanup_tags(coll)
    if fixed:
        print(f"Repaired {fixed} document(s) left tagged by an earlier run\n")

    samples = harness.load_samples()
    movie_ids = samples["movie_ids"][: args.samples]
    actor_ids = samples["actor_person_ids"][: args.samples]

    print(f"MongoDB {version} | {len(movie_ids)} movies, {len(actor_ids)} actors | "
          f"{args.warmup} warm-up + {args.repeats} timed runs each\n")

    raw, skipped, inconsistent = [], 0, []
    try:
        for task, fn, ids in [
            ("T1_movie_cast_crew_2q", t1, movie_ids),
            ("T2_movie_cast_crew_json", t2, movie_ids),
            ("T3_films_by_actor", t3, actor_ids),
        ]:
            print(f"running {task} ...")
            raw += run_task(coll, task, fn, ids, args.repeats, args.warmup)

        if args.skip_rename:
            print("skipping T4_rename_person")
        else:
            print("running T4_rename_person ...")
            rename_raw, skipped, inconsistent = run_rename(
                coll, actor_ids, args.repeats, args.warmup)
            raw += rename_raw
    finally:
        cleanup_tags(coll)

    summary = []
    for task, description in TASKS.items():
        task_rows = [row for row in raw if row["task"] == task]
        if not task_rows:
            continue
        summary.append({
            "database": "MongoDB",
            "task": task,
            "description": description,
            **harness.summarise([row["ms"] for row in task_rows]),
            "avg_rows": round(sum(row["rows"] for row in task_rows) / len(task_rows), 1),
        })

    header = (f"MongoDB {version}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"Run at {datetime.now().isoformat(timespec='seconds')}\n"
              f"samples={len(movie_ids)} repeats={args.repeats} warmup={args.warmup}\n")
    if skipped:
        header += (f"T4 skipped {skipped} person(s) whose embedded name copies "
                   f"disagree: {inconsistent[:10]}\n")
    header += "\n"

    harness.write_text("mongo_experiment1_plans.txt",
                       header + capture_plans(coll, movie_ids[0], actor_ids[0]))
    harness.write_csv("mongo_experiment1_raw.csv", raw)
    path = harness.write_csv("mongo_experiment1_summary.csv", summary)

    print()
    harness.print_table(summary, ["task", "n", "mean_ms", "median_ms", "p95_ms", "avg_rows"])
    print(f"\nResults written to {path.parent}")
    print("Merge both databases into one table with: python benchmark/merge_results.py")
    client.close()


if __name__ == "__main__":
    main()
