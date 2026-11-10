"""
INF2003 Group Project - Team 37
webapp/routes.py : the pages of the UI

Which database answers what (as in the proposal's architecture):
  MySQL   - catalogue, search, users, ratings, reports (integrity + joins)
  MongoDB - nested movie_features (overview, keywords, cast, crew),
            user tags, activity logs, recommendations
Both are joined on movie_id / user_id.
"""

import inspect
import random
from decimal import Decimal, InvalidOperation

import mysql.connector
from bson import ObjectId
from bson.errors import InvalidId
from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from . import queries as Q
from .db import (get_mongo, get_mysql, mongo_timed, mysql_exec, mysql_query,
                 mysql_transaction)

# The team's MongoDB code (database/mongodb) and benchmark helpers,
# importable because db.py put those folders on sys.path.
import activity_logs
import advanced_queries
import aggregation_queries
import crud_user_tags
import event_types as EV
import generate_recommendations as recommender
import harness
import hybrid_recommendations
import mysql_bench
import recommendations as recommendation_store

bp = Blueprint("main", __name__)

PAGE_SIZE = 25
RATING_CHOICES = [x / 2 for x in range(1, 11)]   # 0.5 .. 5.0
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/"   # + size + poster_path
IMPORT_LIMIT = 200          # lines accepted by the bulk rating import

# MySQL error numbers raised by the constraints in 01_schema.sql and by
# the triggers in 08_triggers.sql
ER_DUP_ENTRY = 1062
ER_CHECK_CONSTRAINT = 3819
# InnoDB reports a missing parent row under two different numbers:
# 1452 for a plain INSERT, and 1216 from inside a transaction. Both mean
# the same thing, so both have to be mapped or the message falls through
# to the unhelpful generic one (seen in the batch importer).
ER_NO_REFERENCED_ROW = 1216
ER_NO_REFERENCED_ROW_2 = 1452
ER_ROW_IS_REFERENCED = 1451
ER_SIGNAL_EXCEPTION = 1644      # SIGNAL '45000' from a trigger
ER_NO_SUCH_TABLE = 1146
ER_TABLEACCESS_DENIED = 1142


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def current_user():
    """The user picked in the top bar (no passwords: users are demo profiles)."""
    if "user" not in g:
        uid = session.get("user_id")
        g.user = mysql_query(Q.USER, (uid,), "Current user", one=True) if uid else None
        if uid and g.user is None:
            session.pop("user_id", None)
    return g.user


def require_user():
    user = current_user()
    if user is None:
        flash("Pick a user in the top bar first.", "error")
    return user


def log_event(event_type, movie_id=None, metadata=None):
    """Write an activity log document for the current user, if any.

    event_type must come from database/mongodb/event_types.py: the same
    list is in the $jsonSchema validator, so anything else is refused
    by the server. Logging is a side effect, never the point of the
    page, so a MongoDB failure here is reported and swallowed rather
    than turned into a 500 for a user who was only browsing.
    """
    user = current_user()
    if user is None:
        return
    if event_type not in EV.EVENT_TYPES:
        raise ValueError(f"'{event_type}' is not in event_types.EVENT_TYPES")
    try:
        mongo_timed(f"Log '{event_type}' (insert_one)", lambda: activity_logs.log_activity(
            user_id=int(user["user_id"]), event_type=event_type,
            movie_id=int(movie_id) if movie_id is not None else None,
            metadata=metadata or {}))
    except PyMongoError as err:
        g.setdefault("log_errors", []).append(f"{event_type}: {err}")


def int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def titles_for(movie_ids):
    """movie_id -> {title, release_year, ...} from MySQL, for MongoDB results."""
    ids = [int(m) for m in dict.fromkeys(movie_ids)]
    if not ids:
        return {}
    sql = Q.TITLES_FOR_IDS.format(placeholders=", ".join(["%s"] * len(ids)))
    rows = mysql_query(sql, tuple(ids), "Titles for MongoDB results")
    return {r["movie_id"]: r for r in rows}


def posters_for(movie_ids):
    """movie_id -> poster_path from MongoDB, for MySQL results.

    Posters are decoration, so if MongoDB is unavailable the MySQL page
    still renders (with placeholders) instead of failing.
    """
    ids = [int(m) for m in dict.fromkeys(movie_ids)]
    if not ids:
        return {}
    try:
        docs = mongo_timed("Posters for MySQL results (find $in)", lambda: list(
            get_mongo().movie_features.find({"movie_id": {"$in": ids}, "poster_path": {"$ne": None}},
                                            {"_id": 0, "movie_id": 1, "poster_path": 1})))
    except PyMongoError:
        return {}
    return {d["movie_id"]: d["poster_path"] for d in docs}


def usernames():
    rows = mysql_query("SELECT user_id, username FROM users", (), "Usernames")
    return {r["user_id"]: r["username"] for r in rows}


def mongo_constraint_message(err):
    """Turn a refused MongoDB write into something a reader can learn from.

    The MongoDB twin of constraint_message() below. Both of these are
    rules the DATABASE enforces, not the application:

      DuplicateKeyError  a unique index refused the write. It is the
                         only way MongoDB can express uniqueness - a
                         $jsonSchema validator cannot - so this is
                         exactly the counterpart of MySQL's 1062.
      WriteError 121     the $jsonSchema validator refused the
                         document: a missing required field, the wrong
                         bsonType, a string outside minLength/maxLength,
                         or an event_type outside its enum.

    Without this, both cases ended up at the generic PyMongoError
    handler, which renders "Cannot reach MongoDB" - badly misleading,
    because MongoDB is fine and working exactly as designed.
    """
    if isinstance(err, DuplicateKeyError):
        key = (err.details or {}).get("keyValue") or {}
        if {"user_id", "movie_id", "tag"} <= set(key):
            return (f"Rejected by the unique index on (user_id, movie_id, tag): "
                    f"you have already put “{key['tag']}” on this film.")
        return f"Rejected by a unique index: {key or err}"
    if isinstance(err, WriteError) and err.code == 121:
        return ("Rejected by the collection's $jsonSchema validator: the document "
                "does not match the rules in create_collections.py (a tag must be "
                "1 to 100 characters).")
    return f"MongoDB refused the write: {err}"


