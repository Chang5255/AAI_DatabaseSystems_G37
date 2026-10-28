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
from decimal import Decimal

import mysql.connector
from bson import ObjectId
from bson.errors import InvalidId
from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from pymongo.errors import PyMongoError

from . import queries as Q
from .db import get_mongo, get_mysql, mongo_timed, mysql_exec, mysql_query

# The team's MongoDB code (database/mongodb) and benchmark helpers,
# importable because db.py put those folders on sys.path.
import activity_logs
import aggregation_queries
import crud_user_tags
import generate_recommendations as recommender
import harness
import mysql_bench
import recommendations as recommendation_store

bp = Blueprint("main", __name__)

PAGE_SIZE = 25
RATING_CHOICES = [x / 2 for x in range(1, 11)]   # 0.5 .. 5.0
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/"   # + size + poster_path

# MySQL error numbers raised by the constraints in 01_schema.sql
ER_DUP_ENTRY = 1062
ER_CHECK_CONSTRAINT = 3819
ER_NO_REFERENCED_ROW = 1452


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
    """Write an activity log document for the current user, if any."""
    user = current_user()
    if user is None:
        return
    mongo_timed(f"Log '{event_type}' (insert_one)", lambda: activity_logs.log_activity(
        user_id=int(user["user_id"]), event_type=event_type,
        movie_id=int(movie_id) if movie_id is not None else None,
        metadata=metadata or {}))


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


def constraint_message(err):
    if err.errno == ER_CHECK_CONSTRAINT:
        return f"Rejected by a CHECK constraint: {err.msg}"
    if err.errno == ER_DUP_ENTRY:
        return f"Rejected by a UNIQUE / PRIMARY KEY constraint: {err.msg}"
    if err.errno == ER_NO_REFERENCED_ROW:
        return f"Rejected by a FOREIGN KEY constraint: {err.msg}"
    return f"MySQL error {err.errno}: {err.msg}"


@bp.app_context_processor
def inject_globals():
    users = []
    try:
        users = mysql_query("SELECT user_id, username, display_name FROM users ORDER BY username",
                            (), "User switcher")
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
    user = require_user()
    if user:
        try:
            value = Decimal(request.form.get("rating_value", ""))
        except ArithmeticError:
            value = None
        if value is None:
            flash("Choose a rating.", "error")
        else:
            try:
                existing = mysql_query(Q.RATING_GET, (user["user_id"], movie_id), "My rating", one=True)
                if existing:
                    mysql_exec(Q.RATING_UPDATE, (value, user["user_id"], movie_id), "Update rating")
                    flash(f"Rating updated to {value}.", "ok")
                else:
                    mysql_exec(Q.RATING_INSERT, (user["user_id"], movie_id, value), "Insert rating")
                    flash(f"Rated {value}.", "ok")
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
        mongo_timed("Create tag (insert_one)", lambda: crud_user_tags.create_tag(
            int(user["user_id"]), movie_id, tag[:100]))
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
        mongo_timed("Update tag (update_one $set)", lambda: crud_user_tags.update_tag(tag_id, new_tag[:100]))
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
    return render_template("users.html", users=mysql_query(Q.USERS, (), "Users (v_user_activity)"))


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


@bp.post("/users/<int:user_id>/delete")
def delete_user(user_id):
    # MySQL: ON DELETE CASCADE removes the user's ratings automatically.
    count, _ = mysql_exec(Q.USER_DELETE, (user_id,), "Delete user (cascades to ratings)")
    # MongoDB has no foreign keys, so the app removes the user's documents itself.
    db = get_mongo()
    removed = sum(mongo_timed(f"delete_many {name}",
                              lambda name=name: db[name].delete_many({"user_id": user_id}).deleted_count)
                  for name in ("user_tags", "user_activity_logs", "recommendations"))
    if session.get("user_id") == user_id:
        session.pop("user_id", None)
    flash(f"Deleted user {user_id} ({count} MySQL row, ratings cascaded; "
          f"{removed} MongoDB documents removed).", "ok")
    return redirect(url_for("main.users"))


@bp.post("/users/<int:user_id>/recommend")
def recommend(user_id):
    recs = mongo_timed("Generate recommendations (aggregation pipelines)",
                       lambda: recommender.generate_recommendations(user_id, limit=10))
    if recs:
        mongo_timed("Save recommendations (insert_one)",
                    lambda: recommender.save_recommendations(user_id, recs))
        flash(f"Generated {len(recs)} recommendations from this user's viewing history.", "ok")
    else:
        flash("No recommendations yet: this user needs to view some films first.", "error")
    return redirect(url_for("main.user", user_id=user_id))


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
