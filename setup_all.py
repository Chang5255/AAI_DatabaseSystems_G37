"""
INF2003 Group Project - Team 37
setup_all.py : build both databases with one command

    python setup_all.py

What it does
  MySQL (as root, because the scripts create a database, accounts and
  triggers):
    01_schema.sql       database, 8 tables, keys, CHECKs
    02_load.sql         the cleaned CSVs, with the file paths filled in
                        so nobody has to edit the file by hand
    04_views.sql        the six views
    01b_create_user.sql the three application accounts, with the
                        CHANGE_ME_* placeholders filled in from .env.
                        After the views, because it grants SELECT on them
    07_indexes.sql      the indexing experiments and the indexes they add
    08_triggers.sql     the audit table and the trigger constraints
  then, with --with-demos, also 03_verify.sql, 05_crud.sql,
  06_queries.sql and 09_transactions.sql, which print their output and
  change nothing.

  MongoDB: runs database/mongodb/setup_mongodb.py, which creates the
  collections and validators, imports the documents, builds the co-star
  graph and creates every index.

Then it checks both databases and prints what it found.

WARNING: 01_schema.sql begins with DROP DATABASE IF EXISTS moviedb.
Everything currently in moviedb is destroyed. The script asks before
doing it unless --yes is given.

Options
  --mysql-only / --mongo-only   do half the work
  --skip-load                   MySQL schema and objects, no data
  --with-demos                  also run the read-only demo scripts
  --root-user / --root-password MySQL admin account (prompted if absent)
  --yes                         do not ask for confirmation
"""

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
MYSQL_DIR = REPO_ROOT / "database" / "mysql"
MONGO_DIR = REPO_ROOT / "database" / "mongodb"
CLEANED_DIR = REPO_ROOT / "data" / "cleaned"

# Order matters. 01b grants SELECT on the six views to the reporting
# account, and a table-level GRANT fails if the object does not exist,
# so the views must be created before the accounts are set up.
CORE_SCRIPTS = ["01_schema.sql", "02_load.sql", "04_views.sql",
                "01b_create_user.sql", "07_indexes.sql", "08_triggers.sql"]
DEMO_SCRIPTS = ["03_verify.sql", "05_crud.sql", "06_queries.sql",
                "09_transactions.sql"]

EXPECTED_ROWS = {
    "movies": 4803, "genres": 20, "movie_genres": 12160, "people": 104842,
    "movie_cast": 106257, "movie_crew": 129581,
    # 12 synthetic demo users with 784 synthetic ratings, plus 1,500 real
    # MovieLens 25M users with 246,192 real ratings.
    # See data/cleaned/movielens_report.txt for the audit trail.
    "users": 1512, "ratings": 246976,
}


# =====================================================================
# Reading a .sql file the way the mysql client would
# =====================================================================
def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*--.*$", "", text)
    return text.strip()


def split_statements(sql_text):
    """Split a script into statements, honouring DELIMITER.

    mysql.connector speaks the protocol, not the mysql command-line
    client's language, so DELIMITER (which the trigger and procedure
    definitions in 07 and 08 need) has to be handled here.
    """
    delimiter = ";"
    statements, buffer = [], []

    def flush():
        text = "\n".join(buffer)
        if strip_comments(text):
            statements.append(text.strip())
        buffer.clear()

    for raw_line in sql_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.upper().startswith("DELIMITER"):
            parts = stripped.split()
            if len(parts) >= 2:
                flush()
                delimiter = parts[1]
            continue

        buffer.append(line)
        if stripped.endswith(delimiter):
            text = "\n".join(buffer).rstrip()
            text = text[: -len(delimiter)]
            buffer.clear()
            if strip_comments(text):
                statements.append(text.strip())

    flush()
    return statements


def prepare(script_name, text, passwords):
    """Fill in the placeholders that a human would otherwise edit."""
    if script_name == "02_load.sql":
        data_path = str(CLEANED_DIR).replace("\\", "/")
        text = text.replace("CHANGE/ME/cleaned", data_path)
    if script_name == "01b_create_user.sql":
        for placeholder, password in passwords.items():
            text = text.replace(placeholder, password)
    return text