def constraint_message(err):
    """Turn a MySQL error into something a reader can learn from.

    Every branch here is a rule the DATABASE enforces, not the app:
    that is the point of the message. 1644 is a SIGNAL raised by one of
    our own triggers (08_triggers.sql), which is how we express rules a
    CHECK cannot reach; 1142 is the least-privilege design in
    01b_create_user.sql refusing a statement outright.
    """
    if err.errno == ER_CHECK_CONSTRAINT:
        return f"Rejected by a CHECK constraint: {err.msg}"
    if err.errno == ER_DUP_ENTRY:
        return f"Rejected by a UNIQUE / PRIMARY KEY constraint: {err.msg}"
    if err.errno in (ER_NO_REFERENCED_ROW, ER_NO_REFERENCED_ROW_2):
        return f"Rejected by a FOREIGN KEY constraint: {err.msg}"
    if err.errno == ER_ROW_IS_REFERENCED:
        return f"Rejected by a FOREIGN KEY (ON DELETE RESTRICT): {err.msg}"
    if err.errno == ER_SIGNAL_EXCEPTION:
        return f"Rejected by a trigger: {err.msg}"
    if err.errno == ER_TABLEACCESS_DENIED:
        return (f"Refused: the application's database account is not allowed to "
                f"do this. {err.msg}")
    if err.errno == ER_NO_SUCH_TABLE:
        return (f"That table does not exist yet: {err.msg}. "
                f"Run the database/mysql scripts in order.")
    return f"MySQL error {err.errno}: {err.msg}"


SWITCHER_REAL_USERS = 12      # busiest real users offered in the top bar


@bp.app_context_processor
def inject_globals():
    """The "Acting as" box, on every page.

    This used to be "SELECT every user", which was reasonable when
    there were 12 of them. There are now 1,512 - 12 synthetic demo
    accounts and 1,500 real MovieLens users - so loading them all meant
    a 1,500-row query and a 1,500-option dropdown on every single
    request. The box exists to pick someone to demo as, so it offers
    the 12 demo accounts plus the busiest real users, and the Users
    page handles the rest.
    """
    users = []
    try:
        users = mysql_query(Q.USER_SWITCHER_DEMO, (), "User switcher (demo accounts)")
        users += mysql_query(Q.USER_SWITCHER_REAL, (SWITCHER_REAL_USERS,),
                             "User switcher (busiest real users)")
        # Whoever is selected must appear in the list, or the dropdown
        # would silently show somebody else.
        user = current_user()
        if user and all(u["user_id"] != user["user_id"] for u in users):
            users.append({"user_id": user["user_id"], "username": user["username"],
                          "display_name": user["display_name"]})
    except mysql.connector.Error:
        pass
    return {"current_user": current_user() if users else None, "all_users": users}


@bp.app_template_filter("money")
def money(value):
    if value is None:
        return "-"
    value = float(value)
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


@bp.app_template_filter("num")
def num(value, digits=1):
    if value is None:
        return "-"
    if isinstance(value, (float, Decimal)):
        return f"{float(value):.{digits}f}"
    return value


@bp.app_template_filter("dt")
def dt(value):
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M")


@bp.app_template_filter("poster")
def poster(path, size="w185"):
    """TMDB image URL for a poster_path stored in movie_features, or None."""
    return f"{TMDB_IMAGE_BASE}{size}{path}" if path else None


# ---------------------------------------------------------------------
# Switch user
# ---------------------------------------------------------------------
@bp.post("/switch-user")
def switch_user():
    uid = int_or_none(request.form.get("user_id"))
    if uid:
        session["user_id"] = uid
    else:
        session.pop("user_id", None)
    return redirect(request.referrer or url_for("main.browse"))


# ---------------------------------------------------------------------
# Browse and search (MySQL, Q1)
# ---------------------------------------------------------------------
@bp.get("/")
def browse():
    args = request.args
    params = {
        "q": args.get("q", "").strip() or None,
        "genre": args.get("genre") or None,
        "year_from": int_or_none(args.get("year_from")),
        "year_to": int_or_none(args.get("year_to")),
        "lang": args.get("lang") or None,
    }
    page = max(1, int_or_none(args.get("page")) or 1)
    rows = mysql_query(Q.BROWSE, {**params, "limit": PAGE_SIZE + 1,
                                  "offset": (page - 1) * PAGE_SIZE},
                       "Search films (Q1 on v_movie_cards)")
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    if any(params.values()) and page == 1:
        summary = ", ".join(f"{k}={v}" for k, v in params.items() if v is not None and k != "q")
        query = " ".join(x for x in (params["q"], f"[{summary}]" if summary else "") if x)
        log_event("search", metadata={"query": query, "result_count": len(rows), "source": "browse"})

    return render_template(
        "browse.html", rows=rows, params=params, page=page, has_next=has_next,
        offset=(page - 1) * PAGE_SIZE, posters=posters_for(r["movie_id"] for r in rows),
        genres=[r["name"] for r in mysql_query(Q.GENRES, (), "Genre list")],
        languages=mysql_query(Q.LANGUAGES, (), "Language list"),
    )


# ---------------------------------------------------------------------
# Discover (MongoDB): keyword and overview text search
# ---------------------------------------------------------------------
@bp.get("/discover")
def discover():
    mode = request.args.get("mode", "keyword")
    q = request.args.get("q", "").strip()
    db = get_mongo()
    results = []

    if q and mode == "keyword":
        # Exact keyword match on the embedded array: uses idx_keywords_name
        docs = mongo_timed("find {keywords.name: q}", lambda: list(
            db.movie_features.find({"keywords.name": q.lower()},
                                   {"_id": 0, "movie_id": 1, "overview": 1, "keywords.name": 1,
                                    "poster_path": 1})
            .limit(60)))
        results = [{"movie_id": d["movie_id"], "overview": d.get("overview"),
                    "keywords": [k["name"] for k in d.get("keywords", [])],
                    "poster_path": d.get("poster_path")} for d in docs]
    elif q and mode == "overview":
        # Full-text search on overview, ranked by relevance (text index)
        docs = mongo_timed("find {$text: q} sorted by textScore", lambda: list(
            db.movie_features.find({"$text": {"$search": q}},
                                   {"_id": 0, "movie_id": 1, "overview": 1, "keywords.name": 1,
                                    "poster_path": 1, "score": {"$meta": "textScore"}})
            .sort([("score", {"$meta": "textScore"})]).limit(60)))
        results = [{"movie_id": d["movie_id"], "overview": d.get("overview"),
                    "keywords": [k["name"] for k in d.get("keywords", [])],
                    "poster_path": d.get("poster_path"), "score": d.get("score")} for d in docs]

    titles = titles_for(r["movie_id"] for r in results)
    for r in results:
        r["film"] = titles.get(r["movie_id"])

    if q:
        log_event("search", metadata={"query": q, "result_count": len(results),
                                      "source": f"discover_{mode}"})

    top_keywords = mongo_timed("Top keywords ($unwind + $group)",
                               lambda: aggregation_queries.get_top_keywords(30))
    return render_template("discover.html", mode=mode, q=q, results=results,
                           top_keywords=top_keywords)


