"""
INF2003 Group Project - Team 37
database/mongodb/setup_mongodb.py : build the whole MongoDB side

Runs the scripts in the only order that works:

  1. create_collections.py   collections + $jsonSchema validators, so
                             every document inserted afterwards is
                             validated on the way in
  2. import_movie_features.py  4,803 film documents from
                             data/cleaned/movie_features.jsonl
  3. import_posters.py       poster paths from data/cleaned/posters.csv
  4. import_user_data.py     72,600 REAL MovieLens tags, plus the
                             synthetic activity stream (its timestamps
                             are made relative to this moment, so the
                             TTL index does not delete it)
  5. build_costar_graph.py   derive people_graph from the cast arrays
                             (needs the films to be imported first)
  6. create_indexes.py       every index, including the unique, TTL and
                             text ones (built last: indexing after a
                             bulk load is faster than maintaining the
                             indexes during it, and the unique tag
                             index needs the data in place to check)

Usage (from the repo root, with MongoDB running):
    python database/mongodb/setup_mongodb.py
    python database/mongodb/setup_mongodb.py --skip-posters
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
MONGO_DIR = ROOT_DIR / "database" / "mongodb"

SCRIPTS = [
    ("create_collections.py", "collections and validators"),
    ("import_movie_features.py", "film documents"),
    ("import_posters.py", "poster paths"),
    ("import_user_data.py", "real tags + synthetic activity stream"),
    ("build_costar_graph.py", "co-star graph for $graphLookup"),
    ("create_indexes.py", "indexes (unique, TTL, text, multikey)"),
]


def run_script(script_name, description):
    print("\n" + "=" * 62)
    print(f"Running: {script_name}  ({description})")
    print("=" * 62)

    result = subprocess.run([sys.executable, str(MONGO_DIR / script_name)])

    if result.returncode != 0:
        print(f"\nSetup failed while running {script_name}")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Set up the MongoDB database")
    parser.add_argument("--skip-posters", action="store_true",
                        help="skip import_posters.py")
    parser.add_argument("--skip-graph", action="store_true",
                        help="skip build_costar_graph.py")
    parser.add_argument("--skip-user-data", action="store_true",
                        help="skip import_user_data.py (tags and activity)")
    args = parser.parse_args()

    print("\nMongoDB Project Setup")
    print("=" * 62)

    for script_name, description in SCRIPTS:
        if args.skip_posters and script_name == "import_posters.py":
            print(f"\nSkipping {script_name}")
            continue
        if args.skip_graph and script_name == "build_costar_graph.py":
            print(f"\nSkipping {script_name}")
            continue
        if args.skip_user_data and script_name == "import_user_data.py":
            print(f"\nSkipping {script_name}")
            continue
        run_script(script_name, description)

    print("\n" + "=" * 62)
    print("MongoDB setup completed successfully.")
    print("=" * 62)


if __name__ == "__main__":
    main()