# =====================================================================
# Running the MySQL side
# =====================================================================
def run_script(conn, script_name, text, verbose=False):
    statements = split_statements(text)
    cur = conn.cursor()
    executed, failed = 0, []

    for index, statement in enumerate(statements, start=1):
        try:
            cur.execute(statement)
            # Consume every result set, or the connection stays busy.
            while True:
                if cur.with_rows:
                    rows = cur.fetchall()
                    if verbose and rows:
                        print(f"      -> {rows[:3]}{' ...' if len(rows) > 3 else ''}")
                if not cur.nextset():
                    break
            executed += 1
        except Exception as exc:
            first_line = strip_comments(statement).splitlines()[0][:70]
            failed.append((index, first_line, str(exc)))

    cur.close()
    return executed, failed


def mysql_setup(args, passwords):
    try:
        import mysql.connector
    except ImportError:
        print("mysql-connector-python is not installed. "
              "Run: pip install -r requirements.txt")
        return False

    root_password = args.root_password or os.getenv("MYSQL_ROOT_PASSWORD")
    if root_password is None:
        root_password = getpass.getpass(
            f"MySQL password for {args.root_user}@{args.host}: ")

    try:
        conn = mysql.connector.connect(
            host=args.host, port=args.port, user=args.root_user,
            password=root_password, autocommit=True, charset="utf8mb4",
            allow_local_infile=True,           # 02_load.sql needs this
        )
    except Exception as exc:
        print(f"\nCould not connect to MySQL as {args.root_user}: {exc}")
        return False

    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    print(f"\nConnected to MySQL {version} as {args.root_user}")

    if not version.startswith("8."):
        print(f"  WARNING: this project needs MySQL 8.0.19 or later "
              f"(CHECK constraints, row aliases, window functions). "
              f"Found {version}.")

    # LOAD DATA LOCAL INFILE has to be allowed on the server too; this
    # is the one global setting the project changes, and it is what the
    # user manual used to ask people to run by hand.
    try:
        cur.execute("SET GLOBAL local_infile = 1")
        print("  local_infile enabled for the bulk load")
    except Exception as exc:
        print(f"  could not enable local_infile ({exc}); 02_load.sql may fail")
    cur.close()

    scripts = list(CORE_SCRIPTS)
    if args.skip_load:
        scripts.remove("02_load.sql")
    if args.with_demos:
        scripts += DEMO_SCRIPTS

    all_ok = True
    for script_name in scripts:
        path = MYSQL_DIR / script_name
        if not path.exists():
            print(f"\n  {script_name}: missing, skipped")
            continue

        text = prepare(script_name, path.read_text(encoding="utf-8"), passwords)
        print(f"\n  {script_name} ...", end="", flush=True)
        started = time.perf_counter()
        executed, failed = run_script(conn, script_name, text, args.verbose)
        elapsed = time.perf_counter() - started
        print(f" {executed} statement(s) in {elapsed:.1f}s", end="")

        if failed:
            all_ok = False
            print(f", {len(failed)} FAILED")
            for index, first_line, message in failed[:5]:
                print(f"      statement {index}: {first_line}\n        {message}")
        else:
            print()

    verify_mysql(conn)
    conn.close()
    return all_ok


def verify_mysql(conn):
    cur = conn.cursor()
    cur.execute("USE moviedb")
    print("\n  Row counts")
    for table, expected in EXPECTED_ROWS.items():
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            mark = "ok " if count == expected else "!! "
            print(f"    {mark}{table:<14} {count:>8,}  (expected {expected:,})")
        except Exception as exc:
            print(f"    !! {table:<14} {exc}")

    for label, query in [
        ("views", "SELECT COUNT(*) FROM information_schema.VIEWS "
                  "WHERE TABLE_SCHEMA = 'moviedb'"),
        ("triggers", "SELECT COUNT(*) FROM information_schema.TRIGGERS "
                     "WHERE TRIGGER_SCHEMA = 'moviedb'"),
        ("CHECK constraints", "SELECT COUNT(*) FROM information_schema.CHECK_CONSTRAINTS "
                              "WHERE CONSTRAINT_SCHEMA = 'moviedb'"),
        ("foreign keys", "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                         "WHERE TABLE_SCHEMA = 'moviedb' "
                         "AND CONSTRAINT_TYPE = 'FOREIGN KEY'"),
        ("indexes", "SELECT COUNT(DISTINCT TABLE_NAME, INDEX_NAME) "
                    "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = 'moviedb'"),
    ]:
        try:
            cur.execute(query)
            print(f"    ok {label:<18} {cur.fetchone()[0]:>8,}")
        except Exception as exc:
            print(f"    !! {label:<18} {exc}")
    cur.close()