# ---------------------------------------------------------------------
# People search (MySQL, Q2)
# ---------------------------------------------------------------------
@bp.get("/people")
def people():
    q = request.args.get("q", "").strip()
    groups = []
    if len(q) >= 3:
        rows = mysql_query(Q.PEOPLE_SEARCH, (q, q), "Credits by person (Q2, UNION ALL)")
        by_person = {}
        for r in rows:
            by_person.setdefault((r["person_id"], r["name"]), []).append(r)
        groups = [{"person_id": pid, "name": name, "credits": credits}
                  for (pid, name), credits in by_person.items()]
        log_event("search", metadata={"query": q, "result_count": len(rows), "source": "people"})
    elif q:
        flash("Type at least 3 letters of a name.", "error")
    return render_template("people.html", q=q, groups=groups)


# ---------------------------------------------------------------------
# Movie detail: MySQL core + MongoDB document, ratings and tags
# ---------------------------------------------------------------------
@bp.get("/movie/<int:movie_id>")
def movie(movie_id):
    film = mysql_query(Q.MOVIE, (movie_id,), "Film + genres (Q3a)", one=True)
    if film is None:
        abort(404)

    # The same cast/crew question answered by both databases.
    sql_cast = mysql_query(Q.MOVIE_CAST, (movie_id,), "Cast: 3-table join (Q3b)")
    sql_crew = mysql_query(Q.MOVIE_CREW, (movie_id,), "Crew: 3-table join (Q3c)")
    doc = mongo_timed("movie_features findOne (cast, crew, keywords embedded)",
                      lambda: get_mongo().movie_features.find_one({"movie_id": movie_id}, {"_id": 0}))
    doc = doc or {}

    crew = doc.get("crew", [])
    key_jobs = ("Director", "Screenplay", "Writer", "Producer", "Original Music Composer",
                "Director of Photography", "Editor")
    key_crew = {}
    for c in crew:
        if c.get("job") in key_jobs:
            key_crew.setdefault(c["job"], []).append(c["name"])

    user = current_user()
    my_rating = None
    if user:
        row = mysql_query(Q.RATING_GET, (user["user_id"], movie_id), "My rating", one=True)
        my_rating = row["rating_value"] if row else None

    ratings = mysql_query(Q.MOVIE_RATINGS, (movie_id,), "Ratings for this film")
    tags = mongo_timed("Tags for this film (find)", lambda: crud_user_tags.get_tags_for_movie(movie_id))
    names = usernames() if tags else {}
    for t in tags:
        t["username"] = names.get(t["user_id"], f"user {t['user_id']}")

    # After rating or tagging, the page reloads with from=self: not a new view.
    if request.args.get("from") != "self":
        log_event("view_movie", movie_id, {"source": request.args.get("from", "direct")})

    return render_template(
        "movie.html", film=film, doc=doc, key_crew=key_crew,
        cast=sorted(doc.get("cast", []), key=lambda c: c.get("order", 0))[:20], cast_total=len(doc.get("cast", [])),
        crew_total=len(crew), sql_counts=(len(sql_cast), len(sql_crew)),
        my_rating=my_rating, ratings=ratings, tags=tags, rating_choices=RATING_CHOICES,
    )


@bp.post("/movie/<int:movie_id>/rate")
def rate(movie_id):
    """Rate a film, or change an existing rating, in one statement.

    This used to be SELECT-then-INSERT-or-UPDATE. Two clicks arriving
    together could both read "not rated" and both INSERT, and the
    second one died on the primary key. Q.RATING_UPSERT hands the
    decision to the server instead (INSERT ... ON DUPLICATE KEY
    UPDATE), so the operation is atomic and takes one round trip.
    """
    user = require_user()
    if user:
        try:
            value = Decimal(request.form.get("rating_value", ""))
        except (ArithmeticError, InvalidOperation, TypeError):
            value = None
        if value is None:
            flash("Choose a rating.", "error")
        else:
            try:
                # rowcount: 1 = inserted, 2 = existing row updated.
                affected, _ = mysql_exec(Q.RATING_UPSERT,
                                         (user["user_id"], movie_id, value),
                                         "Rate film (upsert, 1 statement)")
                if affected >= 2:
                    flash(f"Rating changed to {value}.", "ok")
                elif affected == 1:
                    flash(f"Rated {value}.", "ok")
                else:
                    flash(f"Already rated {value}; nothing changed.", "ok")
                log_event("rate_movie", movie_id, {"rating": float(value), "source": "movie_page"})
            except mysql.connector.Error as err:
                flash(constraint_message(err), "error")
    return redirect(url_for("main.movie", movie_id=movie_id, **{"from": "self"}))


@bp.post("/movie/<int:movie_id>/rating/delete")
def delete_rating(movie_id):
    user = require_user()
    if user:
        count, _ = mysql_exec(Q.RATING_DELETE, (user["user_id"], movie_id), "Delete rating")
        flash("Rating deleted." if count else "You had not rated this film.", "ok")
        if count:
            log_event("delete_rating", movie_id, {"source": "movie_page"})
    return redirect(url_for("main.movie", movie_id=movie_id, **{"from": "self"}))


# ---------------------------------------------------------------------
# Tags (MongoDB CRUD, team's crud_user_tags.py)
# ---------------------------------------------------------------------
@bp.post("/movie/<int:movie_id>/tags")
def add_tag(movie_id):
    user = require_user()
    tag = request.form.get("tag", "").strip()
    if user and not tag:
        flash("Type a tag first.", "error")
    elif user:
        try:
            mongo_timed("Create tag (insert_one)", lambda: crud_user_tags.create_tag(
                int(user["user_id"]), movie_id, tag[:100]))
        except (DuplicateKeyError, WriteError) as err:
            flash(mongo_constraint_message(err), "error")
        else:
            log_event("add_tag", movie_id, {"source": "movie_page"})
            flash(f"Tag “{tag[:100]}” added.", "ok")
    return redirect(url_for("main.movie", movie_id=movie_id, **{"from": "self"}))


def _own_tag(tag_id):
    """Return the tag if it belongs to the current user, else None."""
    user = require_user()
    if user is None:
        return None
    try:
        tag = get_mongo().user_tags.find_one({"_id": ObjectId(tag_id)})
    except InvalidId:
        tag = None
    if tag is None or tag["user_id"] != user["user_id"]:
        flash("You can only change your own tags.", "error")
        return None
    return tag


@bp.post("/tags/<tag_id>/edit")
def edit_tag(tag_id):
    tag = _own_tag(tag_id)
    new_tag = request.form.get("tag", "").strip()
    if tag and new_tag:
        try:
            mongo_timed("Update tag (update_one $set)",
                        lambda: crud_user_tags.update_tag(tag_id, new_tag[:100]))
        except (DuplicateKeyError, WriteError) as err:
            flash(mongo_constraint_message(err), "error")
        else:
            flash("Tag updated.", "ok")
    return _back_to_movie(tag)


@bp.post("/tags/<tag_id>/delete")
def delete_tag(tag_id):
    tag = _own_tag(tag_id)
    if tag:
        mongo_timed("Delete tag (delete_one)", lambda: crud_user_tags.delete_tag(tag_id))
        flash("Tag deleted.", "ok")
    return _back_to_movie(tag)


def _back_to_movie(tag):
    if tag:
        return redirect(url_for("main.movie", movie_id=tag["movie_id"], **{"from": "self"}))
    return redirect(request.referrer or url_for("main.browse"))


# ---------------------------------------------------------------------
# Users (MySQL CRUD) and profile (both databases)
# ---------------------------------------------------------------------
@bp.get("/users")
def users():
    """Paged and searchable, because there are 1,512 users.

    Demo accounts first, then real MovieLens users by how much they
    have rated.
    """
    args = request.args
    kind = args.get("kind") if args.get("kind") in ("demo", "real") else None
    params = {"q": args.get("q", "").strip() or None, "kind": kind}
    page = max(1, int_or_none(args.get("page")) or 1)

    rows = mysql_query(Q.USERS, {**params, "limit": PAGE_SIZE + 1,
                                 "offset": (page - 1) * PAGE_SIZE},
                       "Users (v_user_activity, paged)")
    has_next = len(rows) > PAGE_SIZE
    totals = mysql_query(Q.USERS_COUNT, (), "User counts", one=True)
    return render_template("users.html", users=rows[:PAGE_SIZE], params=params,
                           page=page, has_next=has_next,
                           offset=(page - 1) * PAGE_SIZE, totals=totals)


@bp.post("/users")
def create_user():
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip() or None
    try:
        _, new_id = mysql_exec(Q.USER_CREATE, (username, display_name), "Insert user")
        session["user_id"] = new_id
        flash(f"User {username} created and selected.", "ok")
        return redirect(url_for("main.user", user_id=new_id))
    except mysql.connector.Error as err:
        flash(constraint_message(err), "error")
        return redirect(url_for("main.users"))


@bp.get("/users/<int:user_id>")
def user(user_id):
    profile = mysql_query(Q.USER, (user_id,), "User", one=True)
    if profile is None:
        abort(404)
    ratings = mysql_query(Q.USER_RATINGS, (user_id,), "This user's ratings")
    fav_genres = mysql_query(Q.USER_FAVOURITE_GENRE, (user_id,), "Favourite genres (Q14)")

    tags = mongo_timed("This user's tags (find)", lambda: crud_user_tags.get_tags_by_user(user_id))
    activity = mongo_timed("Activity history (find, sort, limit)",
                           lambda: activity_logs.get_user_activity(user_id, limit=25))
    latest = mongo_timed("Latest recommendations (find_one sorted)",
                         lambda: recommendation_store.get_latest_recommendations(user_id))
    recs = (latest or {}).get("recommendations", [])

    titles = titles_for([t["movie_id"] for t in tags]
                        + [a["movie_id"] for a in activity if "movie_id" in a]
                        + [r["movie_id"] for r in recs])
    posters = posters_for([r["movie_id"] for r in recs]
                          + [a["movie_id"] for a in activity if a.get("movie_id")])
    return render_template("user.html", profile=profile, ratings=ratings, fav_genres=fav_genres,
                           tags=tags, activity=activity, latest=latest, recs=recs, titles=titles,
                           posters=posters)


@bp.post("/users/<int:user_id>/edit")
def edit_user(user_id):
    display_name = request.form.get("display_name", "").strip() or None
    try:
        mysql_exec(Q.USER_UPDATE, (display_name, user_id), "Update user")
        flash("Display name updated.", "ok")
    except mysql.connector.Error as err:
        flash(constraint_message(err), "error")
    return redirect(url_for("main.user", user_id=user_id))


MONGO_USER_COLLECTIONS = ("user_tags", "user_activity_logs", "recommendations")