# =====================================================================
# Running the MongoDB side
# =====================================================================
def mongo_setup(args):
    script = MONGO_DIR / "setup_mongodb.py"
    if not script.exists():
        print(f"\n{script} is missing.")
        return False

    print("\n" + "-" * 62)
    print("MongoDB")
    print("-" * 62)
    command = [sys.executable, str(script)]
    if args.skip_load:
        command.append("--skip-graph")
    result = subprocess.run(command, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print("MongoDB setup failed.")
        return False

    try:
        from dotenv import load_dotenv
        from pymongo import MongoClient
        load_dotenv(REPO_ROOT / ".env")
        client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"),
                             serverSelectionTimeoutMS=4000)
        db = client[os.getenv("MONGODB_DATABASE", "movie_app")]
        print("\n  Collections")
        for name in sorted(db.list_collection_names()):
            count = db[name].count_documents({})
            indexes = len(list(db[name].list_indexes()))
            print(f"    ok {name:<22} {count:>8,} documents, {indexes} index(es)")
        client.close()
    except Exception as exc:
        print(f"  could not verify MongoDB: {exc}")
        return False
    return True


# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Set up both databases for the INF2003 Team 37 project")
    parser.add_argument("--mysql-only", action="store_true")
    parser.add_argument("--mongo-only", action="store_true")
    parser.add_argument("--skip-load", action="store_true",
                        help="create the objects but load no data")
    parser.add_argument("--with-demos", action="store_true",
                        help="also run 03, 05, 06 and 09 (read-only demos)")
    parser.add_argument("--root-user", default=os.getenv("MYSQL_ROOT_USER", "root"))
    parser.add_argument("--root-password", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--yes", "-y", action="store_true",
                        help="do not ask before dropping the moviedb database")
    parser.add_argument("--verbose", action="store_true",
                        help="print the first rows each statement returns")
    args = parser.parse_args()

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        print("There is no .env file yet. Copy .env.example to .env and put your\n"
              "own passwords in it, then run this script again.\n\n"
              f"  copy \"{REPO_ROOT / '.env.example'}\" \"{env_file}\"")
        return 1

    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        print("python-dotenv is not installed. Run: pip install -r requirements.txt")
        return 1

    args.host = args.host or os.getenv("MYSQL_HOST", "127.0.0.1")
    args.port = args.port or int(os.getenv("MYSQL_PORT", "3306"))

    app_password = os.getenv("MYSQL_PASSWORD", "")
    if not app_password and not args.mongo_only:
        print("MYSQL_PASSWORD is empty in .env. Choose a password for the\n"
              "application account, put it in .env, and run this again.")
        return 1

    passwords = {
        "CHANGE_ME_APP": app_password,
        "CHANGE_ME_REPORT": os.getenv("MYSQL_REPORT_PASSWORD", app_password),
        "CHANGE_ME_BENCH": os.getenv("MYSQL_BENCH_PASSWORD", app_password),
    }

    print("INF2003 Team 37 - database setup")
    print("=" * 62)
    print(f"  repository   {REPO_ROOT}")
    print(f"  data         {CLEANED_DIR}")
    print(f"  MySQL        {args.host}:{args.port}, database "
          f"{os.getenv('MYSQL_DATABASE', 'moviedb')}")
    print(f"  MongoDB      {os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')}, "
          f"database {os.getenv('MONGODB_DATABASE', 'movie_app')}")

    if not CLEANED_DIR.exists() and not args.skip_load:
        print(f"\n{CLEANED_DIR} does not exist, so there is nothing to load.")
        return 1

    if not args.mongo_only and not args.yes:
        print("\n01_schema.sql starts with DROP DATABASE IF EXISTS moviedb.")
        answer = input("Everything in moviedb will be destroyed. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return 1

    ok = True
    if not args.mongo_only:
        print("\n" + "-" * 62)
        print("MySQL")
        print("-" * 62)
        ok = mysql_setup(args, passwords) and ok
    if not args.mysql_only:
        ok = mongo_setup(args) and ok

    print("\n" + "=" * 62)
    if ok:
        print("Setup finished. Start the application with:\n\n    python app.py\n\n"
              "then open http://127.0.0.1:5000")
    else:
        print("Setup finished WITH ERRORS. Read the messages above: the usual\n"
              "causes are a wrong root password, MySQL older than 8.0.19, or\n"
              "local_infile being disabled on the server.")
    print("=" * 62)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