@bp.post("/users/<int:user_id>/delete")
def delete_user(user_id):
    """Delete a user from BOTH databases, or from neither.

    This is the hardest operation in the application, because the two
    databases cannot share a transaction: MySQL knows nothing about
    MongoDB's writes and vice versa, so there is no two-phase commit to
    lean on. The earlier version deleted from MySQL, then from MongoDB,
    and hoped. If MongoDB was down at that moment the user's ratings
    were gone while their tags, activity and recommendations stayed
    behind for ever, pointing at a user_id that no longer existed.

    The fix is the compensating-transaction (saga) pattern:

      1. snapshot  read everything MongoDB holds for this user, and the
                   user's own MySQL rows, into memory
      2. MySQL     delete inside an explicit transaction, but DO NOT
                   commit yet; ratings go with the user through
                   fk_ratings_user ON DELETE CASCADE
      3. MongoDB   delete the documents. If this throws, the MySQL
                   transaction is rolled back and nothing was lost
      4. commit    commit MySQL. If the commit itself fails, MongoDB
                   has already been changed, so the snapshot is written
                   back - the compensating action

    What it is not: atomic. Between steps 3 and 4 a crash would leave
    MongoDB empty and MySQL intact, and no application-level pattern
    can close that window. The honest summary for the report is that
    this makes the failure window small and recoverable rather than
    eliminating it, which is exactly the guarantee you give up when
    data is split across two stores.
    """
    db = get_mongo()

    # 1. Snapshot, before anything is destroyed.
    try:
        snapshot = {name: mongo_timed(
            f"Snapshot {name} (find)",
            lambda name=name: list(db[name].find({"user_id": user_id})))
            for name in MONGO_USER_COLLECTIONS}
    except PyMongoError as err:
        flash(f"MongoDB is unreachable, so the delete was not attempted: {err}",
              "error")
        return redirect(url_for("main.users"))

    profile = mysql_query(Q.USER, (user_id,), "User to delete", one=True)
    if profile is None:
        abort(404)
    ratings = mysql_query(Q.USER_RATINGS_SNAPSHOT, (user_id,),
                          "Snapshot this user's ratings")
    mongo_total = sum(len(docs) for docs in snapshot.values())

    try:
        # 2 and 3: MySQL first but uncommitted, then MongoDB.
        with mysql_transaction(f"Delete user {user_id}") as cur:
            cur.execute(Q.USER_DELETE, (user_id,))
            deleted_rows = cur.rowcount

            # A PyMongoError here leaves the with-block by raising,
            # which rolls MySQL back, so the user and their ratings
            # survive untouched.
            removed = sum(mongo_timed(
                f"delete_many {name}",
                lambda name=name: db[name].delete_many(
                    {"user_id": user_id}).deleted_count)
                for name in MONGO_USER_COLLECTIONS)

    except PyMongoError as err:
        flash(f"MongoDB delete failed, so the MySQL delete was rolled back: "
              f"{err}. Nothing was deleted.", "error")
        return redirect(url_for("main.users"))

    except mysql.connector.Error as err:
        # 4. MySQL refused or failed to commit, but MongoDB may already
        # be gone: put the documents back.
        restored = _restore_mongo_snapshot(db, snapshot)
        flash(f"{constraint_message(err)} The MySQL delete was rolled back and "
              f"{restored} MongoDB document(s) were restored from the snapshot.",
              "error")
        return redirect(url_for("main.users"))

    if session.get("user_id") == user_id:
        session.pop("user_id", None)

    flash(f"Deleted user {user_id} ({profile['username']}): {deleted_rows} MySQL row "
          f"+ {len(ratings)} rating(s) by ON DELETE CASCADE, and {removed} MongoDB "
          f"document(s) in {len(MONGO_USER_COLLECTIONS)} collections "
          f"({mongo_total} were snapshotted first). "
          f"Note: the ratings removed by CASCADE are not in rating_audit, because "
          f"MySQL does not fire triggers for foreign-key actions.", "ok")
    return redirect(url_for("main.users"))


def _restore_mongo_snapshot(db, snapshot):
    """Compensating action: re-insert the documents we deleted.

    The documents keep their original _id values, so restoring is
    idempotent: a second attempt fails on the duplicate key rather than
    creating copies.
    """
    restored = 0
    for name, documents in snapshot.items():
        if not documents:
            continue
        try:
            result = mongo_timed(
                f"Restore {name} (insert_many)",
                lambda name=name, documents=documents: db[name].insert_many(
                    documents, ordered=False))
            restored += len(result.inserted_ids)
        except PyMongoError:
            # Already there, or MongoDB is down as well; the snapshot
            # is logged so the data can be put back by hand.
            g.setdefault("log_errors", []).append(
                f"could not restore {len(documents)} {name} documents")
    return restored


@bp.post("/users/<int:user_id>/recommend")
def recommend(user_id):
    """MongoDB-only recommender: keywords of the films this user viewed."""
    recs = mongo_timed("Generate recommendations (aggregation pipelines)",
                       lambda: recommender.generate_recommendations(user_id, limit=10))
    if recs:
        mongo_timed("Save recommendations (insert_one)",
                    lambda: recommender.save_recommendations(user_id, recs))
        flash(f"Generated {len(recs)} recommendations from this user's viewing history.", "ok")
    else:
        flash("No recommendations yet: this user needs to view some films first.", "error")
    log_event(EV.GENERATE_RECOMMENDATIONS, metadata={"algorithm": "keyword_similarity",
                                                     "results": len(recs or [])})
    return redirect(url_for("main.user", user_id=user_id))


@bp.post("/users/<int:user_id>/recommend-hybrid")
def recommend_hybrid(user_id):
    """Recommender that reads from both databases.

    MySQL supplies the ratings (what this user actually liked, and how
    much) and their favourite genres; MongoDB supplies the keywords and
    does the scoring. See database/mongodb/hybrid_recommendations.py.
    """
    db = get_mongo()
    cur = get_mysql().cursor()
    try:
        recs, based_on = mongo_timed(
            "Hybrid recommender (MySQL ratings + MongoDB keywords)",
            lambda: hybrid_recommendations.generate(db, cur, user_id, limit=10))
    except mysql.connector.Error as err:
        flash(constraint_message(err), "error")
        return redirect(url_for("main.user", user_id=user_id))
    finally:
        cur.close()

    if recs:
        mongo_timed("Save recommendations (insert_one)",
                    lambda: hybrid_recommendations.save(db, user_id, recs, based_on))
        flash(f"Generated {len(recs)} recommendations from {based_on['liked_films']} "
              f"film(s) this user rated {based_on['min_rating']}+ "
              f"({based_on['profile_keywords']} keywords in the taste profile"
              + (f", boosted for {', '.join(based_on['favourite_genres'])}"
                 if based_on.get("favourite_genres") else "") + ").", "ok")
    else:
        flash(f"No hybrid recommendations: {based_on.get('reason', 'unknown reason')}. "
              f"Rate a few films 4 stars or more as this user first.", "error")
    log_event(EV.GENERATE_RECOMMENDATIONS,
              metadata={"algorithm": hybrid_recommendations.ALGORITHM,
                        "results": len(recs or [])})
    return redirect(url_for("main.user", user_id=user_id))


# ---------------------------------------------------------------------
# Bulk import of ratings: one transaction, one SAVEPOINT per line
# ---------------------------------------------------------------------
def _parse_import(text):
    """Parse "movie_id, rating" lines into (line_no, movie_id, value, error)."""
    parsed = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(parsed) >= IMPORT_LIMIT:
            break
        parts = [p.strip() for p in line.replace("\t", ",").split(",") if p.strip()]
        if len(parts) < 2:
            parsed.append((line_no, None, None, "expected 'movie_id, rating'"))
            continue
        movie_id, value, error = int_or_none(parts[0]), None, None
        if movie_id is None:
            error = f"'{parts[0]}' is not a film id"
        else:
            try:
                value = Decimal(parts[1])
            except (ArithmeticError, InvalidOperation, TypeError):
                error = f"'{parts[1]}' is not a number"
        parsed.append((line_no, movie_id, value, error))
    return parsed


@bp.route("/import-ratings", methods=["GET", "POST"])
def import_ratings():
    """Paste a list of ratings and load it in one transaction.

    This is the SAVEPOINT demo from database/mysql/09_transactions.sql,
    driven from the UI, and the two modes are the point of the page:

      per-line (default)  SAVEPOINT before each row; a row that the
                          database refuses is undone with ROLLBACK TO
                          SAVEPOINT and the import carries on, so the
                          good rows are still committed together.
      all-or-nothing      no savepoints; the first refusal rolls back
                          the entire transaction (atomicity, the A in
                          ACID).

    Every rejection comes from the database, not from Python: the range
    and half-star CHECKs, the foreign key on movie_id, and the trigger
    that refuses unreleased films. The app only reports them.
    """
    user = current_user()
    text = request.form.get("ratings", "") if request.method == "POST" else ""
    all_or_nothing = request.form.get("mode") == "all"
    results, committed, mode_used = [], None, None

    if request.method == "POST":
        if user is None:
            flash("Pick a user in the top bar first.", "error")
        else:
            parsed = _parse_import(text)
            if not parsed:
                flash("Nothing to import. One 'movie_id, rating' per line.", "error")
            else:
                mode_used = "all-or-nothing" if all_or_nothing else "per-line savepoints"
                results, committed = _run_import(user["user_id"], parsed, all_or_nothing)
                ok = sum(1 for r in results if r["status"] == "ok")
                if committed:
                    flash(f"Committed {ok} of {len(parsed)} line(s) "
                          f"({mode_used}).", "ok")
                    log_event(EV.IMPORT_RATINGS, metadata={
                        "lines": len(parsed), "accepted": ok, "mode": mode_used})
                else:
                    # In all-or-nothing mode the loop stops at the first
                    # refusal, so results holds everything up to it.
                    refused = next((r for r in results if r["status"] == "failed"), None)
                    where = f"line {refused['line']} was refused" if refused else "a line was refused"
                    flash(f"Rolled back: {where}, and the mode was all-or-nothing, so "
                          f"none of the {len(parsed)} line(s) were kept — including the "
                          f"{ok} that the database had already accepted.", "error")

    sample = mysql_query(
        """SELECT m.movie_id, m.title
             FROM movies m
            WHERE m.vote_count > 3000
              AND NOT EXISTS (SELECT 1 FROM ratings r
                               WHERE r.movie_id = m.movie_id AND r.user_id = %s)
            ORDER BY m.popularity DESC
            LIMIT 6""",
        (user["user_id"] if user else 0,), "Films to suggest for the import")

    return render_template("import_ratings.html", text=text, results=results,
                           committed=committed, mode_used=mode_used,
                           all_or_nothing=all_or_nothing, sample=sample,
                           limit=IMPORT_LIMIT)


def _run_import(user_id, parsed, all_or_nothing):
    """Execute the parsed lines. Returns (results, committed)."""
    results = []
    label = ("Import ratings (all-or-nothing)" if all_or_nothing
             else "Import ratings (savepoint per line)")
    try:
        with mysql_transaction(label) as cur:
            for index, (line_no, movie_id, value, error) in enumerate(parsed):
                if error:
                    # Could not even be parsed: no statement is sent.
                    results.append({"line": line_no, "movie_id": movie_id,
                                    "value": value, "status": "skipped",
                                    "detail": error})
                    continue

                savepoint = f"line_{index}"
                if not all_or_nothing:
                    cur.execute(f"SAVEPOINT {savepoint}")
                try:
                    cur.execute(Q.RATING_UPSERT, (user_id, movie_id, value))
                    results.append({
                        "line": line_no, "movie_id": movie_id, "value": value,
                        "status": "ok",
                        "detail": "updated" if cur.rowcount >= 2 else "inserted"})
                except mysql.connector.Error as err:
                    if all_or_nothing:
                        results.append({"line": line_no, "movie_id": movie_id,
                                        "value": value, "status": "failed",
                                        "detail": constraint_message(err)})
                        raise
                    cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    results.append({"line": line_no, "movie_id": movie_id,
                                    "value": value, "status": "failed",
                                    "detail": constraint_message(err)})
    except mysql.connector.Error:
        return results, False
    return results, True


# ---------------------------------------------------------------------
# Audit log written by the triggers in 08_triggers.sql
# ---------------------------------------------------------------------
@bp.get("/audit")
def audit():
    """Every change ever made to a rating, recorded by MySQL itself.

    Nothing in the application writes to rating_audit - it has no
    privilege to (01b_create_user.sql). The rows appear because three
    AFTER triggers put them there, which means a change made in
    Workbench is recorded too. changed_by shows which account did it.
    """
    limit = min(max(int_or_none(request.args.get("limit")) or 100, 10), 1000)
    try:
        rows = mysql_query(Q.AUDIT_RECENT, (limit,), "Audit trail (recent changes)")
        summary = mysql_query(Q.AUDIT_SUMMARY, (), "Audit summary (WITH ROLLUP)")
    except mysql.connector.Error as err:
        if err.errno in (ER_NO_SUCH_TABLE, ER_TABLEACCESS_DENIED):
            flash(constraint_message(err), "error")
            return render_template("audit.html", rows=[], summary=[], limit=limit,
                                   missing=True)
        raise
    return render_template("audit.html", rows=rows, summary=summary, limit=limit,
                           missing=False)


# ---------------------------------------------------------------------
# Network: $graphLookup over the co-star graph
# ---------------------------------------------------------------------
@bp.get("/network")
def network():
    """Degrees of separation between two actors (MongoDB).

    Two different MongoDB techniques, side by side:
      * $graphLookup walks the graph inside the server and reports how
        many people sit at each degree
      * a breadth-first search with indexed $in lookups returns the
        actual chain, which $graphLookup cannot do

    The graph itself is people_graph, derived from the cast arrays by
    database/mongodb/build_costar_graph.py.
    """
    db = get_mongo()
    name_a = request.args.get("a", "").strip()
    name_b = request.args.get("b", "").strip()
    max_degrees = min(max(int_or_none(request.args.get("degrees")) or 3, 1), 4)

    has_graph = mongo_timed("people_graph present?",
                            lambda: db.people_graph.find_one({}, {"_id": 1}))
    if not has_graph:
        return render_template("network.html", missing=True, name_a=name_a,
                               name_b=name_b, max_degrees=max_degrees)

    matches_a = mongo_timed("Find person A (regex on name)",
                            lambda: advanced_queries.find_people(db, name_a))
    matches_b = mongo_timed("Find person B (regex on name)",
                            lambda: advanced_queries.find_people(db, name_b))

    person_a = matches_a[0] if matches_a else None
    person_b = matches_b[0] if matches_b else None
    path, degrees_rows, together = None, [], []

    if person_a:
        degrees_rows = mongo_timed(
            f"$graphLookup to {max_degrees} degree(s)",
            lambda: advanced_queries.costar_network(
                db, person_a["person_id"], max_degrees=min(max_degrees, 2)))

    if person_a and person_b:
        path = mongo_timed(
            "Breadth-first search for the shortest chain",
            lambda: advanced_queries.degrees_of_separation(
                db, person_a["person_id"], person_b["person_id"], max_degrees))
        together = mongo_timed(
            "Films they share (multikey $all)",
            lambda: advanced_queries.shared_films(
                db, person_a["person_id"], person_b["person_id"]))
        log_event(EV.VIEW_NETWORK, metadata={
            "from": person_a["name"], "to": person_b["name"],
            "degrees": path.get("degrees")})

    film_ids = [f["movie_id"] for f in together]
    return render_template("network.html", missing=False, name_a=name_a, name_b=name_b,
                           matches_a=matches_a, matches_b=matches_b,
                           person_a=person_a, person_b=person_b, path=path,
                           degrees_rows=degrees_rows, max_degrees=max_degrees,
                           together=together, titles=titles_for(film_ids),
                           posters=posters_for(film_ids))


# ---------------------------------------------------------------------
# Insights: $facet, $bucket and $lookup
# ---------------------------------------------------------------------
@bp.get("/insights")
def insights():
    """Six breakdowns of the catalogue from ONE pass over MongoDB.

    $facet runs independent sub-pipelines over the same documents, so
    this page costs one round trip where MySQL would need six queries.
    The cast-size panel is a server-side histogram ($bucket) and the
    tag panel is a join between two collections ($lookup).
    """
    db = get_mongo()
    limit = min(max(int_or_none(request.args.get("limit")) or 10, 3), 25)

    facets = mongo_timed("$facet: 6 breakdowns in one pass",
                         lambda: advanced_queries.catalogue_facets(db, limit))
    tagged = mongo_timed("$lookup: most-tagged films joined to movie_features",
                         lambda: advanced_queries.tagged_films(db, limit))
    vocabulary = mongo_timed("$sortByCount on tags",
                             lambda: advanced_queries.tag_vocabulary(db, limit))

    titles = titles_for([row["movie_id"] for row in tagged])
    log_event(EV.VIEW_INSIGHTS, metadata={"limit": limit})
    return render_template("insights.html", facets=facets, tagged=tagged,
                           vocabulary=vocabulary, titles=titles, limit=limit,
                           source=inspect.getsource(advanced_queries.catalogue_facets))


# ---------------------------------------------------------------------
# Reports: SQL (06_queries.sql) and MongoDB aggregations
# ---------------------------------------------------------------------
def _mongo_reports():
    def most_viewed():
        rows = aggregation_queries.get_most_viewed_movies(15)
        titles = titles_for([r["movie_id"] for r in rows]) if rows else {}
        return [{"movie_id": r["movie_id"],
                 "title": (titles.get(r["movie_id"]) or {}).get("title"),
                 "views": r["views"]} for r in rows]

    def tag_counts():
        rows = list(get_mongo().user_tags.aggregate([
            {"$group": {"_id": "$tag", "uses": {"$sum": 1},
                        "films": {"$addToSet": "$movie_id"}}},
            {"$project": {"_id": 0, "tag": "$_id", "uses": 1, "films": {"$size": "$films"}}},
            {"$sort": {"uses": -1, "tag": 1}},
            {"$limit": 20},
        ]))
        return rows

    def tagged_with_films():
        rows = advanced_queries.tagged_films(get_mongo(), 20)
        titles = titles_for([r["movie_id"] for r in rows]) if rows else {}
        return [{"movie_id": r["movie_id"],
                 "title": (titles.get(r["movie_id"]) or {}).get("title"),
                 "tag_count": r["tag_count"],
                 "distinct_taggers": r["distinct_taggers"],
                 "tags": ", ".join(r["tags"][:5]),
                 "keywords": ", ".join(r["keywords"][:5]),
                 "orphan": r["orphan"]} for r in rows]

    def degree_distribution():
        db = get_mongo()
        if db.people_graph.find_one({}, {"_id": 1}) is None:
            return []       # build_costar_graph.py has not been run yet
        return list(db.people_graph.aggregate([
            {"$project": {"_id": 0, "costars": {"$size": "$costar_ids"}}},
            {"$bucket": {"groupBy": "$costars",
                         "boundaries": [0, 5, 10, 25, 50, 100, 250, 10000],
                         "default": "unknown",
                         "output": {"people": {"$sum": 1},
                                    "avg_costars": {"$avg": "$costars"}}}},
            {"$project": {"_id": 0, "from_costars": "$_id", "people": 1,
                          "avg_costars": {"$round": ["$avg_costars", 1]}}},
        ]))

    return {
        "mongo_keywords": ("Most common movie keywords", "$unwind + $group + $sort",
                           lambda: aggregation_queries.get_top_keywords(20),
                           aggregation_queries.get_top_keywords),
        "mongo_activity": ("Activity by event type", "$group on user_activity_logs",
                           aggregation_queries.get_activity_counts,
                           aggregation_queries.get_activity_counts),
        "mongo_viewed": ("Most viewed films", "$match + $group, titles joined from MySQL",
                         most_viewed, aggregation_queries.get_most_viewed_movies),
        "mongo_tags": ("Most used tags", "$group + $addToSet + $size", tag_counts, tag_counts),
        "mongo_tagged_films": ("Most-tagged films, joined to their document",
                               "$lookup - a join between two collections",
                               tagged_with_films, advanced_queries.tagged_films),
        "mongo_costar_degrees": ("How connected are actors?",
                                 "$bucket - a server-side histogram over people_graph",
                                 degree_distribution, degree_distribution),
    }


@bp.get("/reports")
def reports():
    mongo_reports = _mongo_reports()
    key = request.args.get("r", "top_weighted")
    if key in Q.SQL_REPORTS:
        title, feature, sql = Q.SQL_REPORTS[key]
        rows = mysql_query(sql, (), title)
        source, database = sql, "MySQL"
    elif key in mongo_reports:
        title, feature, fn, code_fn = mongo_reports[key]
        rows = mongo_timed(title, fn)
        source, database = inspect.getsource(code_fn), "MongoDB"
    else:
        abort(404)
    columns = list(rows[0].keys()) if rows else []
    return render_template(
        "reports.html", key=key, title=title, feature=feature, rows=rows, columns=columns,
        source=source, database=database,
        sql_menu=[(k, v[0]) for k, v in Q.SQL_REPORTS.items()],
        mongo_menu=[(k, v[0]) for k, v in mongo_reports.items()],
    )


# ---------------------------------------------------------------------
# Compare: the benchmark tasks run live, timed with benchmark/harness.py
# ---------------------------------------------------------------------
def _plan_stages(plan):
    # On MongoDB 6.0+ a query answered by the slot-based engine puts the
    # stage tree one level down under "queryPlan", next to a
    # "slotBasedPlan" sibling. Classic-engine finds keep "stage" at the
    # top. Unwrapping first means the page reads IXSCAN either way
    # instead of falling through to "?".
    if isinstance(plan, dict) and "stage" not in plan and "queryPlan" in plan:
        plan = plan["queryPlan"]
    stages = []
    while plan:
        stages.append(plan.get("stage", "?") + (f" ({plan['indexName']})" if "indexName" in plan else ""))
        plan = plan.get("inputStage") or (plan.get("inputStages") or [None])[0]
    return " ← ".join(stages)


@bp.get("/compare")
def compare():
    samples = harness.load_samples()
    movie_id = int_or_none(request.args.get("movie_id"))
    person_id = int_or_none(request.args.get("person_id"))
    repeats = min(max(int_or_none(request.args.get("repeats")) or 20, 3), 200)
    if request.args.get("random"):
        movie_id = random.choice(samples["movie_ids"])
        person_id = random.choice(samples["actor_person_ids"])
    movie_id = movie_id or 19995
    person_id = person_id or samples["actor_person_ids"][0]

    film = mysql_query("SELECT title FROM movies WHERE movie_id = %s", (movie_id,), "Title", one=True)
    person = mysql_query("SELECT name FROM people WHERE person_id = %s", (person_id,), "Name", one=True)
    results, plans = [], {}
    if film and person:
        cur = get_mysql().cursor()
        coll = get_mongo().movie_features

        def mongo_t1():
            d = coll.find_one({"movie_id": movie_id}, {"cast": 1, "crew": 1}) or {}
            return len(d.get("cast", [])) + len(d.get("crew", []))

        def mongo_t3():
            return len(list(coll.find({"cast.person_id": person_id}, {"movie_id": 1, "cast.$": 1})))

        tasks = [
            ("T1", "One film's cast + crew", "MySQL", "2 queries, each a 3-table join",
             lambda: mysql_bench.t1(cur, movie_id)),
            ("T2", "One film's cast + crew", "MySQL", "1 statement returning JSON",
             lambda: mysql_bench.t2(cur, movie_id)),
            ("T1", "One film's cast + crew", "MongoDB", "find_one on movie_id (embedded arrays)",
             mongo_t1),
            ("T3", "All films for one actor", "MySQL", "WHERE person_id = ? on movie_cast (indexed)",
             lambda: mysql_bench.t3(cur, person_id)),
            ("T3", "All films for one actor", "MongoDB", "find on cast.person_id, cast.$ projection",
             mongo_t3),
        ]
        for task, question, database, method, fn in tasks:
            samples_ms, rows = harness.time_call(fn, repeats=repeats, warmup=1)
            results.append({"task": task, "question": question, "db": database, "method": method,
                            "rows": rows, **harness.summarise(samples_ms)})
        cur.close()

        plans["MongoDB T3"] = _plan_stages(
            coll.find({"cast.person_id": person_id}, {"movie_id": 1, "cast.$": 1})
            .explain()["queryPlanner"]["winningPlan"])
        plans["MongoDB T1"] = _plan_stages(
            coll.find({"movie_id": movie_id}).limit(1).explain()["queryPlanner"]["winningPlan"])

        slowest = max(r["median_ms"] for r in results) or 1
        for r in results:
            r["bar"] = max(2, round(r["median_ms"] / slowest * 100))

    return render_template("compare.html", results=results, plans=plans, movie_id=movie_id,
                           person_id=person_id, repeats=repeats, film=film, person=person,
                           samples=samples)


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------
@bp.app_errorhandler(404)
def not_found(_err):
    return render_template("error.html", title="Not found",
                           message="That page, film or user does not exist."), 404
